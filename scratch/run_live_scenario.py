#!/usr/bin/env python3
"""
scratch/run_live_scenario.py

A complete live test scenario script. It:
1. Clones a popular micro-repository (bottlepy/bottle)
2. Starts the uvicorn FastAPI server and the RQ worker in subprocesses
3. Hits POST /init to queue a codebase scan
4. Polls GET /status/{job_id} until completion, printing updates
5. Verifies that the generated specifications exist on MinIO
6. Gracefully terminates all services and cleans up cloned files
"""

import os
import sys
import time
import subprocess
import shutil
import uuid
import boto3
import urllib.request
import urllib.error
import json
import socket

# Load dotenv to get GEMINI_API_KEY if present in workspace .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configurations
REPO_URL = "https://github.com/bottlepy/bottle.git"
CLONE_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "cloned-repos", "bottle"))
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cckb", "live_scenario_jobs.db"))

def find_free_port(start_port=3005):
    port = start_port
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            port += 1

def check_env():
    """Verify LLM and API configuration."""
    api_key = os.environ.get("GEMINI_API_KEY")
    
    # Try loading from .cckb/.env as well
    if not api_key:
        cckb_env = os.path.join(".cckb", ".env")
        if os.path.exists(cckb_env):
            with open(cckb_env) as f:
                for line in f:
                    if "GEMINI_API_KEY" in line and "=" in line:
                        api_key = line.split("=", 1)[1].strip()
                        os.environ["GEMINI_API_KEY"] = api_key
                        print("[Scenario] Loaded GEMINI_API_KEY from .cckb/.env")
                        break

    if api_key:
        print("[Scenario] Using Google Gemini API (Free Tier). This should be fast.")
    else:
        print("[Scenario] Warning: GEMINI_API_KEY not found. Falling back to local Ollama (Llama 3.2).")
        # Check if Ollama is running
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            print("[Scenario] Local Ollama is online.")
        except Exception:
            print("[Scenario] Error: Neither GEMINI_API_KEY is set nor is local Ollama reachable.")
            print("[Scenario] Please define GEMINI_API_KEY in your shell or .env file.")
            sys.exit(1)

def clean_cloned_repo():
    if os.path.exists(CLONE_PATH):
        print(f"[Scenario] Cleaning up clone directory: {CLONE_PATH}")
        shutil.rmtree(CLONE_PATH, ignore_errors=True)

def clone_repo():
    print(f"[Scenario] Cloning popular repository {REPO_URL} to {CLONE_PATH}...")
    os.makedirs(os.path.dirname(CLONE_PATH), exist_ok=True)
    subprocess.run(["git", "clone", "--depth=1", REPO_URL, CLONE_PATH], check=True)
    print("[Scenario] Clone completed successfully.")

def main():
    check_env()
    clean_cloned_repo()
    clone_repo()

    # Remove old DB if exists to avoid noise
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    port = find_free_port(3005)
    server_url = f"http://127.0.0.1:{port}"
    print(f"[Scenario] Using database path: {DB_PATH}")
    print(f"[Scenario] Starting FastAPI server on {server_url}...")

    # Set up logs dir
    os.makedirs(".cckb", exist_ok=True)
    server_log = open(".cckb/live_server.log", "w")
    worker_log = open(".cckb/live_worker.log", "w")

    # Start FastAPI server
    env = os.environ.copy()
    env["CCKB_DB_PATH"] = DB_PATH
    env["PYTHONUNBUFFERED"] = "1"
    
    workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    uvicorn_bin = os.path.join(workspace_dir, ".venv", "bin", "uvicorn")
    python_bin = os.path.join(workspace_dir, ".venv", "bin", "python")

    server_process = subprocess.Popen(
        [uvicorn_bin, "init_engine:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT
    )

    # Start RQ worker
    print("[Scenario] Starting background RQ worker process...")
    worker_process = subprocess.Popen(
        [python_bin, "worker.py"],
        env=env,
        stdout=worker_log,
        stderr=subprocess.STDOUT
    )

    # Give server a second to start up
    time.sleep(3)

    job_id = None
    try:
        # Step 1: Submit POST /init
        init_endpoint = f"{server_url}/init"
        print(f"[Scenario] Sending POST to {init_endpoint} with repo_path={CLONE_PATH}")
        
        req_data = json.dumps({"repo_path": CLONE_PATH}).encode('utf-8')
        req = urllib.request.Request(
            init_endpoint, 
            data=req_data, 
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            job_id = res_data["job_id"]
            print(f"[Scenario] Job successfully queued. Job ID: {job_id}")

        # Step 2: Poll status endpoint
        status_endpoint = f"{server_url}/status/{job_id}"
        print("[Scenario] Entering polling loop (timeout: 300s)...")
        
        start_time = time.monotonic()
        last_progress = -1
        while time.monotonic() - start_time < 300:
            try:
                with urllib.request.urlopen(status_endpoint, timeout=5) as resp:
                    status_info = json.loads(resp.read().decode('utf-8'))
                    status = status_info["status"]
                    progress = status_info.get("progress", 0)
                    message = status_info.get("message", "")
                    
                    if progress != last_progress or status in ("completed", "failed"):
                        print(f"  [Poll] Status: {status:<10} | Progress: {progress:3d}% | Message: {message}", flush=True)
                        last_progress = progress
                        
                    if status == "completed":
                        print("[Scenario] Scan completed successfully!")
                        break
                    elif status == "failed":
                        print(f"[Scenario] Scan failed: {message}")
                        sys.exit(1)
            except urllib.error.URLError as e:
                # Server might still be booting or busy
                pass
            
            time.sleep(2)
        else:
            print("[Scenario] Timeout: Job did not finish within 300 seconds.")
            sys.exit(1)

        # Step 3: Verify MinIO Upload
        print("[Scenario] Verifying spec uploads in MinIO...")
        minio_user = os.getenv("MINIO_ROOT_USER", "minioadmin")
        minio_pass = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
        s3 = boto3.client(
            "s3",
            endpoint_url="http://localhost:9000",
            aws_access_key_id=minio_user,
            aws_secret_access_key=minio_pass,
            region_name="us-east-1",
        )
        
        objects = s3.list_objects_v2(Bucket="cckb-data")
        keys = [obj["Key"] for obj in objects.get("Contents", [])]
        print(f"[Scenario] Found {len(keys)} objects in MinIO bucket 'cckb-data':")
        for key in keys:
            if "hot_memory" in key or "features/" in key:
                print(f"  - {key}")

        assert any("hot_memory.md" in k for k in keys), "Constitution 'hot_memory.md' missing from MinIO!"
        print("[Scenario] All validation checks passed!")

    finally:
        # Cleanup processes
        print("[Scenario] Terminating backend server and worker...")
        server_process.terminate()
        worker_process.terminate()
        try:
            server_process.wait(timeout=5)
            worker_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
            worker_process.kill()
        
        server_log.close()
        worker_log.close()
        
        # Cleanup clone directory
        clean_cloned_repo()
        
        # Cleanup temporary DB
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            
        print("[Scenario] Teardown complete. Goodbye!")

if __name__ == "__main__":
    main()
