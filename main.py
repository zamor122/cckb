#!/usr/bin/env python3
import os
import sys
import logging
import builtins
from mcp.server.fastmcp import FastMCP
from rag_engine import query_rag_engine

# 1. Logging and Stdout/Stderr Redirection
os.makedirs(".cckb", exist_ok=True)
log_file_path = ".cckb/server.log"

# Setup basic logging to write strictly to server.log
logging.basicConfig(
    filename=log_file_path,
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Keep track of original stdout
original_stdout = sys.stdout

# Wrapper to redirect binary writes from sys.stdout.buffer
class BufferRedirector:
    def __init__(self, log_file, orig_buffer):
        self.log_file = log_file
        self.orig_buffer = orig_buffer
        
    def write(self, data: bytes):
        try:
            decoded = data.decode('utf-8', errors='replace')
        except Exception:
            decoded = ""
            
        if "jsonrpc" in decoded:
            return self.orig_buffer.write(data)
        else:
            if decoded.strip():
                with open(self.log_file, "a") as f:
                    f.write(decoded)
                    if not decoded.endswith("\n"):
                        f.write("\n")
            return len(data)
            
    def flush(self):
        self.orig_buffer.flush()
        
    def __getattr__(self, name):
        return getattr(self.orig_buffer, name)

# Wrapper to intercept stdout prints/logs and preserve JSON-RPC frames
class StdoutRedirector:
    def __init__(self, log_file, orig_stdout):
        self.log_file = log_file
        self.orig_stdout = orig_stdout
        self.buffer = BufferRedirector(log_file, orig_stdout.buffer)
        
    def write(self, data):
        # Only write valid JSON-RPC messages to the actual stdout
        if "jsonrpc" in data:
            self.orig_stdout.write(data)
            self.orig_stdout.flush()
        else:
            if data.strip():
                with open(self.log_file, "a") as f:
                    f.write(data)
                    if not data.endswith("\n"):
                        f.write("\n")
                        
    def flush(self):
        self.orig_stdout.flush()
        
    def __getattr__(self, name):
        return getattr(self.orig_stdout, name)

# Redirect stdout to protect stdio transport
sys.stdout = StdoutRedirector(log_file_path, original_stdout)

# Redirect built-in print statement to write strictly to log file
def custom_print(*args, **kwargs):
    msg = " ".join(map(str, args))
    with open(log_file_path, "a") as f:
        f.write(msg + "\n")

builtins.print = custom_print

# 2. FastMCP Server Initialization
mcp = FastMCP("cckb-server")

# 3. Master Tool definition
@mcp.tool()
def cckb_query(intent_string: str) -> str:
    """
    Query the CCKB intelligence layer with a natural language developer intent.
    It analyzes the intent, retrieves global rules, specific features specs,
    and maps the codebase dependencies from MinIO storage.
    """
    try:
        # Invoke LangGraph synthesis pipeline
        payload = query_rag_engine(intent_string)
        
        # Hard limit of 50,000 characters to prevent buffer overflow/context tax
        char_limit = 50000
        if len(payload) > char_limit:
            warning = "\n\n[WARNING: Response truncated to 50,000 characters to maintain protocol stability and prevent context bloating.]"
            payload = payload[:char_limit] + warning
            
        return payload
    except Exception as e:
        # Return graceful error string to IDE
        return f"Error executing CCKB intelligence query: {str(e)}"

import argparse

# 4. Standard execution entry point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CCKB Master Server")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="Launch the local FastAPI credentials gatherer UI")
    group.add_argument("--start", action="store_true", help="Start the headless MCP server (stdio transport)")
    
    args = parser.parse_args()
    
    if args.init:
        # Import and run the init CLI
        from init_engine import run_cli
        run_cli()
    else:
        # Standard stdio transport for Cursor / Claude Desktop subprocesses
        mcp.run(transport="stdio")
