#!/usr/bin/env python3
"""
seed_minio.py - Upload manually curated seed knowledge to MinIO.

This script uploads:
  - seed_knowledge/hot_memory.md          → cckb-data/hot_memory.md
  - seed_knowledge/*.md (features)        → cckb-data/features/<SPEC_ID>.md
  - seed_knowledge/*.meta.json (metadata) → cckb-data/metadata/<SPEC_ID>.meta.json

Usage:
  python3 seed_minio.py [--dry-run]

Run from the local-cckb directory.
"""
import os
import sys
import json
import argparse
import glob

# Boto3 import
try:
    import boto3
    import botocore.exceptions
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

SEED_DIR = os.path.join(os.path.dirname(__file__), "seed_knowledge")
BUCKET = "cckb-data"

def load_cckb_env():
    """Load MinIO credentials from .cckb/.env in current working directory."""
    env_path = os.path.join(os.getcwd(), ".cckb", ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars

def get_s3_client():
    cckb_env = load_cckb_env()
    minio_user = cckb_env.get("MINIO_ROOT_USER") or os.getenv("MINIO_ROOT_USER", "minioadmin")
    minio_pass = cckb_env.get("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
    return boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        aws_access_key_id=minio_user,
        aws_secret_access_key=minio_pass,
        region_name="us-east-1",
    )

def ensure_bucket(s3, dry_run=False):
    """Create bucket if it doesn't exist."""
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"✓ Bucket '{BUCKET}' already exists.")
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            if not dry_run:
                s3.create_bucket(Bucket=BUCKET)
            print(f"{'[DRY RUN] Would create' if dry_run else '✓ Created'} bucket '{BUCKET}'.")
        else:
            raise

def upload_file(s3, local_path, s3_key, content_type="text/markdown", dry_run=False):
    """Upload a single file to MinIO."""
    with open(local_path, "rb") as f:
        content = f.read()
    size = len(content)
    if dry_run:
        print(f"  [DRY RUN] Would upload: {s3_key} ({size} bytes)")
        return
    s3.put_object(Bucket=BUCKET, Key=s3_key, Body=content, ContentType=content_type)
    print(f"  ✓ Uploaded: {s3_key} ({size} bytes)")

def main():
    parser = argparse.ArgumentParser(description="Seed MinIO with curated CCKB knowledge")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        print("=== DRY RUN MODE — no changes will be made ===\n")

    if not os.path.isdir(SEED_DIR):
        print(f"ERROR: seed_knowledge/ directory not found at {SEED_DIR}", file=sys.stderr)
        sys.exit(1)

    print("Connecting to MinIO at http://localhost:9000 ...")
    try:
        s3 = get_s3_client()
        s3.list_buckets()  # Connectivity check
        print("✓ Connected to MinIO.\n")
    except Exception as e:
        print(f"ERROR: Cannot connect to MinIO: {e}", file=sys.stderr)
        print("Make sure MinIO docker container is running.", file=sys.stderr)
        sys.exit(1)

    ensure_bucket(s3, dry_run)
    print()

    # 1. Upload hot_memory.md (global constitution)
    hot_memory_path = os.path.join(SEED_DIR, "hot_memory.md")
    if os.path.exists(hot_memory_path):
        print("--- Uploading Project Constitution ---")
        upload_file(s3, hot_memory_path, "hot_memory.md", "text/markdown", dry_run)
    else:
        print("WARNING: seed_knowledge/hot_memory.md not found, skipping.")
    print()

    # 2. Upload feature spec .md files (excluding hot_memory.md)
    md_files = sorted(glob.glob(os.path.join(SEED_DIR, "*.md")))
    feature_mds = [f for f in md_files if os.path.basename(f) != "hot_memory.md"]

    if feature_mds:
        print(f"--- Uploading {len(feature_mds)} Feature Spec(s) ---")
        for md_path in feature_mds:
            spec_id = os.path.splitext(os.path.basename(md_path))[0]
            s3_key = f"features/{spec_id}.md"
            upload_file(s3, md_path, s3_key, "text/markdown", dry_run)
    else:
        print("No feature spec .md files found.")
    print()

    # 3. Upload .meta.json files
    meta_files = sorted(glob.glob(os.path.join(SEED_DIR, "*.meta.json")))

    if meta_files:
        print(f"--- Uploading {len(meta_files)} Metadata File(s) ---")
        for meta_path in meta_files:
            basename = os.path.basename(meta_path)
            # e.g. RESUME-TAILOR-PIPELINE.meta.json → RESUME-TAILOR-PIPELINE
            spec_id = basename.replace(".meta.json", "")
            s3_key = f"metadata/{spec_id}.meta.json"
            # Validate JSON before uploading
            try:
                with open(meta_path, "r") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                print(f"  ERROR: Invalid JSON in {basename}: {e}")
                continue
            upload_file(s3, meta_path, s3_key, "application/json", dry_run)
    else:
        print("No .meta.json files found.")
    print()

    # 4. Summary
    print("=== Summary ===")
    print(f"Seed directory: {SEED_DIR}")
    all_uploads = (1 if os.path.exists(hot_memory_path) else 0) + len(feature_mds) + len(meta_files)
    print(f"Total files {'that would be' if dry_run else ''} uploaded: {all_uploads}")

    if not dry_run:
        print("\n✅ MinIO seeding complete!")
        print("You can now query the CCKB server about:")
        for md_path in feature_mds:
            spec_id = os.path.splitext(os.path.basename(md_path))[0]
            print(f"  - {spec_id}")
    else:
        print("\nRun without --dry-run to apply changes.")

if __name__ == "__main__":
    main()
