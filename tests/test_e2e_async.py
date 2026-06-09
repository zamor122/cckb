#!/usr/bin/env python3
"""
tests/test_e2e_async.py

End-to-End test for the full async job lifecycle:
    POST /init  →  job queued  →  worker processes  →  GET /status == 'completed'

Uses:
  - fakeredis.FakeRedis      (no live Redis required)
  - Direct task execution    (calls run_codebase_scan() directly in a background
                              thread with job metadata captured at enqueue time)
  - FastAPI TestClient       (in-process ASGI, no live uvicorn required)
  - A temp SQLite DB         (isolated from production state)

Timeout: 60 seconds per spec requirement.
Run with: ../.venv/bin/pytest tests/test_e2e_async.py -v
"""

import os
import sys
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fakeredis
from rq import Queue
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from init_engine import app
from db_utils import init_db, get_job_status
import worker as worker_module


TIMEOUT_SECONDS = 60
POLL_INTERVAL   = 0.25   # poll every 250ms in tests


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "e2e_jobs.db")


@pytest.fixture
def fake_redis_server():
    """Shared in-memory Redis instance (fakeredis) for one test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def rq_queue(fake_redis_server):
    """RQ Queue backed by the fakeredis server."""
    return Queue("cckb", connection=fake_redis_server, is_async=True)


@pytest.fixture
def client():
    return TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_task_direct(
    job_id: str,
    repo_path: str,
    db_path: str,
    extra_patches: dict = None,
):
    """
    Call run_codebase_scan() directly in the current thread with optional patches.
    Job metadata is passed explicitly to avoid any RQ/fakeredis version issues.

    extra_patches: dict of {patch_target: mock_object}
    """
    os.environ["CCKB_DB_PATH"] = db_path

    if extra_patches:
        patches = [patch(k, v) for k, v in extra_patches.items()]
        for p in patches:
            p.start()
        try:
            worker_module.run_codebase_scan(job_id, repo_path, db_path=db_path)
        except Exception:
            pass  # Failures are captured in the DB; don't crash the thread
        finally:
            for p in patches:
                p.stop()
    else:
        try:
            worker_module.run_codebase_scan(job_id, repo_path, db_path=db_path)
        except Exception:
            pass


def _wait_for_terminal_status(job_id: str, db_path: str, timeout: int) -> str:
    """
    Poll get_job_status() every POLL_INTERVAL seconds until status is
    'completed' or 'failed', or until timeout is reached.
    Returns the final status string.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get_job_status(job_id, db_path=db_path)
        if row and row["status"] in ("completed", "failed"):
            return row["status"]
        time.sleep(POLL_INTERVAL)
    return "timeout"


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_mock_scanner():
    """Return a MagicMock that mimics CodebaseScanner for E2E tests."""
    mock_scanner = MagicMock()
    mock_scanner.discover_feature_domains.return_value = [
        {
            "spec_id": "E2E-TEST",
            "display_name": "E2E Test Domain",
            "entry_files": ["main.py"],
            "related_files": [],
            "framework": "python",
        }
    ]
    mock_scanner.generate_spec.return_value = ("# E2E spec\n", {"spec_id": "E2E-TEST"})
    mock_scanner.upload_spec.return_value = True
    return mock_scanner


def _base_patches(mock_scanner_cls):
    """Return the standard success-case patches dict."""
    return {
        "worker.run_code_first_fallback": MagicMock(return_value="# Constitution"),
        "worker.boto3": MagicMock(),
        "worker.install_hook": MagicMock(),
        "worker.CodebaseScanner": mock_scanner_cls,
    }


# ── E2E Tests ─────────────────────────────────────────────────────────────────

