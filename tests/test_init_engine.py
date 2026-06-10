#!/usr/bin/env python3
"""
tests/test_init_engine.py

Unit and integration tests for the refactored init_engine.py.
Covers legacy credential routes AND the new async /init / /status API.

Run with: ../.venv/bin/pytest tests/test_init_engine.py -v
"""

import os
import sys
import stat
import uuid
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pytest
import boto3
import fakeredis
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from init_engine import app, run_code_first_fallback, update_gitignore, is_ollama_online
from db_utils import init_db, get_job_status


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "jobs.db")


# ── Unit Tests: POST /init ────────────────────────────────────────────────────

class TestPostInit:
    def test_returns_200_with_valid_uuid(self, client, tmp_db, monkeypatch):
        """POST /init must return 200 with a valid UUID job_id."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        # Fake Redis so the job is 'enqueued' without a real Redis server
        fake_redis = fakeredis.FakeRedis()
        with patch("init_engine._get_redis_queue") as mock_q:
            mock_queue = MagicMock()
            mock_q.return_value = mock_queue
            res = client.post("/init", json={"repo_path": "/tmp/test-repo"})

        assert res.status_code == 200
        data = res.json()
        assert "job_id" in data
        assert data["status"] == "queued"
        # Verify it is a valid UUID
        parsed = uuid.UUID(data["job_id"])
        assert str(parsed) == data["job_id"]

    def test_job_enqueued_in_redis(self, client, tmp_db, monkeypatch):
        """POST /init must call queue.enqueue with the correct task."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        with patch("init_engine._get_redis_queue") as mock_q:
            mock_queue = MagicMock()
            mock_q.return_value = mock_queue
            res = client.post("/init", json={"repo_path": "/some/repo"})

        assert res.status_code == 200
        mock_queue.enqueue.assert_called_once()
        call_args = mock_queue.enqueue.call_args
        # First positional arg should be the task string
        assert call_args[0][0] == "worker.run_codebase_scan"

    def test_returns_400_for_empty_repo_path(self, client, tmp_db, monkeypatch):
        """POST /init with an empty repo_path must return 400."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        res = client.post("/init", json={"repo_path": "   "})
        assert res.status_code == 400

    def test_job_written_to_sqlite(self, client, tmp_db, monkeypatch):
        """After POST /init, the job must exist in SQLite with status 'queued'."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        with patch("init_engine._get_redis_queue") as mock_q:
            mock_queue = MagicMock()
            mock_q.return_value = mock_queue
            res = client.post("/init", json={"repo_path": "/my/repo"})

        job_id = res.json()["job_id"]
        row = get_job_status(job_id, db_path=tmp_db)
        assert row is not None
        assert row["status"] == "queued"
        assert row["repo_path"] == "/my/repo"

    def test_redis_unavailable_sets_failed_status(self, client, tmp_db, monkeypatch):
        """If Redis is unavailable, the job should be marked as failed gracefully."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        with patch("init_engine._get_redis_queue", return_value=None):
            res = client.post("/init", json={"repo_path": "/offline/repo"})

        assert res.status_code == 200  # HTTP still succeeds
        job_id = res.json()["job_id"]
        row = get_job_status(job_id, db_path=tmp_db)
        assert row["status"] == "failed"
        assert "Redis" in row["message"]


# ── Unit Tests: GET /status/{job_id} ─────────────────────────────────────────

class TestGetStatus:
    def test_returns_job_data(self, client, tmp_db, monkeypatch):
        """GET /status/{job_id} must return the job data from SQLite."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        # Seed a row directly
        init_db(tmp_db)
        from db_utils import create_job, update_job_status
        job_id = str(uuid.uuid4())
        create_job(job_id, "/test/repo", db_path=tmp_db)
        update_job_status(job_id, "processing", 45, "Parsing routes", db_path=tmp_db)

        res = client.get(f"/status/{job_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["job_id"] == job_id
        assert data["status"] == "processing"
        assert data["progress"] == 45
        assert data["message"] == "Parsing routes"

    def test_returns_404_for_unknown_job(self, client, tmp_db, monkeypatch):
        """GET /status with an unknown job_id must return 404."""
        monkeypatch.setenv("CCKB_DB_PATH", tmp_db)
        res = client.get("/status/non-existent-uuid")
        assert res.status_code == 404


# ── Legacy Unit Test: run_code_first_fallback ─────────────────────────────────

class TestUnitInitEngine(unittest.TestCase):
    @patch("init_engine.Repo")
    @patch("llm_client.get_llm")
    def test_run_code_first_fallback_langgraph(self, mock_get_llm, mock_repo_class):
        mock_repo = MagicMock()
        mock_repo_class.return_value = mock_repo
        mock_commit = MagicMock()
        mock_commit.hexsha = "abc123xyz"
        mock_commit.author.name = "Test Author"
        mock_commit.message = "Initial commit"
        mock_diff = MagicMock()
        mock_diff.a_path = "helper.py"
        mock_diff.b_path = "helper.py"
        mock_diff.diff = b"def sum(a, b):\n    return a + b\n"
        mock_commit.parents = [MagicMock()]
        mock_commit.parents[0].diff.return_value = [mock_diff]
        mock_repo.iter_commits.return_value = [mock_commit]
        mock_llm_instance = MagicMock()
        mock_get_llm.return_value = mock_llm_instance
        mock_llm_instance.invoke.side_effect = [
            "Summary: Added sum helper.",
            "# Project Constitution (Hot Memory)\n\n## 1. Architectural Design & Project Structure\n- Modular python helpers.\n\n## 2. Key Styling Patterns\n- None\n\n## 3. Implementation Conventions\n- Uses sum helper.",
        ]
        with patch("init_engine.is_ollama_online", return_value=True):
            result = run_code_first_fallback("/mock/path")

        self.assertIn("# Project Constitution (Hot Memory)", result)
        self.assertIn("Modular python helpers", result)
        mock_repo.iter_commits.assert_called_once_with(max_count=50)


# ── Legacy Integration Tests ──────────────────────────────────────────────────

class TestIntegrationInitEngine(unittest.TestCase):
    def setUp(self):
        self.gitignore_backup = None
        if os.path.exists(".gitignore"):
            with open(".gitignore", "r") as f:
                self.gitignore_backup = f.read()
        self.cckb_backup = None
        if os.path.exists(".cckb"):
            self.cckb_backup = {}
            env_path = ".cckb/.env"
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            self.cckb_backup[k] = v
            shutil.rmtree(".cckb")
        self.client = TestClient(app)
        self.shutdown_patcher = patch("init_engine.shutdown_server")
        self.mock_shutdown = self.shutdown_patcher.start()

    def tearDown(self):
        self.shutdown_patcher.stop()
        if os.path.exists(".cckb"):
            shutil.rmtree(".cckb")
        if self.cckb_backup is not None:
            os.makedirs(".cckb", exist_ok=True)
            os.chmod(".cckb", 0o700)
            env_path = ".cckb/.env"
            with open(env_path, "w") as f:
                for k, v in self.cckb_backup.items():
                    f.write(f"{k}={v}\n")
            os.chmod(env_path, 0o600)
        if self.gitignore_backup is not None:
            with open(".gitignore", "w") as f:
                f.write(self.gitignore_backup)
        elif os.path.exists(".gitignore"):
            os.remove(".gitignore")

    def test_submit_credentials_flow(self):
        response = self.client.post(
            "/submit",
            data={"jira_api_key": "test_jira_token_123", "notion_api_key": "test_notion_token_456"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Credentials saved", response.text)
        env_path = ".cckb/.env"
        self.assertTrue(os.path.exists(env_path))
        with open(env_path, "r") as f:
            lines = f.read().splitlines()
        self.assertIn("JIRA_API_KEY=test_jira_token_123", lines)
        self.assertIn("NOTION_API_KEY=test_notion_token_456", lines)
        mode = os.stat(env_path).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        cckb_mode = os.stat(".cckb").st_mode
        self.assertEqual(stat.S_IMODE(cckb_mode), 0o700)
        self.assertTrue(os.path.exists(".gitignore"))
        with open(".gitignore", "r") as f:
            gitignore_content = f.read()
        self.assertIn(".cckb/.env", gitignore_content)


# ── Legacy E2E Test ───────────────────────────────────────────────────────────

class TestE2EInitEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        from git import Repo
        self.repo = Repo.init(self.test_dir)
        with self.repo.config_writer() as cw:
            cw.set_value("user", "name", "Test User")
            cw.set_value("user", "email", "test@example.com")
        self.dummy_file = os.path.join(self.test_dir, "calculator.py")
        with open(self.dummy_file, "w") as f:
            f.write("def add(x, y):\n    return x + y\n")
        self.repo.index.add(["calculator.py"])
        self.repo.index.commit("Initial commit")
        with open(self.dummy_file, "a") as f:
            f.write("\ndef subtract(x, y):\n    return x - y\n")
        self.repo.index.add(["calculator.py"])
        self.repo.index.commit("Add subtract")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_e2e_fallback_minio_upload(self):
        final_markdown = run_code_first_fallback(self.test_dir)
        self.assertIn("# Project Constitution (Hot Memory)", final_markdown)
        self.assertIn("Architectural Design", final_markdown)
        self.assertIn("Styling Patterns", final_markdown)
        self.assertIn("Implementation Conventions", final_markdown)
        try:
            s3_client = boto3.client(
                "s3",
                endpoint_url="http://localhost:9000",
                aws_access_key_id="minioadmin",
                aws_secret_access_key="minioadmin123",
                region_name="us-east-1",
            )
            s3_client.put_object(
                Bucket="cckb-data",
                Key="hot_memory.md",
                Body=final_markdown.encode("utf-8"),
                ContentType="text/markdown",
            )
            download_path = os.path.join(self.test_dir, "downloaded.md")
            s3_client.download_file("cckb-data", "hot_memory.md", download_path)
            with open(download_path) as f:
                self.assertEqual(f.read(), final_markdown)
        except Exception as e:
            self.fail(f"MinIO storage validation failed: {e}")
