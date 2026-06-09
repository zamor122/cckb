#!/usr/bin/env python3
import os
import sys
import time
import uuid
import socket
import signal
import threading
import webbrowser
import operator
import boto3
from typing import Annotated, TypedDict, Optional
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# CCKB auto-spec generation
try:
    from codebase_scanner import CodebaseScanner
    _SCANNER_AVAILABLE = True
except ImportError:
    _SCANNER_AVAILABLE = False

# GitPython
try:
    from git import Repo
    import git.exc
except ImportError:
    print("Warning: gitpython not installed. Git features will not work.")

# LangGraph & LangChain Ollama
try:
    from langchain_ollama import OllamaLLM
    from langgraph.graph import StateGraph, START, END
except ImportError:
    print("Warning: langchain-ollama or langgraph not installed.")

# Redis / RQ (optional — falls back gracefully if not installed)
try:
    import redis as _redis_lib
    from rq import Queue as _RQQueue
    _RQ_AVAILABLE = True
except ImportError:
    _RQ_AVAILABLE = False

# DB utils
from db_utils import create_job, get_job_status, init_db

app = FastAPI(title="CCKB Init Engine")

# HTML Page template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>CCKB Initialization</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        body {
            background-color: #0b0f19;
            color: #f3f4f6;
            font-family: 'Outfit', sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background-image: radial-gradient(circle at 10% 20%, rgba(30, 58, 138, 0.15) 0%, transparent 40%),
                              radial-gradient(circle at 90% 80%, rgba(99, 102, 241, 0.15) 0%, transparent 40%);
        }
        .container {
            background: rgba(17, 24, 39, 0.7);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 24px;
            padding: 40px;
            width: 450px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.8s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        h1 {
            font-size: 28px;
            font-weight: 800;
            margin-bottom: 8px;
            background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-align: center;
        }
        p.subtitle {
            font-size: 14px;
            color: #9ca3af;
            text-align: center;
            margin-bottom: 32px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #818cf8;
            margin-bottom: 8px;
        }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 12px 16px;
            background: rgba(31, 41, 55, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            color: #ffffff;
            font-family: inherit;
            font-size: 14px;
            box-sizing: border-box;
            transition: all 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #6366f1;
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25);
            background: rgba(31, 41, 55, 0.8);
        }
        button.btn-primary {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
            border: none;
            border-radius: 12px;
            color: #ffffff;
            font-family: inherit;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
        }
        button.btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
        }
        .divider {
            display: flex;
            align-items: center;
            text-align: center;
            margin: 24px 0;
            color: #4b5563;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .divider::before, .divider::after {
            content: '';
            flex: 1;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }
        .divider:not(:empty)::before {
            margin-right: .5em;
        }
        .divider:not(:empty)::after {
            margin-left: .5em;
        }
        button.btn-secondary {
            width: 100%;
            padding: 14px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 12px;
            color: #e5e7eb;
            font-family: inherit;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }
        button.btn-secondary:hover {
            background: rgba(255, 255, 255, 0.08);
            color: #ffffff;
        }
        .alert {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            padding: 16px;
            border-radius: 12px;
            margin-bottom: 24px;
            font-size: 14px;
            color: #fca5a5;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Initialize CCKB</h1>
        <p class="subtitle">Enter credentials or use Code-First fallback to scan repository history.</p>
        <form action="/submit" method="post">
            <div class="form-group">
                <label for="jira_api_key">Jira API Key / Token</label>
                <input type="password" id="jira_api_key" name="jira_api_key" placeholder="Enter Jira API key (optional)">
            </div>
            <div class="form-group">
                <label for="notion_api_key">Notion API Key / Token</label>
                <input type="password" id="notion_api_key" name="notion_api_key" placeholder="Enter Notion API key (optional)">
            </div>
            <button type="submit" class="btn-primary">Save and Initialize</button>
        </form>
        
        <div class="divider">or</div>
        
        <form action="/fallback" method="post">
            <button type="submit" class="btn-secondary">Skip and Generate from Code</button>
        </form>
    </div>
</body>
</html>
"""

# Helper to find free ports starting from 3000
def find_free_port(start_port=3000):
    port = start_port
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            port += 1

# Graceful shutdown helper
def shutdown_server():
    def stop():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=stop).start()

# Gitignore update helper
def update_gitignore():
    gitignore_path = ".gitignore"
    entry = ".cckb/.env"
    content = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            content = f.read()
    if entry not in content:
        with open(gitignore_path, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")

@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the React frontend SPA if compiled, otherwise the legacy HTML template."""
    dist_index = os.path.join(os.path.dirname(__file__), "frontend", "dist", "index.html")
    if os.path.exists(dist_index):
        with open(dist_index, "r") as f:
            return f.read()
    return HTML_TEMPLATE


# ─────────────────────────────────────────────
# Async Job API  (Phase 2 additions)
# ─────────────────────────────────────────────

class InitRequest(BaseModel):
    repo_path: str


def _get_redis_queue() -> Optional["_RQQueue"]:
    """Return an RQ Queue connected to Redis, or None if unavailable."""
    if not _RQ_AVAILABLE:
        return None
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        conn = _redis_lib.from_url(redis_url)
        conn.ping()  # Raise if Redis is not reachable
        return _RQQueue("cckb", connection=conn)
    except Exception:
        return None


@app.post("/init")
def init_scan(req: InitRequest):
    """
    Enqueue a new async codebase scan job.

    Accepts JSON: {"repo_path": "/abs/path/to/repo"}
    Returns:      {"job_id": "<uuid>", "status": "queued"}
    """
    repo_path = req.repo_path.strip()
    if not repo_path:
        raise HTTPException(status_code=400, detail="repo_path must not be empty")

    job_id = str(uuid.uuid4())
    init_db()  # ensure table exists
    create_job(job_id, repo_path)

    queue = _get_redis_queue()
    if queue is not None:
        queue.enqueue("worker.run_codebase_scan", job_id, repo_path, job_id=job_id)
    else:
        # Redis unavailable — update status to reflect this
        from db_utils import update_job_status
        update_job_status(
            job_id,
            "failed",
            0,
            "Redis is unavailable. Start the Redis container and retry.",
        )

    return JSONResponse({"job_id": job_id, "status": "queued"})


@app.get("/status/{job_id}")
def job_status(job_id: str):
    """
    Retrieve the current status of an async scan job.

    Returns: {"job_id": ..., "status": ..., "progress": ..., "message": ...}
    """
    row = get_job_status(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JSONResponse({
        "job_id":   row["job_id"],
        "status":   row["status"],
        "progress": row["progress"],
        "message":  row.get("message") or "",
    })

@app.post("/submit")
def submit_credentials(jira_api_key: str = Form(None), notion_api_key: str = Form(None)):
    cckb_dir = os.path.abspath(".cckb")
    os.makedirs(cckb_dir, exist_ok=True)
    os.chmod(cckb_dir, 0o700)
    
    env_path = os.path.join(cckb_dir, ".env")
    with open(env_path, "w") as f:
        f.write(f"JIRA_API_KEY={jira_api_key or ''}\n")
        f.write(f"NOTION_API_KEY={notion_api_key or ''}\n")
        # Save MinIO credentials for the maintenance crawler
        minio_user = os.getenv("MINIO_ROOT_USER", "minioadmin")
        minio_pass = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
        f.write(f"MINIO_ROOT_USER={minio_user}\n")
        f.write(f"MINIO_ROOT_PASSWORD={minio_pass}\n")
    os.chmod(env_path, 0o600)
    
    update_gitignore()
    
    # Silently install post-commit hook
    try:
        from crawler import install_hook
        install_hook()
    except Exception as e:
        print(f"Warning: Failed to install post-commit hook: {e}")
        
    shutdown_server()
    
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Initialization Complete</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            body {
                background-color: #0b0f19;
                color: #f3f4f6;
                font-family: 'Outfit', sans-serif;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }
            .container {
                background: rgba(17, 24, 39, 0.7);
                backdrop-filter: blur(16px);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 24px;
                padding: 40px;
                width: 450px;
                text-align: center;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
            }
            h1 {
                background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Success!</h1>
            <p>Credentials saved to <code>.cckb/.env</code>.</p>
            <p>FastAPI server is shutting down.</p>
        </div>
    </body>
    </html>
    """)

# Define Graph State for LangGraph
class GraphState(TypedDict):
    batches: list[list[dict]]
    batch_index: int
    batch_summaries: Annotated[list[str], operator.add]
    final_markdown: str

# Helper to check if Ollama is online
def is_ollama_online():
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False

# LangGraph nodes
def summarize_batch_node(state: GraphState):
    idx = state['batch_index']
    batches = state['batches']
    
    if idx >= len(batches):
        return {"batch_index": idx}
        
    batch = batches[idx]
    commits_text = ""
    for c in batch:
        commits_text += f"Commit: {c['hexsha']}\nAuthor: {c['author']}\nMessage: {c['message']}\nDiff:\n{c['diff']}\n\n"
        
    prompt = f"""You are analyzing a batch of git commits to extract architectural, design, styling, and structural rules of the codebase.
Analyze the following commits:
{commits_text}

Identify and summarize:
1. Architectural patterns & component structures.
2. Styling patterns, UI preferences, and library choices.
3. Common coding conventions, helper function patterns, or testing conventions.

Provide a concise, bulleted summary of your findings.
"""
    
    summary = ""
    if is_ollama_online():
        try:
            llm = OllamaLLM(model="llama3.2:3b", base_url="http://127.0.0.1:11434", timeout=30)
            summary = llm.invoke(prompt)
        except Exception as e:
            print(f"Ollama invocation error: {e}", file=sys.stderr)
            summary = f"Ollama execution error: {str(e)}"
    else:
        # Graceful fallback logic
        summary = "Ollama is offline. Skipped batch summary."
        
    return {
        "batch_summaries": [summary],
        "batch_index": idx + 1
    }

def synthesize_constitution_node(state: GraphState):
    summaries = state.get('batch_summaries', [])
    
    if not is_ollama_online():
        # Complete fallback when offline
        print("Warning: Ollama container is unreachable. Generating fallback hot_memory.md.", file=sys.stderr)
        final_md = """# Project Constitution (Hot Memory)

> [!WARNING]
> Ollama was offline during code-first constitution generation. This is a basic fallback document.

## 1. Architectural Design & Project Structure
- Standard Python codebase.
- MinIO Object Storage layer for caching data.
- Ollama Local Inference engine for LLM processing.

## 2. Key Styling Patterns & UI Design Guidelines
- Modern responsive layout.
- Styling prefers standard CSS or standard widgets where appropriate.

## 3. Implementation Conventions
- Clean code with robust exception handling.
- Integrated testing setup utilizing pytest.
"""
        return {"final_markdown": final_md}
        
    summaries_text = "\n\n".join([f"Batch {i+1} Summary:\n{s}" for i, s in enumerate(summaries)])
    prompt = f"""You are an elite software architect compiling a project constitution (hot_memory.md) from git diff summaries.
Combine the following findings into a comprehensive, professional, and well-structured markdown document:
{summaries_text}

The output must be formatted as Markdown with these sections:
# Project Constitution (Hot Memory)

## 1. Architectural Design & Project Structure
- Describe project layout, module boundaries, database interactions, etc.

## 2. Key Styling Patterns & UI Design Guidelines
- Detail color palettes, CSS conventions, transitions, fonts, and responsiveness.

## 3. Implementation Conventions
- Detail code patterns, helper logic, testing suites, error-handling conventions, and design paradigms.

Ensure there are no placeholders and the document reads like a production-ready guide for onboarded engineers.
"""
    
    try:
        llm = OllamaLLM(model="llama3.2:3b", base_url="http://127.0.0.1:11434", timeout=45)
        final_md = llm.invoke(prompt)
    except Exception as e:
        print(f"Ollama synthesis error: {e}", file=sys.stderr)
        final_md = f"# Project Constitution (Hot Memory)\n\nFailed to compile due to Ollama error: {str(e)}"
        
    return {"final_markdown": final_md}

def run_code_first_fallback(repo_path: str) -> str:
    # 1. Initialize Repo
    try:
        repo = Repo(repo_path)
    except Exception as e:
        # Propagate GitRepositoryError
        raise git.exc.InvalidGitRepositoryError(f"Invalid git repository: {e}")
        
    # 2. Extract last 50 commits
    commits = list(repo.iter_commits(max_count=50))
    commit_data = []
    
    for commit in commits:
        diff_lines = []
        try:
            if commit.parents:
                diffs = commit.parents[0].diff(commit, create_patch=True)
            else:
                diffs = repo.git.show(commit.hexsha, format="").splitlines()
                diff_lines = diffs
                
            if not diff_lines:
                for d in diffs:
                    diff_lines.append(f"File: {d.a_path} -> {d.b_path}")
                    if d.diff:
                        diff_lines.append(d.diff.decode('utf-8', errors='replace'))
        except Exception as e:
            diff_lines = [f"Error getting diff: {str(e)}"]
            
        commit_data.append({
            'hexsha': commit.hexsha[:8],
            'author': commit.author.name if commit.author else "Unknown",
            'message': commit.message.strip() if commit.message else "",
            'diff': "\n".join(diff_lines)[:2000]
        })
        
    # 3. Batch commits (5 per batch)
    batches = [commit_data[i:i + 5] for i in range(0, len(commit_data), 5)]
    
    # 4. Construct LangGraph Workflow
    workflow = StateGraph(GraphState)
    workflow.add_node("summarize_batch", summarize_batch_node)
    workflow.add_node("synthesize_constitution", synthesize_constitution_node)
    
    def should_continue(state: GraphState):
        if state['batch_index'] < len(state['batches']):
            return "summarize_batch"
        else:
            return "synthesize_constitution"
            
    workflow.add_edge(START, "summarize_batch")
    workflow.add_conditional_edges(
        "summarize_batch",
        should_continue,
        {
            "summarize_batch": "summarize_batch",
            "synthesize_constitution": "synthesize_constitution"
        }
    )
    workflow.add_edge("synthesize_constitution", END)
    
    graph = workflow.compile()
    
    initial_state = {
        "batches": batches,
        "batch_index": 0,
        "batch_summaries": [],
        "final_markdown": ""
    }
    
    result = graph.invoke(initial_state)
    return result.get("final_markdown", "")

@app.post("/fallback")
def fallback():
    # Verify Git Repository before starting fallback computation
    try:
        Repo(os.getcwd())
    except Exception as e:
        return HTMLResponse(
            status_code=500,
            content=f"<div class='alert'>Error: Not a valid Git repository ({str(e)}).</div>"
        )
        
    # Log Ollama offline if unreachable
    if not is_ollama_online():
        print("Warning: Local Ollama container is unreachable. Running offline fallback.", file=sys.stderr)
        
    # Run code-first fallback
    try:
        final_markdown = run_code_first_fallback(os.getcwd())
    except Exception as e:
        return HTMLResponse(
            status_code=500,
            content=f"<h1>Error processing git history</h1><p>{str(e)}</p>"
        )
        
    # Upload to MinIO
    try:
        # Read MinIO credentials from env or default values
        minio_user = os.getenv("MINIO_ROOT_USER", "minioadmin")
        minio_pass = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
        
        s3 = boto3.client(
            's3',
            endpoint_url='http://localhost:9000',
            aws_access_key_id=minio_user,
            aws_secret_access_key=minio_pass,
            region_name='us-east-1'
        )
        s3.put_object(
            Bucket='cckb-data',
            Key='hot_memory.md',
            Body=final_markdown.encode('utf-8'),
            ContentType='text/markdown'
        )
    except Exception as e:
        print(f"Error uploading to MinIO: {e}", file=sys.stderr)
        # Even if MinIO fails, we shouldn't crash completely, but let's report it
        return HTMLResponse(
            status_code=500,
            content=f"<h1>Failed to upload to MinIO</h1><p>{str(e)}</p>"
        )
        
    # Phase 2: Auto-generate feature specs using CodebaseScanner
    indexed_specs = []
    if _SCANNER_AVAILABLE:
        try:
            scanner = CodebaseScanner(os.getcwd(), max_domains=10)
            print(f"[Phase 2] Framework detected: {scanner.framework}", file=sys.stderr)
            domains = scanner.discover_feature_domains()
            print(f"[Phase 2] Discovered {len(domains)} feature domain(s)", file=sys.stderr)
            for domain in domains:
                try:
                    spec_md, spec_meta = scanner.generate_spec(domain, use_llm=True)
                    success = scanner.upload_spec(domain["spec_id"], spec_md, spec_meta)
                    if success:
                        indexed_specs.append(domain["spec_id"])
                        print(f"[Phase 2] Indexed: {domain['spec_id']}", file=sys.stderr)
                except Exception as spec_err:
                    print(f"[Phase 2] Warning: Failed to generate spec for {domain.get('spec_id', '?')}: {spec_err}", file=sys.stderr)
        except Exception as phase2_err:
            print(f"[Phase 2] Warning: Feature spec generation failed: {phase2_err}", file=sys.stderr)
    else:
        print("[Phase 2] Skipped: codebase_scanner.py not available", file=sys.stderr)

    # Silently install post-commit hook
    try:
        from crawler import install_hook
        install_hook()
    except Exception as e:
        print(f"Warning: Failed to install post-commit hook: {e}")

    shutdown_server()

    # Build indexed specs HTML
    specs_html = ""
    if indexed_specs:
        spec_items = "".join(f"<li><code>{s}</code></li>" for s in indexed_specs)
        specs_html = f"""
        <div style="margin-top:20px;text-align:left;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);border-radius:12px;padding:16px;">
            <p style="margin:0 0 8px;font-size:13px;color:#a5b4fc;font-weight:600;">✅ {len(indexed_specs)} feature spec(s) auto-indexed:</p>
            <ul style="margin:0;padding-left:18px;font-size:13px;color:#d1d5db;">{spec_items}</ul>
        </div>"""
    else:
        specs_html = "<p style='color:#6b7280;font-size:13px;margin-top:12px;'>No feature specs auto-indexed (Ollama may be offline or no domains detected).</p>"

    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Initialization Complete</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            body {{
                background-color: #0b0f19;
                color: #f3f4f6;
                font-family: 'Outfit', sans-serif;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }}
            .container {{
                background: rgba(17, 24, 39, 0.7);
                backdrop-filter: blur(16px);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 24px;
                padding: 40px;
                width: 480px;
                text-align: center;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
            }}
            h1 {{
                background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .check {{ color: #34d399; margin-right: 6px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>CCKB Initialized!</h1>
            <p><span class="check">✓</span> Project constitution generated (<code>hot_memory.md</code>)</p>
            {specs_html}
            <p style="color:#6b7280;font-size:12px;margin-top:20px;">Server shutting down.</p>
        </div>
    </body>
    </html>
    """)

# Mount static assets if frontend is built
try:
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    dist_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    if os.path.exists(dist_dir):
        app.mount("/assets", StaticFiles(directory=os.path.join(dist_dir, "assets")), name="assets")

        @app.get("/favicon.svg")
        def favicon():
            return FileResponse(os.path.join(dist_dir, "favicon.svg"))

        @app.get("/icons.svg")
        def icons():
            return FileResponse(os.path.join(dist_dir, "icons.svg"))

        @app.get("/vite.svg")
        def vite():
            # Fallback to favicon.svg if vite.svg is requested
            return FileResponse(os.path.join(dist_dir, "favicon.svg"))
except Exception as e:
    print(f"Warning: could not mount frontend assets: {e}")

def run_cli():
    # Check if we are in a valid Git repository first
    try:
        Repo(os.getcwd())
    except Exception:
        print("Error: The current directory is not a valid Git repository.", file=sys.stderr)
        sys.exit(1)
        
    # Find port and start
    port = find_free_port(3000)
    print(f"Starting initialization server on 127.0.0.1:{port}...")
    
    # Auto-open browser on startup
    def open_browser():
        time.sleep(1)
        webbrowser.open(f"http://127.0.0.1:{port}")
    threading.Thread(target=open_browser, daemon=True).start()
    
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("Server stopped manually.")

if __name__ == "__main__":
    run_cli()