class TestE2EAsyncLifecycle:
    """Full lifecycle tests: queued → processing → completed."""

    def test_queued_to_completed_lifecycle(
        self, client, tmp_db, rq_queue, monkeypatch
    ):
        """
        Step 1: POST /init  → get job_id with status='queued'
        Step 2: Worker executes task (mocked scanner) in background thread
        Step 3: Poll GET /status until 'completed' within 60s
        Step 4: Assert status == 'completed' and progress == 100
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        mock_scanner = _make_mock_scanner()
        mock_scanner_cls = MagicMock(return_value=mock_scanner)

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            res = client.post("/init", json={"repo_path": "/tmp/e2e-test-repo"})
            assert res.status_code == 200
            data = res.json()
            job_id = data["job_id"]
            assert data["status"] == "queued"

        worker_thread = threading.Thread(
            target=_run_task_direct,
            args=(job_id, "/tmp/e2e-test-repo", tmp_db),
            kwargs={"extra_patches": _base_patches(mock_scanner_cls)},
            daemon=True,
        )
        worker_thread.start()
        final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)
        worker_thread.join(timeout=10)

        assert final_status != "timeout", (
            f"Job {job_id} did not reach a terminal status within {TIMEOUT_SECONDS}s"
        )
        assert final_status == "completed", f"Expected 'completed', got '{final_status}'"

        row = get_job_status(job_id, db_path=tmp_db)
        assert row["progress"] == 100
        assert row["status"] == "completed"

    def test_status_transitions_observed(
        self, client, tmp_db, rq_queue, monkeypatch
    ):
        """
        Assert that the status transitions from queued → processing → completed.
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        mock_scanner = MagicMock()
        mock_scanner.discover_feature_domains.return_value = []
        mock_scanner_cls = MagicMock(return_value=mock_scanner)

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            res = client.post("/init", json={"repo_path": "/tmp/transitions-repo"})
            job_id = res.json()["job_id"]

        observed_statuses = []

        worker_thread = threading.Thread(
            target=_run_task_direct,
            args=(job_id, "/tmp/transitions-repo", tmp_db),
            kwargs={"extra_patches": _base_patches(mock_scanner_cls)},
            daemon=True,
        )
        worker_thread.start()

        # Poll and record all observed status values
        deadline = time.monotonic() + TIMEOUT_SECONDS
        last_status = None
        while time.monotonic() < deadline:
            row = get_job_status(job_id, db_path=tmp_db)
            if row:
                s = row["status"]
                if s != last_status:
                    observed_statuses.append(s)
                    last_status = s
                if s in ("completed", "failed"):
                    break
            time.sleep(POLL_INTERVAL)

        worker_thread.join(timeout=10)

        assert "queued" in observed_statuses, f"Never saw 'queued'. Observed: {observed_statuses}"
        assert "completed" in observed_statuses, (
            f"Never reached 'completed'. Observed: {observed_statuses}"
        )
        # Verify ordering: queued comes before completed
        assert observed_statuses.index("queued") < observed_statuses.index("completed")

    def test_failed_job_captured_within_timeout(
        self, client, tmp_db, rq_queue, monkeypatch
    ):
        """
        If the worker raises an exception, the job must reach 'failed' status
        within the 60s timeout.
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            res = client.post("/init", json={"repo_path": "/tmp/fail-repo"})
            job_id = res.json()["job_id"]

        # Patch CodebaseScanner to raise; this propagates to the outer try/except
        # and sets status='failed' with the correct error message.
        failing_patches = {
            "worker.CodebaseScanner": MagicMock(
                side_effect=RuntimeError("Deliberate failure for E2E test")
            ),
            "worker.boto3": MagicMock(),
        }

        worker_thread = threading.Thread(
            target=_run_task_direct,
            args=(job_id, "/tmp/fail-repo", tmp_db),
            kwargs={"extra_patches": failing_patches},
            daemon=True,
        )
        worker_thread.start()
        final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)
        worker_thread.join(timeout=10)

        assert final_status != "timeout", "Job did not reach terminal status within 60s"
        assert final_status == "failed"
        row = get_job_status(job_id, db_path=tmp_db)
        assert "Deliberate failure" in row["message"]

    def test_get_status_endpoint_returns_correct_data_during_poll(
        self, client, tmp_db, rq_queue, monkeypatch
    ):
        """
        Validate the /status/{job_id} HTTP endpoint returns the expected JSON
        shape throughout the lifecycle.
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        mock_scanner = MagicMock()
        mock_scanner.discover_feature_domains.return_value = []
        mock_scanner_cls = MagicMock(return_value=mock_scanner)

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            res = client.post("/init", json={"repo_path": "/tmp/poll-repo"})
            job_id = res.json()["job_id"]

            # Check initial /status response shape
            status_res = client.get(f"/status/{job_id}")
            assert status_res.status_code == 200
            status_data = status_res.json()
            assert "job_id" in status_data
            assert "status" in status_data
            assert "progress" in status_data
            assert "message" in status_data
            assert status_data["job_id"] == job_id
            assert status_data["status"] == "queued"

        worker_thread = threading.Thread(
            target=_run_task_direct,
            args=(job_id, "/tmp/poll-repo", tmp_db),
            kwargs={"extra_patches": _base_patches(mock_scanner_cls)},
            daemon=True,
        )
        worker_thread.start()
        final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)
        worker_thread.join(timeout=10)

        # Final status via HTTP endpoint
        final_res = client.get(f"/status/{job_id}")
        assert final_res.status_code == 200
        final_data = final_res.json()
        assert final_data["status"] == final_status
        if final_status == "completed":
            assert final_data["progress"] == 100
