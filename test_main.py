import os
import sys
import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import pytest
import boto3
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Import from main
from main import cckb_query

# 1. Unit Tests

class TestUnitMain(unittest.TestCase):
    @patch('main.query_rag_engine')
    def test_cckb_query_direct_invocation(self, mock_query):
        # Mock payload return
        mock_query.return_value = "# RAG Context\nAll good."
        
        # Invoke master tool directly
        res = cckb_query("Implement Jira auth link")
        self.assertEqual(res, "# RAG Context\nAll good.")
        mock_query.assert_called_once_with("Implement Jira auth link")

    @patch('main.query_rag_engine')
    def test_cckb_query_truncation_warning(self, mock_query):
        # Create huge payload > 50,000 chars
        large_payload = "A" * 60000
        mock_query.return_value = large_payload
        
        res = cckb_query("Huge query")
        
        # Assert truncated to 50,000 + warning
        self.assertEqual(len(res), 50000 + len("\n\n[WARNING: Response truncated to 50,000 characters to maintain protocol stability and prevent context bloating.]"))
        self.assertTrue(res.endswith("[WARNING: Response truncated to 50,000 characters to maintain protocol stability and prevent context bloating.]"))

# 2. Integration Tests

class TestIntegrationMain(unittest.TestCase):
    def test_stdio_jsonrpc_initialization_transport(self):
        # Start main.py server as subprocess
        process = subprocess.Popen(
            [sys.executable, "main.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Construct valid JSON-RPC 2.0 initialize request
        init_req = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0"
                }
            },
            "id": 1
        }
        
        try:
            # Send initial request to process stdin
            process.stdin.write(json.dumps(init_req) + "\n")
            process.stdin.flush()
            
            # Read first line from stdout
            response_line = process.stdout.readline()
            
            # Parse JSON-RPC response
            res = json.loads(response_line)
            
            # Assert schema conforms to protocol
            self.assertEqual(res.get("jsonrpc"), "2.0")
            self.assertEqual(res.get("id"), 1)
            self.assertIn("capabilities", res.get("result", {}))
            
        finally:
            # Terminate process safely
            process.terminate()
            process.wait()

# 3. End-to-End Tests

@pytest.mark.anyio
async def test_e2e_mcp_client_tool_query_loop():
    # Setup mock files in MinIO cckb-data bucket to verify end-to-end routing
    s3_client = boto3.client(
        's3',
        endpoint_url='http://localhost:9000',
        aws_access_key_id='minioadmin',
        aws_secret_access_key='minioadmin123',
        region_name='us-east-1'
    )
    
    # Pre-seed E2E auth spec
    s3_client.put_object(
        Bucket='cckb-data',
        Key='features/AUTH-101.md',
        Body=b"# Auth spec details\nSSO authentication logic E2E.",
        ContentType='text/markdown'
    )
    
    s3_client.put_object(
        Bucket='cckb-data',
        Key='metadata/AUTH-101.meta.json',
        Body=json.dumps({
            "spec_id": "AUTH-101",
            "dependencies": [{"type": "file", "name": "SSO", "file": "auth.py"}]
        }).encode('utf-8'),
        ContentType='application/json'
    )
    
    # Pre-seed global constitution
    s3_client.put_object(
        Bucket='cckb-data',
        Key='hot_memory.md',
        Body=b"# System rules\nNo prints allowed.",
        ContentType='text/markdown'
    )
    
    # Configure stdio server parameters pointing directly to main.py
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["main.py"],
        env=os.environ
    )
    
    # Run client session loop
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1. Initialize
            await session.initialize()
            
            # 2. List tools and assert cckb_query exists
            tools_resp = await session.list_tools()
            tool_names = [tool.name for tool in tools_resp.tools]
            assert "cckb_query" in tool_names
            
            # 3. Call tool
            call_resp = await session.call_tool(
                "cckb_query",
                arguments={"intent_string": "Implement AUTH-101 SSO auth flow"}
            )
            
            content = call_resp.content[0].text
            assert "Auth spec details" in content
            assert "SSO authentication logic E2E." in content
            assert "System rules" in content
            print("E2E FastMCP Tool query loop verified successfully!")
