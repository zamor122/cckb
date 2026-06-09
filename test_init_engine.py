import os
import stat
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

import pytest
import boto3
from fastapi.testclient import TestClient

from init_engine import app, run_code_first_fallback, update_gitignore, is_ollama_online

# 1. Unit Tests

class TestUnitInitEngine(unittest.TestCase):
    @patch('init_engine.Repo')
    @patch('init_engine.OllamaLLM')
    def test_run_code_first_fallback_langgraph(self, mock_ollama_class, mock_repo_class):
        # Mock git commit objects
        mock_repo = MagicMock()
        mock_repo_class.return_value = mock_repo
        
        # Create a mock commit
        mock_commit = MagicMock()
        mock_commit.hexsha = "abc123xyz"
        mock_commit.author.name = "Test Author"
        mock_commit.message = "Initial commit: Add sum helper function"
        
        # Mock diff against parent
        mock_diff = MagicMock()
        mock_diff.a_path = "helper.py"
        mock_diff.b_path = "helper.py"
        mock_diff.diff = b"def sum(a, b):\n    return a + b\n"
        
        mock_commit.parents = [MagicMock()]
        mock_commit.parents[0].diff.return_value = [mock_diff]
        
        # iter_commits returns our mock commit
        mock_repo.iter_commits.return_value = [mock_commit]
        
        # Mock Ollama response
        mock_llm_instance = MagicMock()
        mock_ollama_class.return_value = mock_llm_instance
        # The first call is for batch summary, the second is for synthesis
        mock_llm_instance.invoke.side_effect = [
            "Summary of changes: Added sum helper function in helper.py.",
            "# Project Constitution (Hot Memory)\n\n## 1. Architectural Design & Project Structure\n- Modular python helper functions.\n\n## 2. Key Styling Patterns\n- None\n\n## 3. Implementation Conventions\n- Uses sum helper."
        ]
        
        # Patch is_ollama_online to return True so the graph executes the LLM nodes
        with patch('init_engine.is_ollama_online', return_value=True):
            markdown_result = run_code_first_fallback("/mock/path")
            
        # Verify result contains the expected content from the mock synthesis
        self.assertIn("# Project Constitution (Hot Memory)", markdown_result)
        self.assertIn("Modular python helper functions", markdown_result)
        
        # Verify iter_commits was called once with max_count=50
        mock_repo.iter_commits.assert_called_once_with(max_count=50)

# 2. Integration Tests

class TestIntegrationInitEngine(unittest.TestCase):
    def setUp(self):
        # Backup the existing .gitignore if it exists
        self.gitignore_backup = None
        if os.path.exists(".gitignore"):
            with open(".gitignore", "r") as f:
                self.gitignore_backup = f.read()
        
        # Remove active .cckb if any
        self.cckb_backup = None
        if os.path.exists(".cckb"):
            # Move to temp or delete (we'll just remove it and restore later if needed,
            # but to be safe we backup the credentials)
            self.cckb_backup = {}
            env_path = ".cckb/.env"
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            self.cckb_backup[k] = v
            shutil.rmtree(".cckb")
            
        # Setup test client
        self.client = TestClient(app)
        
        # Patch shutdown_server to avoid terminating the test process
        self.shutdown_patcher = patch('init_engine.shutdown_server')
        self.mock_shutdown = self.shutdown_patcher.start()

    def tearDown(self):
        # Stop the shutdown server patcher
        self.shutdown_patcher.stop()
        
        # Clean up test creations
        if os.path.exists(".cckb"):
            shutil.rmtree(".cckb")
            
        # Restore backups
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
        # Send credentials submission via TestClient
        response = self.client.post(
            "/submit",
            data={"jira_api_key": "test_jira_token_123", "notion_api_key": "test_notion_token_456"}
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertIn("Credentials saved", response.text)
        
        # Verify .cckb/.env existence and contents
        env_path = ".cckb/.env"
        self.assertTrue(os.path.exists(env_path))
        
        with open(env_path, "r") as f:
            lines = f.read().splitlines()
        
        self.assertIn("JIRA_API_KEY=test_jira_token_123", lines)
        self.assertIn("NOTION_API_KEY=test_notion_token_456", lines)
        
        # Assert restrictive file permissions
        mode = os.stat(env_path).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        
        cckb_mode = os.stat(".cckb").st_mode
        self.assertEqual(stat.S_IMODE(cckb_mode), 0o700)
        
        # Assert .gitignore contains .cckb/.env
        self.assertTrue(os.path.exists(".gitignore"))
        with open(".gitignore", "r") as f:
            gitignore_content = f.read()
        self.assertIn(".cckb/.env", gitignore_content)

# 3. End-to-End Tests

class TestE2EInitEngine(unittest.TestCase):
    def setUp(self):
        # Create temp sandbox directory for git repository
        self.test_dir = tempfile.mkdtemp()
        
        # Initialize Git repo in the sandbox
        from git import Repo
        self.repo = Repo.init(self.test_dir)
        
        # Set dummy user details
        with self.repo.config_writer() as cw:
            cw.set_value("user", "name", "Test User")
            cw.set_value("user", "email", "test@example.com")
            
        # Create some dummy commits
        self.dummy_file = os.path.join(self.test_dir, "calculator.py")
        with open(self.dummy_file, "w") as f:
            f.write("# Calculator project\n\ndef add(x, y):\n    return x + y\n")
        self.repo.index.add(["calculator.py"])
        self.repo.index.commit("Initial commit: Add calculator module with add function")
        
        with open(self.dummy_file, "a") as f:
            f.write("\ndef subtract(x, y):\n    return x - y\n")
        self.repo.index.add(["calculator.py"])
        self.repo.index.commit("Feature: Add subtract function to calculator")

    def tearDown(self):
        # Remove sandbox directory
        shutil.rmtree(self.test_dir)

    def test_e2e_fallback_minio_upload(self):
        # If Ollama is running offline we test the offline fallback.
        # Run code first fallback directly targeting our temporary repository path.
        final_markdown = run_code_first_fallback(self.test_dir)
        
        # Assert markdown was generated and has expected structure
        self.assertIn("# Project Constitution (Hot Memory)", final_markdown)
        self.assertIn("Architectural Design", final_markdown)
        self.assertIn("Styling Patterns", final_markdown)
        self.assertIn("Implementation Conventions", final_markdown)
        
        # Test direct uploading to MinIO using boto3 (assuming local MinIO container is running)
        minio_url = 'http://localhost:9000'
        bucket_name = 'cckb-data'
        key_name = 'hot_memory.md'
        
        try:
            s3_client = boto3.client(
                's3',
                endpoint_url=minio_url,
                aws_access_key_id='minioadmin',
                aws_secret_access_key='minioadmin123',
                region_name='us-east-1'
            )
            
            # Upload
            s3_client.put_object(
                Bucket=bucket_name,
                Key=key_name,
                Body=final_markdown.encode('utf-8'),
                ContentType='text/markdown'
            )
            
            # Download and verify
            download_path = os.path.join(self.test_dir, "downloaded_constitution.md")
            s3_client.download_file(bucket_name, key_name, download_path)
            
            with open(download_path, "r") as f:
                downloaded_content = f.read()
                
            self.assertEqual(downloaded_content, final_markdown)
            print("E2E Storage & Fallback loop succeeded: markdown downloaded matches uploaded!")
            
        except Exception as e:
            # If MinIO is not running or fails, we log it, but since we verified Epic 1
            # is running, we expect this to succeed in our environment.
            self.fail(f"MinIO storage validation failed: {str(e)}")
