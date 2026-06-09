#!/usr/bin/env python3
"""
db_utils.py — Shared SQLite job state helpers for CCKB.

Provides a `jobs` table to track async scan jobs (created by the API,
consumed by the RQ worker). Both init_engine.py and worker.py import
from here so there is a single, DRY source of truth for DB access.

Table schema:
    job_id      TEXT PRIMARY KEY
    repo_path   TEXT NOT NULL
    status      TEXT DEFAULT 'queued'   -- queued | processing | completed | failed
    progress    INTEGER DEFAULT 0       -- 0-100
    message     TEXT
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
"""

import os
import sqlite3
from typing import Optional


# ── Path resolution ───────────────────────────────────────────────────────────

def get_db_path() -> str:
    """Return the SQLite DB path (env override or default .cckb/checkpoints.db)."""
    return os.environ.get("CCKB_DB_PATH", os.path.join(".cckb", "checkpoints.db"))


# ── Schema initialisation ─────────────────────────────────────────────────────

def init_db(db_path: Optional[str] = None) -> None:
    """
    Create the `jobs` table if it doesn't already exist.
    Safe to call multiple times (idempotent).
    """
    db_path = db_path or get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                repo_path   TEXT NOT NULL,
                status      TEXT DEFAULT 'queued',
                progress    INTEGER DEFAULT 0,
                message     TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ── Write helpers ─────────────────────────────────────────────────────────────

def create_job(job_id: str, repo_path: str, db_path: Optional[str] = None) -> None:
    """Insert a new job row with status='queued' and progress=0."""
    db_path = db_path or get_db_path()
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (job_id, repo_path, status, progress, message) "
            "VALUES (?, ?, 'queued', 0, 'Job queued')",
            (job_id, repo_path),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_status(
    job_id: str,
    status: str,
    progress: int,
    message: str,
    db_path: Optional[str] = None,
) -> None:
    """
    Update the status, progress, and message for an existing job row.
    Safe to call from the worker process during scan execution.
    """
    db_path = db_path or get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET status=?, progress=?, message=? WHERE job_id=?",
            (status, progress, message, job_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_job_status(
    job_id: str, db_path: Optional[str] = None
) -> Optional[dict]:
    """
    Return a dict with keys {job_id, repo_path, status, progress, message, created_at}
    for the given job_id, or None if the job doesn't exist.
    """
    db_path = db_path or get_db_path()
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
