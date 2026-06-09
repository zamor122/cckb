#!/usr/bin/env python3
"""
tests/test_db_utils.py — Unit tests for db_utils.py

Run with: ../.venv/bin/pytest tests/test_db_utils.py -v
"""

import os
import sys
import tempfile
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_utils import init_db, create_job, update_job_status, get_job_status


@pytest.fixture
def tmp_db(tmp_path):
    """Return a fresh temp SQLite DB path for each test."""
    return str(tmp_path / "test_jobs.db")


class TestInitDb:
    def test_creates_jobs_table(self, tmp_db):
        init_db(tmp_db)
        conn = sqlite3.connect(tmp_db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "jobs" in tables

    def test_idempotent(self, tmp_db):
        """Calling init_db twice must not raise."""
        init_db(tmp_db)
        init_db(tmp_db)  # second call — should not raise

    def test_jobs_table_has_expected_columns(self, tmp_db):
        init_db(tmp_db)
        conn = sqlite3.connect(tmp_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        conn.close()
        for expected in ("job_id", "repo_path", "status", "progress", "message", "created_at"):
            assert expected in cols


class TestCreateJob:
    def test_insert_queued_row(self, tmp_db):
        init_db(tmp_db)
        create_job("job-001", "/repos/myapp", db_path=tmp_db)
        row = get_job_status("job-001", db_path=tmp_db)
        assert row is not None
        assert row["job_id"] == "job-001"
        assert row["repo_path"] == "/repos/myapp"
        assert row["status"] == "queued"
        assert row["progress"] == 0

    def test_message_initialised(self, tmp_db):
        init_db(tmp_db)
        create_job("job-002", "/repos/x", db_path=tmp_db)
        row = get_job_status("job-002", db_path=tmp_db)
        assert row["message"] == "Job queued"


class TestUpdateJobStatus:
    def test_update_processing(self, tmp_db):
        init_db(tmp_db)
        create_job("job-u1", "/repos/a", db_path=tmp_db)
        update_job_status("job-u1", "processing", 42, "Doing stuff", db_path=tmp_db)
        row = get_job_status("job-u1", db_path=tmp_db)
        assert row["status"] == "processing"
        assert row["progress"] == 42
        assert row["message"] == "Doing stuff"

    def test_update_completed(self, tmp_db):
        init_db(tmp_db)
        create_job("job-u2", "/repos/b", db_path=tmp_db)
        update_job_status("job-u2", "completed", 100, "Done", db_path=tmp_db)
        row = get_job_status("job-u2", db_path=tmp_db)
        assert row["status"] == "completed"
        assert row["progress"] == 100

    def test_update_failed(self, tmp_db):
        init_db(tmp_db)
        create_job("job-u3", "/repos/c", db_path=tmp_db)
        update_job_status("job-u3", "failed", 0, "Boom", db_path=tmp_db)
        row = get_job_status("job-u3", db_path=tmp_db)
        assert row["status"] == "failed"
        assert row["message"] == "Boom"


class TestGetJobStatus:
    def test_returns_none_for_missing_job(self, tmp_db):
        init_db(tmp_db)
        row = get_job_status("nonexistent", db_path=tmp_db)
        assert row is None

    def test_returns_none_when_db_missing(self, tmp_path):
        """If the DB file doesn't exist yet, return None gracefully."""
        row = get_job_status("any", db_path=str(tmp_path / "no.db"))
        assert row is None

    def test_returns_dict(self, tmp_db):
        init_db(tmp_db)
        create_job("job-g1", "/repos/d", db_path=tmp_db)
        row = get_job_status("job-g1", db_path=tmp_db)
        assert isinstance(row, dict)
