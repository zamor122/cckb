#!/usr/bin/env python3
"""
worker.py — CCKB RQ Background Worker

Consumes the 'cckb' Redis queue and executes long-running codebase scan jobs.
Each job corresponds to a POST /init request from the API.

Usage (standalone):
    python worker.py                      # uses REDIS_URL env or redis://localhost:6379

Usage (rq CLI):
    rq worker --url redis://localhost:6379 cckb
"""

import os
import sys
import logging

# ── Module-level imports (patchable via unittest.mock.patch) ──────────────────
# All heavy symbols are imported here so tests can cleanly patch them.

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore

try:
    from init_engine import run_code_first_fallback
except ImportError:
    run_code_first_fallback = None  # type: ignore

try:
    from codebase_scanner import CodebaseScanner
except ImportError:
    CodebaseScanner = None  # type: ignore

try:
    from crawler import install_hook
except ImportError:
    install_hook = None  # type: ignore

from db_utils import update_job_status, get_db_path as _default_db_path

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cckb.worker")


# ── Redis connection helper ───────────────────────────────────────────────────

def get_redis_conn():
    """
    Return a Redis connection using REDIS_URL env var (default redis://localhost:6379).
    """
    import redis as redis_lib
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return redis_lib.from_url(url)


# ── Main task function ────────────────────────────────────────────────────────

def run_codebase_scan(job_id: str, repo_path: str, db_path: str = None) -> None:
    """
    RQ task: orchestrate the full codebase onboarding pipeline for a repo.

    Progress milestones:
        5   — Initializing scan
        10  — Analyzing git history
        30  — Constitution uploaded to MinIO
        40  — Domain discovery
        50  — Domains discovered, specs starting
        50–95 — Spec generation (one step per domain)
        95  — Installing git hook
        100 — Completed
    """
    effective_db_path = db_path or _default_db_path()

    def _update(status: str, progress: int, message: str) -> None:
        logger.info("[%s] %s (%d%%) — %s", job_id[:8], status, progress, message)
        update_job_status(job_id, status, progress, message, db_path=effective_db_path)

    try:
        _update("processing", 5, "Initializing scan...")

        # ── Phase 1: Generate Project Constitution ────────────────────────────
        _update("processing", 10, "Analyzing git history to generate project constitution...")
        try:
            constitution_md = run_code_first_fallback(repo_path)

            # Upload to MinIO
            minio_user = os.getenv("MINIO_ROOT_USER", "minioadmin")
            minio_pass = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
            s3 = boto3.client(
                "s3",
                endpoint_url="http://localhost:9000",
                aws_access_key_id=minio_user,
                aws_secret_access_key=minio_pass,
                region_name="us-east-1",
            )
            s3.put_object(
                Bucket="cckb-data",
                Key="hot_memory.md",
                Body=constitution_md.encode("utf-8"),
                ContentType="text/markdown",
            )
            _update("processing", 30, "Project constitution uploaded to MinIO")
        except Exception as e:
            logger.warning("Constitution generation failed (non-fatal): %s", e)
            _update("processing", 30, f"Constitution skipped: {e}")

        # ── Phase 2: Domain Discovery & Spec Generation ───────────────────────

        # Progress callback bridges scanner's internal events to the DB
        _last_progress = [30]

        def _scan_progress(progress: int, message: str) -> None:
            if progress > 0:
                # Scale scanner progress (10–45) into our 30–50 window
                scaled = 30 + int((progress - 10) / 35 * 20) if progress >= 10 else 30
                scaled = max(_last_progress[0], min(scaled, 50))
                _last_progress[0] = scaled
                _update("processing", scaled, message)
            else:
                # progress == -1 means message-only update — just log
                logger.info("[%s] Scanner: %s", job_id[:8], message)

        scanner = CodebaseScanner(
            repo_path,
            max_domains=10,
            progress_callback=_scan_progress,
        )

        _update("processing", 40, "Discovering feature domains...")
        domains = scanner.discover_feature_domains()
        n = len(domains)
        _update("processing", 50, f"Discovered {n} feature domain(s) — generating specs...")

        # Generate and upload specs, distributing progress from 50 → 95
        progress_per_domain = int(45 / n) if n > 0 else 45
        current_progress = 50

        for i, domain in enumerate(domains):
            spec_id = domain.get("spec_id", f"domain-{i}")
            try:
                spec_md, spec_meta = scanner.generate_spec(domain, use_llm=True)
                scanner.upload_spec(spec_id, spec_md, spec_meta)
                current_progress = min(95, current_progress + progress_per_domain)
                _update(
                    "processing",
                    current_progress,
                    f"Indexed {i + 1}/{n}: {spec_id}",
                )
            except Exception as e:
                logger.warning("Spec generation failed for %s: %s", spec_id, e)

        # ── Phase 3: Install git hook ─────────────────────────────────────────
        _update("processing", 95, "Installing post-commit hook...")
        try:
            if callable(install_hook):
                install_hook()
        except Exception as e:
            logger.warning("Hook installation failed (non-fatal): %s", e)

        _update("completed", 100, f"Scan complete — {n} feature spec(s) indexed.")

    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id[:8], exc)
        try:
            update_job_status(
                job_id, "failed", 0, f"Scan failed: {exc}", db_path=effective_db_path
            )
        except Exception:
            pass  # Don't double-fault
        raise  # Re-raise so RQ marks the job as failed


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import redis as redis_lib
    import sys
    
    # Disable macOS fork safety check that causes SIGSEGV when libraries like gRPC/urllib are loaded
    os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    
    if sys.platform == "darwin":
        from rq import SimpleWorker as Worker
        logger.info("Running on macOS — using SimpleWorker (non-forking) for stability")
    else:
        from rq import Worker
        logger.info("Running on non-macOS — using standard forking Worker")

    from rq import Queue

    conn = get_redis_conn()
    queue = Queue("cckb", connection=conn)
    w = Worker([queue], connection=conn)
    logger.info("CCKB worker started — listening on 'cckb' queue")
    w.work()
