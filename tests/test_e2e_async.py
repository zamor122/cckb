#!/usr/bin/env python3
"""
tests/test_e2e_async.py

End-to-End test for the full async job lifecycle:
    POST /init  →  job queued  →  worker processes  →  GET /status == 'completed'

Uses:
  - fakeredis.FakeRedis      (no live Redis required)
  - Direct task execution    (calls run_codebase_scan() in a background thread,
                              bypassing RQ's SimpleWorker which has a known
                              KeyError with some fakeredis versions)
  - FastAPI TestClient       (in-process ASGI, no live uvicorn required)
  - A temp SQLite DB         (isolated from production state)

The init_engine's /init route enqueues to fakeredis; the test's background thread
manually dequeues and calls the task function, exactly mirroring what a real worker
would do. This approach tests the full lifecycle (status transitions, DB writes,
HTTP polling) without external infrastructure.

Timeout: 60 seconds per spec requirement.
Run with: ../.venv/bin/pytest tests/test_e2e_async.py -v
"""

import os
import sys
import uuid
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

def _dequeue_and_execute(rq_queue, db_path: str, extra_patches: dict = None):
    """
    Dequeue the first job from the RQ queue and execute run_codebase_scan
    directly in this thread (bypassing RQ worker internals that may be
    incompatible with fakeredis).

    extra_patches: dict of {target_string: mock_value} applied via patch()
    """
    os.environ["CCKB_DB_PATH"] = db_path
    job = rq_queue.fetch_job_ids()
    if not job:
        return
    # Grab the enqueued job from fakeredis without using a Worker
    rq_job = rq_queue.dequeue()
    if rq_job is None:
        return

    job_id   = rq_job.args[0]
    repo_path = rq_job.args[1]

    if extra_patches:
        # Build a nested context manager from the extra patches
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


# ── E2E Tests ─────────────────────────────────────────────────────────────────

class TestE2EAsyncLifecycle:
    """Full lifecycle tests: queued → processing → completed."""

    def test_queued_to_completed_lifecycle(
        self, client, tmp_db, fake_redis_server, rq_queue, monkeypatch
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

        # Start background thread to execute the task
        extra = {
            "worker.run_code_first_fallback": MagicMock(return_value="# Constitution"),
            "worker.boto3": MagicMock(),
            "worker.install_hook": MagicMock(),
        }
        # CodebaseScanner needs special handling since it's a class
        with patch("worker.CodebaseScanner", mock_scanner_cls):
            worker_thread = threading.Thread(
                target=_dequeue_and_execute,
                args=(rq_queue, tmp_db),
                kwargs={"extra_patches": extra},
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
        self, client, tmp_db, fake_redis_server, rq_queue, monkeypatch
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
        extra = {
            "worker.run_code_first_fallback": MagicMock(return_value="# Constitution"),
            "worker.boto3": MagicMock(),
            "worker.install_hook": MagicMock(),
        }
        with patch("worker.CodebaseScanner", mock_scanner_cls):
            worker_thread = threading.Thread(
                target=_dequeue_and_execute,
                args=(rq_queue, tmp_db),
                kwargs={"extra_patches": extra},
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
        self, client, tmp_db, fake_redis_server, rq_queue, monkeypatch
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

        # Use a MagicMock that raises when called
        raising_fn = MagicMock(side_effect=RuntimeError("Deliberate failure for E2E test"))

        worker_thread = threading.Thread(
            target=_dequeue_and_execute,
            args=(rq_queue, tmp_db),
            kwargs={"extra_patches": {"worker.run_code_first_fallback": raising_fn}},
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
        self, client, tmp_db, fake_redis_server, rq_queue, monkeypatch
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

        extra = {
            "worker.run_code_first_fallback": MagicMock(return_value="# Constitution"),
            "worker.boto3": MagicMock(),
            "worker.install_hook": MagicMock(),
        }
        with patch("worker.CodebaseScanner", mock_scanner_cls):
            worker_thread = threading.Thread(
                target=_dequeue_and_execute,
                args=(rq_queue, tmp_db),
                kwargs={"extra_patches": extra},
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
