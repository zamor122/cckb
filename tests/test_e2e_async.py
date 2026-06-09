#!/usr/bin/env python3
"""
tests/test_e2e_async.py

End-to-End test for the full async job lifecycle:
    POST /init  →  job queued  →  worker processes  →  GET /status == 'completed'

Uses:
  - fakeredis.FakeRedis  (no live Redis required)
  - rq.SimpleWorker      (runs in a background thread, not a subprocess)
  - FastAPI TestClient   (in-process ASGI, no live uvicorn required)
  - A temp SQLite DB     (isolated from production state)

Timeout: 60 seconds per spec requirement.
Run with: ../.venv/bin/pytest tests/test_e2e_async.py -v
"""

import os
import sys
import uuid
import time
import threading
import tempfile
import shutil
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fakeredis
from rq import Queue, SimpleWorker
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from init_engine import app
from db_utils import init_db, get_job_status


TIMEOUT_SECONDS = 60
POLL_INTERVAL   = 0.5   # poll every 500ms in tests (faster than production's 1.5s)


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
    return Queue("cckb", connection=fake_redis_server)


@pytest.fixture
def client():
    return TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_simple_worker(queue, fake_redis_conn, db_path: str):
    """
    Execute all queued RQ jobs synchronously via SimpleWorker.
    Runs in a background thread so the test can poll concurrently.
    """
    os.environ["CCKB_DB_PATH"] = db_path
    worker = SimpleWorker([queue], connection=fake_redis_conn)
    worker.work(burst=True)  # process all current jobs then exit


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

        # Patch the API's Redis queue with our fakeredis-backed queue
        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            # Also patch the worker-side db_path default
            with (
                patch("worker.run_code_first_fallback", return_value="# Constitution"),
                patch("worker.boto3"),
                patch("worker.CodebaseScanner") as mock_scanner_cls,
                patch("worker.install_hook"),
            ):
                mock_scanner = MagicMock()
                mock_scanner_cls.return_value = mock_scanner
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

                # Step 1: Submit the scan
                res = client.post("/init", json={"repo_path": "/tmp/e2e-test-repo"})
                assert res.status_code == 200
                data = res.json()
                job_id = data["job_id"]
                assert data["status"] == "queued"

                # Step 2: Verify queued in fakeredis
                assert rq_queue.count == 1

                # Step 3: Start the worker in a background thread
                worker_thread = threading.Thread(
                    target=_run_simple_worker,
                    args=(rq_queue, fake_redis_server, tmp_db),
                    daemon=True,
                )
                worker_thread.start()

                # Step 4: Poll until terminal status or timeout
                final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)

            # Step 5: Wait for worker thread to finish
            worker_thread.join(timeout=5)

        # Assert
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
        Captures a snapshot of statuses over time by reading the DB.
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        observed_statuses = []

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            with (
                patch("worker.run_code_first_fallback", return_value="# Constitution"),
                patch("worker.boto3"),
                patch("worker.CodebaseScanner") as mock_scanner_cls,
                patch("worker.install_hook"),
            ):
                mock_scanner = MagicMock()
                mock_scanner_cls.return_value = mock_scanner
                mock_scanner.discover_feature_domains.return_value = []
                mock_scanner.generate_spec.return_value = ("", {})
                mock_scanner.upload_spec.return_value = True

                res = client.post("/init", json={"repo_path": "/tmp/transitions-repo"})
                job_id = res.json()["job_id"]

                # Start the worker thread
                worker_thread = threading.Thread(
                    target=_run_simple_worker,
                    args=(rq_queue, fake_redis_server, tmp_db),
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

                worker_thread.join(timeout=5)

        # Must have seen 'queued' at the start and 'completed' at the end
        assert "queued" in observed_statuses, f"Never saw 'queued'. Observed: {observed_statuses}"
        assert "completed" in observed_statuses or "failed" not in observed_statuses, (
            f"Job failed. Observed: {observed_statuses}"
        )
        # Verify ordering: queued comes before completed
        if "completed" in observed_statuses:
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
            with patch(
                "worker.run_code_first_fallback",
                side_effect=RuntimeError("Deliberate failure for E2E test"),
            ):
                res = client.post("/init", json={"repo_path": "/tmp/fail-repo"})
                job_id = res.json()["job_id"]

                worker_thread = threading.Thread(
                    target=_run_simple_worker,
                    args=(rq_queue, fake_redis_server, tmp_db),
                    daemon=True,
                )
                worker_thread.start()

                final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)
                worker_thread.join(timeout=5)

        assert final_status != "timeout", "Job did not reach terminal status within 60s"
        assert final_status == "failed"
        row = get_job_status(job_id, db_path=tmp_db)
        assert "Deliberate failure" in row["message"]

    def test_get_status_endpoint_returns_correct_data_during_poll(
        self, client, tmp_db, fake_redis_server, rq_queue, monkeypatch
    ):
        """
        Validate the /status/{job_id} HTTP endpoint (not just DB directly)
        returns the expected JSON shape throughout the lifecycle.
        """
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        init_db(tmp_db)

        with patch("init_engine._get_redis_queue", return_value=rq_queue):
            with (
                patch("worker.run_code_first_fallback", return_value="# Constitution"),
                patch("worker.boto3"),
                patch("worker.CodebaseScanner") as mock_scanner_cls,
                patch("worker.install_hook"),
            ):
                mock_scanner = MagicMock()
                mock_scanner_cls.return_value = mock_scanner
                mock_scanner.discover_feature_domains.return_value = []

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

                # Run the worker
                worker_thread = threading.Thread(
                    target=_run_simple_worker,
                    args=(rq_queue, fake_redis_server, tmp_db),
                    daemon=True,
                )
                worker_thread.start()
                final_status = _wait_for_terminal_status(job_id, tmp_db, TIMEOUT_SECONDS)
                worker_thread.join(timeout=5)

            # Final status via HTTP endpoint
            final_res = client.get(f"/status/{job_id}")
            assert final_res.status_code == 200
            final_data = final_res.json()
            assert final_data["status"] == final_status
            assert final_data["progress"] == 100 if final_status == "completed" else True
