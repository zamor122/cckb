#!/usr/bin/env python3
"""
tests/test_worker.py

Unit and integration tests for worker.py.
Tests bypass the Redis queue entirely and call run_codebase_scan() directly
with mocked heavy I/O (Ollama, MinIO, scanner).

Run with: ../.venv/bin/pytest tests/test_worker.py -v
"""

import os
import sys
import uuid
import tempfile
import shutil
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from db_utils import init_db, create_job, get_job_status, update_job_status


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Return a fresh temp SQLite DB path for each test."""
    return str(tmp_path / "jobs.db")


@pytest.fixture
def job(tmp_db):
    """Seed a queued job and return (job_id, repo_path, db_path)."""
    init_db(tmp_db)
    job_id = str(uuid.uuid4())
    create_job(job_id, "/tmp/fake-repo", db_path=tmp_db)
    return job_id, "/tmp/fake-repo", tmp_db


# ── Unit Tests ─────────────────────────────────────────────────────────────────

class TestRunCodebaseScanSuccess:
    """run_codebase_scan reaches 'completed' when all steps succeed."""

    def test_status_transitions_to_completed(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        # Mock heavy I/O
        mock_constitution = "# Project Constitution (Hot Memory)\n\nstub"
        with (
            patch("worker.run_code_first_fallback", return_value=mock_constitution),
            patch("worker.boto3") as mock_boto,
            patch("worker.CodebaseScanner") as mock_scanner_cls,
            patch("worker.install_hook"),
        ):
            # Minimal scanner stub
            mock_scanner = MagicMock()
            mock_scanner_cls.return_value = mock_scanner
            mock_scanner.discover_feature_domains.return_value = [
                {
                    "spec_id": "AUTH",
                    "display_name": "Authentication",
                    "entry_files": ["auth.py"],
                    "related_files": [],
                    "framework": "fastapi",
                }
            ]
            mock_scanner.generate_spec.return_value = ("# Auth Spec\n", {"spec_id": "AUTH"})
            mock_scanner.upload_spec.return_value = True

            # Mock boto3 S3 client
            mock_boto.client.return_value = MagicMock()

            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert row["status"] == "completed"
        assert row["progress"] == 100

    def test_progress_reaches_100(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner") as mock_scanner_cls,
            patch("worker.install_hook"),
        ):
            mock_scanner = MagicMock()
            mock_scanner_cls.return_value = mock_scanner
            mock_scanner.discover_feature_domains.return_value = []  # zero domains
            mock_scanner.generate_spec.return_value = ("", {})
            mock_scanner.upload_spec.return_value = True

            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert row["progress"] == 100

    def test_message_mentions_spec_count(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner") as mock_scanner_cls,
            patch("worker.install_hook"),
        ):
            mock_scanner = MagicMock()
            mock_scanner_cls.return_value = mock_scanner
            # Return 3 domains so message says "3 feature spec(s)"
            mock_scanner.discover_feature_domains.return_value = [
                {"spec_id": "A", "display_name": "A", "entry_files": ["a.py"], "related_files": [], "framework": "python"},
                {"spec_id": "B", "display_name": "B", "entry_files": ["b.py"], "related_files": [], "framework": "python"},
                {"spec_id": "C", "display_name": "C", "entry_files": ["c.py"], "related_files": [], "framework": "python"},
            ]
            mock_scanner.generate_spec.return_value = ("spec", {"spec_id": "A"})
            mock_scanner.upload_spec.return_value = True

            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert "3 feature spec" in row["message"]

    def test_hook_installed(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner") as mock_scanner_cls,
            patch("worker.install_hook") as mock_hook,
        ):
            mock_scanner = MagicMock()
            mock_scanner_cls.return_value = mock_scanner
            mock_scanner.discover_feature_domains.return_value = []
            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        mock_hook.assert_called_once()


class TestRunCodebaseScanFailure:
    """run_codebase_scan transitions to 'failed' on exception."""

    def test_status_transitions_to_failed(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner", side_effect=RuntimeError("Git error")),
        ):
            with pytest.raises(RuntimeError):
                worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert row["status"] == "failed"
        assert "Git error" in row["message"]

    def test_scanner_exception_marks_failed(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner", side_effect=ValueError("Bad path")),
        ):
            with pytest.raises(ValueError):
                worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert row["status"] == "failed"

    def test_error_message_captured_in_db(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        with (
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner", side_effect=Exception("Unique error XYZ")),
        ):
            with pytest.raises(Exception):
                worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        row = get_job_status(job_id, db_path=db_path)
        assert "Unique error XYZ" in row["message"]


class TestRunCodebaseScanIntegration:
    """Integration: real SQLite + mocked LLM/scanner, verifies DB transitions."""

    def test_status_progresses_through_stages(self, job, monkeypatch):
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        statuses_observed = []

        original_update = update_job_status

        def tracking_update(jid, status, progress, message, db_path=None):
            statuses_observed.append(status)
            original_update(jid, status, progress, message, db_path=db_path)

        with (
            patch("worker.update_job_status", side_effect=tracking_update),
            patch("worker.run_code_first_fallback", return_value="# Constitution"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner") as mock_scanner_cls,
            patch("worker.install_hook"),
        ):
            mock_scanner = MagicMock()
            mock_scanner_cls.return_value = mock_scanner
            mock_scanner.discover_feature_domains.return_value = []
            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        # Must start with 'processing' and end with 'completed'
        assert statuses_observed[0] == "processing"
        assert statuses_observed[-1] == "completed"

    def test_progress_callback_fired_during_scan(self, job, monkeypatch):
        """Scanner progress_callback must be injected and callable."""
        job_id, repo_path, db_path = job
        monkeypatch.setenv("CCKB_DB_PATH", db_path)

        captured_callbacks = []

        class SpyScanner:
            def __init__(self, repo_path, max_domains=10, progress_callback=None):
                self.progress_callback = progress_callback
                captured_callbacks.append(progress_callback)

            def discover_feature_domains(self):
                if self.progress_callback:
                    self.progress_callback(20, "Test progress")
                return []

            def generate_spec(self, domain, use_llm=True):
                return "", {}

            def upload_spec(self, *a):
                return True

        with (
            patch("worker.run_code_first_fallback", return_value="# C"),
            patch("worker.boto3"),
            patch("worker.CodebaseScanner", new=SpyScanner),
            patch("worker.install_hook"),
        ):
            worker.run_codebase_scan(job_id, repo_path, db_path=db_path)

        assert len(captured_callbacks) == 1
        assert callable(captured_callbacks[0])
