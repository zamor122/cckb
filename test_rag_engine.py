import os
import sqlite3
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

import boto3
import botocore.exceptions

from rag_engine import query_rag_engine, create_graph_workflow, is_ollama_online, analyze_intent_node

# 1. Unit Tests

class TestUnitRAGEngine(unittest.TestCase):
    def setUp(self):
        self.workflow = create_graph_workflow()
        self.app_graph = self.workflow.compile()
        
    @patch('rag_engine.get_s3_client')
    def test_state_transitions_normal(self, mock_get_s3):
        # Mock MinIO client
        s3_mock = MagicMock()
        mock_get_s3.return_value = s3_mock
        
        # Mock available spec documents on MinIO list
        s3_mock.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "features/AUTH-101.md"},
                {"Key": "features/JIRA-202.md"}
            ]
        }
        
        # Mock get_object responses
        mock_meta = {
            "spec_id": "AUTH-101",
            "dependencies": [{"type": "function", "name": "login", "file": "auth.py"}]
        }
        mock_meta_body = MagicMock()
        mock_meta_body.read.return_value = json_dumps_bytes(mock_meta)
        
        mock_doc_body = MagicMock()
        mock_doc_body.read.return_value = b"# Auth Spec Details\nThis describes auth flow."
        
        mock_const_body = MagicMock()
        mock_const_body.read.return_value = b"# Global rules\nIgnore comments."
        
        s3_mock.get_object.side_effect = lambda Bucket, Key: {
            "metadata/AUTH-101.meta.json": {"Body": mock_meta_body},
            "features/AUTH-101.md": {"Body": mock_doc_body},
            "hot_memory.md": {"Body": mock_const_body}
        }.get(Key, {"Body": MagicMock()})
        
        # Invoke graph with patched Ollama offline routing or mock router
        with patch('rag_engine.is_ollama_online', return_value=False):
            # Query maps to AUTH-101 because 'AUTH-101' is in the query text
            result = self.app_graph.invoke({
                "intent": "Implement AUTH-101 SSO auth flow",
                "implicated_features": [],
                "metadata_docs": [],
                "feature_docs": [],
                "global_constitution": "",
                "final_payload": "",
                "clarification_message": None,
                "history": []
            })
            
        self.assertIn("AUTH-101", result['implicated_features'])
        self.assertIn("# Global Constitution", result['final_payload'])
        self.assertIn("Auth Spec Details", result['final_payload'])
        self.assertIn("Codebase Mappings", result['final_payload'])
        self.assertIn("Type: function, Name: login", result['final_payload'])
        self.assertIsNone(result['clarification_message'])

    @patch('rag_engine.get_s3_client')
    def test_state_transitions_clarification(self, mock_get_s3):
        s3_mock = MagicMock()
        mock_get_s3.return_value = s3_mock
        s3_mock.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "features/AUTH-101.md"},
                {"Key": "features/JIRA-202.md"}
            ]
        }
        
        # Test routing node behavior when Ollama LLM requests clarification
        # Mock Ollama output to return clarification needed JSON
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = '{"implicated_features": [], "clarification_needed": true, "clarification_message": "Query is too broad. Please narrow down."}'
        
        with patch('rag_engine.OllamaLLM', return_value=mock_llm):
            with patch('rag_engine.is_ollama_online', return_value=True):
                result = self.app_graph.invoke({
                    "intent": "Explain the whole system in detail",
                    "implicated_features": [],
                    "metadata_docs": [],
                    "feature_docs": [],
                    "global_constitution": "",
                    "final_payload": "",
                    "clarification_message": None,
                    "history": []
                })
                
        self.assertEqual(result['implicated_features'], [])
        self.assertEqual(result['clarification_message'], "Query is too broad. Please narrow down.")
        self.assertIn("Clarification Needed", result['final_payload'])

def json_dumps_bytes(data):
    import json
    return json.dumps(data).encode('utf-8')

# 2. Integration Tests

