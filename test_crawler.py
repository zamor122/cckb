import os
import stat
import tempfile
import shutil
import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import boto3
import botocore.exceptions

from crawler import detect_renames, detect_renames_fallback, process_post_commit, get_s3_client, install_hook

# 1. Unit Tests

class TestUnitCrawler(unittest.TestCase):
    def test_ast_rename_detection(self):
        old_source = """
class Authenticator:
    def verify_credentials(self, username, password):
        if username == "admin" and password == "secret":
            return True
        return False
"""
        # Renamed verify_credentials to check_login, body is identical
        new_source = """
class Authenticator:
    def check_login(self, username, password):
        if username == "admin" and password == "secret":
            return True
        return False
"""
        renames = detect_renames(old_source, new_source)
        self.assertEqual(renames, {"verify_credentials": "check_login"})

    def test_regex_fallback_rename_detection(self):
        old_source = "def old_helper():\n    pass"
        new_source = "def new_helper():\n    pass"
        
        # Test direct regex fallback
        renames = detect_renames_fallback(old_source, new_source)
        self.assertEqual(renames, {"old_helper": "new_helper"})

# 2. Integration Tests

class TestIntegrationCrawler(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_dir)
        
        # Write temporary .cckb/.env
        self.cckb_dir = os.path.join(self.temp_dir, ".cckb")
        os.makedirs(self.cckb_dir)
        self.env_file = os.path.join(self.cckb_dir, ".env")
        with open(self.env_file, "w") as f:
            f.write("MINIO_ROOT_USER=minioadmin\n")
            f.write("MINIO_ROOT_PASSWORD=minioadmin123\n")
            
        # Create dummy service.py
        with open("service.py", "w") as f:
            f.write("def login_user():\n    pass\n")
            
        # Mock MinIO client and s3 calls
        self.s3_mock = MagicMock()
        
    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.temp_dir)

    @patch('crawler.get_s3_client')
    @patch('crawler.load_cckb_env')
    @patch('crawler.subprocess.check_output')
    def test_post_commit_integration(self, mock_check_output, mock_load_env, mock_get_s3):
        mock_load_env.return_value = {
            "MINIO_ROOT_USER": "minioadmin",
            "MINIO_ROOT_PASSWORD": "minioadmin123"
        }
        mock_get_s3.return_value = self.s3_mock
        
        # Mock git calls: diff-tree lists 'service.py'
        # git show HEAD:service.py -> new source
        # git show HEAD~1:service.py -> old source
        def git_mock_run(cmd, **kwargs):
            if "diff-tree" in cmd:
                return b"service.py\n"
            elif "HEAD~1:service.py" in cmd[2]:
                return b"def authenticate_user():\n    return True\n"
            elif "HEAD:service.py" in cmd[2]:
                return b"def login_user():\n    return True\n"
            elif "rev-parse" in cmd:
                return b"HEAD_SHA\n"
            return b""
            
        mock_check_output.side_effect = git_mock_run
        
        # Mock S3 list_objects_v2 and get_object
        self.s3_mock.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "metadata/spec_auth.meta.json"}
            ]
        }
        
        mock_meta_json = {
            "spec_id": "auth_spec",
            "dependencies": [
                {"type": "function", "name": "authenticate_user", "file": "service.py"}
            ]
        }
        
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(mock_meta_json).encode('utf-8')
        self.s3_mock.get_object.return_value = {"Body": mock_body}
        
        # Run process
        process_post_commit()
        
        # Assert S3 put_object was called to update dependency name to 'login_user'
        self.s3_mock.put_object.assert_called_once()
        call_args = self.s3_mock.put_object.call_args[1]
        self.assertEqual(call_args['Bucket'], 'cckb-data')
        self.assertEqual(call_args['Key'], 'metadata/spec_auth.meta.json')
        
        updated_meta = json.loads(call_args['Body'].decode('utf-8'))
        self.assertEqual(updated_meta['dependencies'][0]['name'], 'login_user')

# 3. End-to-End Tests

class TestE2ECrawler(unittest.TestCase):
    def setUp(self):
        # Create temp sandbox directory for git repository
        self.test_dir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.test_dir)
        
        # Initialize Git repo
        from git import Repo
        self.repo = Repo.init(self.test_dir)
        
        # Set dummy user details
        with self.repo.config_writer() as cw:
            cw.set_value("user", "name", "Test User")
            cw.set_value("user", "email", "test@example.com")
            
        # Write .cckb/.env
        os.makedirs(".cckb", exist_ok=True)
        with open(".cckb/.env", "w") as f:
            f.write("MINIO_ROOT_USER=minioadmin\n")
            f.write("MINIO_ROOT_PASSWORD=minioadmin123\n")
            
        # Create a Python file in sandbox
        self.python_file = "utils.py"
        with open(self.python_file, "w") as f:
            f.write("def perform_auth():\n    return 'auth_success'\n")
            
        # Commit it
        self.repo.index.add([self.python_file])
        self.repo.index.commit("Initial commit: Add perform_auth function")
        
        # Pre-seed a dependency map in MinIO metadata/ directory
        self.s3 = boto3.client(
            's3',
            endpoint_url='http://localhost:9000',
            aws_access_key_id='minioadmin',
            aws_secret_access_key='minioadmin123',
            region_name='us-east-1'
        )
        
        # Put metadata spec in MinIO
        self.meta_key = "metadata/spec_utils.meta.json"
        self.spec_content = {
            "spec_id": "auth_spec",
            "dependencies": [
                {"type": "function", "name": "perform_auth", "file": "utils.py"}
            ]
        }
        
        self.s3.put_object(
            Bucket='cckb-data',
            Key=self.meta_key,
            Body=json.dumps(self.spec_content).encode('utf-8'),
            ContentType='application/json'
        )

    def tearDown(self):
        # Restore cwd and cleanup temp sandbox
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.test_dir)

    def test_e2e_hook_self_healing(self):
        # 1. Install post-commit hook in the sandbox repo
        success = install_hook()
        self.assertTrue(success)
        
        hook_path = ".git/hooks/post-commit"
        self.assertTrue(os.path.exists(hook_path))
        mode = os.stat(hook_path).st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR)) # Assert executable
        
        # 2. Modify python file to rename perform_auth to execute_auth
        with open(self.python_file, "w") as f:
            f.write("def execute_auth():\n    return 'auth_success'\n")
            
        # Commit the rename
        self.repo.index.add([self.python_file])
        self.repo.index.commit("Refactor: Rename perform_auth to execute_auth")
        
        # 3. Simulate Git trigger by calling process_post_commit synchronously
        # (Since nohup background process run by hook executes in background, calling it synchronously in test ensures
        # we can assert result immediately)
        process_post_commit()
        
        # 4. Assert MinIO dependency map was self-healed
        obj = self.s3.get_object(Bucket='cckb-data', Key=self.meta_key)
        updated_spec = json.loads(obj['Body'].read().decode('utf-8'))
        
        dep_name = updated_spec['dependencies'][0]['name']
        self.assertEqual(dep_name, "execute_auth")
        print("E2E Crawler validation succeeded: metadata dependency self-healed on MinIO!")