class TestIntegrationRAGEngine(unittest.TestCase):
    @patch('rag_engine.get_s3_client')
    def test_ollama_intent_analyzer_routing(self, mock_get_s3):
        # Skip if Ollama container is offline
        if not is_ollama_online():
            self.skipTest("Ollama is offline. Skipping LLM integration test.")
            
        s3_mock = MagicMock()
        mock_get_s3.return_value = s3_mock
        s3_mock.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "features/AUTH-101.md"},
                {"Key": "features/JIRA-202.md"}
            ]
        }
        
        # Run Analyze node with live Ollama connection
        # Intent explicitly targets AUTH-101 SSO auth flow
        state = {
            "intent": "Implement AUTH-101 SSO auth flow",
            "implicated_features": [],
            "metadata_docs": [],
            "feature_docs": [],
            "global_constitution": "",
            "final_payload": "",
            "clarification_message": None,
            "history": []
        }
        
        res = analyze_intent_node(state)
        # Verify Ollama successfully maps it to AUTH-101 or triggers fallback
        self.assertTrue('implicated_features' in res)
        # Expecting AUTH-101 in the list since it's in the query
        self.assertIn("AUTH-101", res['implicated_features'])

# 3. End-to-End Tests

class TestE2ERAGEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_dir)
        
        # Mock S3 credentials
        os.makedirs(".cckb", exist_ok=True)
        with open(".cckb/.env", "w") as f:
            f.write("MINIO_ROOT_USER=minioadmin\n")
            f.write("MINIO_ROOT_PASSWORD=minioadmin123\n")
            
        # Write dummy specs on local running MinIO container
        self.s3 = boto3.client(
            's3',
            endpoint_url='http://localhost:9000',
            aws_access_key_id='minioadmin',
            aws_secret_access_key='minioadmin123',
            region_name='us-east-1'
        )
        
        # Pre-seed global constitution
        self.s3.put_object(
            Bucket='cckb-data',
            Key='hot_memory.md',
            Body=b"# System Constitution\nDesign rules only.",
            ContentType='text/markdown'
        )
        
        # Pre-seed AUTH-101
        self.s3.put_object(
            Bucket='cckb-data',
            Key='features/AUTH-101.md',
            Body=b"# AUTH-101 Spec\nSSO authentication logic.",
            ContentType='text/markdown'
        )
        self.s3.put_object(
            Bucket='cckb-data',
            Key='metadata/AUTH-101.meta.json',
            Body=json_dumps_bytes({
                "spec_id": "AUTH-101",
                "dependencies": [{"type": "file", "name": "SSO", "file": "auth.py"}]
            }),
            ContentType='application/json'
        )

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.temp_dir)

    def test_e2e_multiturn_conversational_checkpointer(self):
        thread_id = "thread_e2e_test_99"
        
        # Feed three sequential, related prompts under the same thread config
        payload_1 = query_rag_engine("We need to configure AUTH-101", thread_id)
        self.assertIn("AUTH-101 Spec", payload_1)
        
        payload_2 = query_rag_engine("We are also looking at JIRA-202 keys", thread_id)
        
        # Third query
        payload_3 = query_rag_engine("Let's review the AUTH-101 spec again", thread_id)
        self.assertIn("AUTH-101 Spec", payload_3)
        
        # Verify checkpoints.db sqlite database directly
        db_path = ".cckb/checkpoints.db"
        self.assertTrue(os.path.exists(db_path))
        
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # Verify thread checkpoints rows exist
        c.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,))
        count = c.fetchone()[0]
        self.assertTrue(count > 0, f"Expected checkpoints to be recorded for thread_id, found {count}")
        
        # Retrieve state history from checkpoints directly
        c.execute("SELECT checkpoint FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id DESC LIMIT 1", (thread_id,))
        # Since the checkpointer stores MsgPack serialized blobs, we can check that it's present and non-empty
        blob = c.fetchone()[0]
        self.assertIsNotNone(blob)
        self.assertTrue(len(blob) > 0)
        
        conn.close()
        print("E2E RAG checkpointer verification succeeded!")
