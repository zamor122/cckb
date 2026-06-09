#!/usr/bin/env python3
import os
import sys
import re
import ast
import json
import time
import subprocess
import argparse
import boto3
import botocore.exceptions

# CCKB auto-spec generation (optional — does not break crawler if missing)
try:
    from codebase_scanner import CodebaseScanner
    _SCANNER_AVAILABLE = True
except ImportError:
    _SCANNER_AVAILABLE = False

# Helper to load CCKB env variables
def load_cckb_env():
    env_path = ".cckb/.env"
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v
    return env_vars

# Logging helper
def log_message(msg):
    log_dir = ".cckb/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "crawler.log")
    with open(log_file, "a") as f:
        f.write(msg + "\n")

# AST Extractor
def extract_functions(source_code):
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        raise
    
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # Create a name-agnostic dump to check body similarity
            node_copy = ast.FunctionDef(
                name="dummy",
                args=node.args,
                body=node.body,
                decorator_list=node.decorator_list,
                returns=node.returns
            )
            functions[node.name] = ast.dump(node_copy)
    return functions

# AST semantic rename matcher
def detect_renames(old_source, new_source):
    try:
        old_funcs = extract_functions(old_source)
        new_funcs = extract_functions(new_source)
    except SyntaxError:
        raise
        
    removed = {name: dump for name, dump in old_funcs.items() if name not in new_funcs}
    added = {name: dump for name, dump in new_funcs.items() if name not in old_funcs}
    
    renames = {}
    
    # 1. Match by exact body dump
    for r_name, r_dump in list(removed.items()):
        for a_name, a_dump in list(added.items()):
            if r_dump == a_dump:
                renames[r_name] = a_name
                removed.pop(r_name)
                added.pop(a_name)
                break
                
    # 2. Fallback: if there is exactly 1 removed and 1 added function left, assume it was renamed
    if len(removed) == 1 and len(added) == 1:
        r_name = list(removed.keys())[0]
        a_name = list(added.keys())[0]
        renames[r_name] = a_name
        
    return renames

# Regex fallback for SyntaxError
def detect_renames_fallback(old_source, new_source):
    old_names = set(re.findall(r'def\s+(\w+)\s*\(', old_source))
    new_names = set(re.findall(r'def\s+(\w+)\s*\(', new_source))
    
    removed = old_names - new_names
    added = new_names - old_names
    
    renames = {}
    if len(removed) == 1 and len(added) == 1:
        renames[list(removed)[0]] = list(added)[0]
    return renames

# S3 Helper
def get_s3_client():
    cckb_env = load_cckb_env()
    minio_user = cckb_env.get("MINIO_ROOT_USER") or os.getenv("MINIO_ROOT_USER", "minioadmin")
    minio_pass = cckb_env.get("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
    
    return boto3.client(
        's3',
        endpoint_url='http://localhost:9000',
        aws_access_key_id=minio_user,
        aws_secret_access_key=minio_pass,
        region_name='us-east-1'
    )

# Hook installer
def install_hook():
    hook_dir = ".git/hooks"
    if not os.path.isdir(hook_dir):
        print(f"Error: {hook_dir} directory not found. Is this a Git repository?", file=sys.stderr)
        return False
        
    hook_path = os.path.join(hook_dir, "post-commit")
    
    # Use dynamic absolute paths to ensure the hook can run from any repository directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(script_dir, ".venv", "bin", "python")
    crawler_path = os.path.join(script_dir, "crawler.py")
    
    hook_content = f"""#!/usr/bin/env bash
# Detached background execution to not block the developer
nohup {venv_python} {crawler_path} --post-commit >/dev/null 2>&1 &
exit 0
"""
    
    with open(hook_path, "w") as f:
        f.write(hook_content)
        
    os.chmod(hook_path, 0o755)
    print("Post-commit hook installed successfully.")
    return True

# Post-commit processing loop
def process_post_commit():
    log_message("Starting post-commit crawl...")
    
    # 1. Identify changed files
    try:
        # Check if we have commits in the repository
        subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        log_message("No commits found in repo.")
        return
        
    try:
        diff_output = subprocess.check_output(
            ["git", "diff-tree", "-r", "--name-only", "--no-commit-id", "HEAD"],
            stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='replace').splitlines()
    except subprocess.CalledProcessError as e:
        log_message(f"Error running git diff-tree: {e}")
        return
        
    # 2. Check for large refactor
    if len(diff_output) > 50:
        os.environ['GRAPHIFY_FORCE'] = 'true'
        log_message(f"Large refactor detected ({len(diff_output)} files). Setting GRAPHIFY_FORCE=true and deferring to background queue.")
        return
        
    python_files = [f for f in diff_output if f.endswith(".py") and os.path.exists(f)]
    if python_files:
        # Check if HEAD~1 exists
        has_parent = True
        try:
            subprocess.check_output(["git", "rev-parse", "HEAD~1"], stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            has_parent = False
            
        renamed_map = {} # maps old function name to new function name
        
        for filename in python_files:
            # Get new source (from HEAD or filesystem)
            try:
                new_source = subprocess.check_output(
                    ["git", "show", f"HEAD:{filename}"],
                    stderr=subprocess.DEVNULL
                ).decode('utf-8', errors='replace')
            except subprocess.CalledProcessError:
                # Fallback to reading file directly
                with open(filename, "r", encoding="utf-8", errors="replace") as f:
                    new_source = f.read()
                    
            # Get old source
            if has_parent:
                try:
                    old_source = subprocess.check_output(
                        ["git", "show", f"HEAD~1:{filename}"],
                        stderr=subprocess.DEVNULL
                    ).decode('utf-8', errors='replace')
                except subprocess.CalledProcessError:
                    # File was newly added in HEAD, so it didn't exist in HEAD~1
                    continue
            else:
                # First commit, no parent
                continue
                
            # Analyze AST
            try:
                file_renames = detect_renames(old_source, new_source)
            except SyntaxError as e:
                # Handle syntax errors gracefully
                log_message(f"SyntaxError in {filename}: {str(e)}. Falling back to regex string matching.")
                file_renames = detect_renames_fallback(old_source, new_source)
                
            if file_renames:
                # Keep track of file context if needed, but key rename maps the names globally for now
                renamed_map.update(file_renames)
                
        if not renamed_map:
            log_message("No semantic rename operations detected.")
        else:
            log_message(f"Detected function renames: {renamed_map}")

            # 3. MinIO self-healing loop (existing logic)
            try:
                s3 = get_s3_client()
            except Exception as e:
                log_message(f"Error initializing boto3 client: {e}")
                return

            try:
                response = s3.list_objects_v2(Bucket='cckb-data', Prefix='metadata/')
            except botocore.exceptions.ConnectionError as e:
                log_message(f"MinIO connection error: {e}")
                return
            except Exception as e:
                log_message(f"Error listing metadata bucket: {e}")
                return

            if 'Contents' in response:
                for item in response['Contents']:
                    key = item['Key']
                    if not key.endswith(".meta.json"):
                        continue
                    try:
                        obj = s3.get_object(Bucket='cckb-data', Key=key)
                        meta_data = json.loads(obj['Body'].read().decode('utf-8'))
                        modified = False
                        if 'dependencies' in meta_data:
                            for dep in meta_data['dependencies']:
                                dep_name = dep.get('name')
                                if dep_name in renamed_map:
                                    new_name = renamed_map[dep_name]
                                    log_message(f"Self-healing {key}: '{dep_name}' -> '{new_name}'")
                                    dep['name'] = new_name
                                    modified = True
                        if modified:
                            s3.put_object(
                                Bucket='cckb-data',
                                Key=key,
                                Body=json.dumps(meta_data, indent=2).encode('utf-8'),
                                ContentType='application/json'
                            )
                            log_message(f"Self-healed and updated {key}")
                    except Exception as e:
                        log_message(f"Error processing metadata file {key}: {e}")
            else:
                log_message("No .meta.json files found in metadata/ directory.")
    else:
        log_message("No python files modified. Skipping Phase 1 AST self-healing.")

    # ── Phase 2: Feature spec regeneration ──────────────────────────────────
    if not _SCANNER_AVAILABLE:
        log_message("Phase 2 skipped: codebase_scanner.py not available.")
        return

    if not diff_output:
        return

    # Normalise changed file paths to repo-relative forward-slash format
    changed_relative = [
        f.replace(os.sep, "/")
        for f in diff_output
        if not f.startswith(".")
    ]

    try:
        scanner = CodebaseScanner(os.getcwd(), max_domains=10)
    except Exception as e:
        log_message(f"Phase 2: Failed to initialise CodebaseScanner: {e}")
        return

    # 2a. Find which existing specs are impacted by changed files
    impacted_spec_ids = scanner.find_specs_for_files(changed_relative)
    log_message(f"Phase 2: Impacted specs for changed files: {impacted_spec_ids}")

    # 2b. Regenerate impacted specs (with throttle)
    regen_ts_path = os.path.join(".cckb", "regen_timestamps.json")
    regen_timestamps: dict = {}
    if os.path.exists(regen_ts_path):
        try:
            with open(regen_ts_path) as f:
                regen_timestamps = json.load(f)
        except Exception:
            regen_timestamps = {}

    now = time.time()
    THROTTLE_SECONDS = 300  # 5 minutes

    for spec_id in impacted_spec_ids:
        last_regen = regen_timestamps.get(spec_id, 0)
        if now - last_regen < THROTTLE_SECONDS:
            log_message(f"Phase 2: Throttled {spec_id} (regenerated {int(now - last_regen)}s ago).")
            continue

        # Find the domain definition for this spec_id
        domains = scanner.discover_feature_domains()
        domain = next((d for d in domains if d["spec_id"] == spec_id), None)
        if not domain:
            log_message(f"Phase 2: Domain not found for spec_id={spec_id}, skipping.")
            continue

        try:
            log_message(f"Phase 2: Regenerating {spec_id}...")
            spec_md, spec_meta = scanner.generate_spec(domain, use_llm=True)
            success = scanner.upload_spec(spec_id, spec_md, spec_meta)
            if success:
                regen_timestamps[spec_id] = now
                log_message(f"Phase 2: Regenerated and uploaded {spec_id}.")
            else:
                log_message(f"Phase 2: Upload failed for {spec_id}.")
        except Exception as e:
            log_message(f"Phase 2: Error regenerating {spec_id}: {e}")

    # 2c. New-feature detection — check for changed files not covered by any existing spec
    covered_files: set = set()
    for spec_id in [d["spec_id"] for d in scanner.discover_feature_domains()]:
        covered_files.update(scanner.find_specs_for_files([]))  # placeholder

    # Simpler approach: find changed files in route/api/service dirs with no existing spec
    try:
        s3 = get_s3_client()
        existing_keys_resp = s3.list_objects_v2(Bucket='cckb-data', Prefix='features/')
        existing_spec_ids = set()
        for item in existing_keys_resp.get('Contents', []):
            k = item['Key']  # e.g. features/HUMANIZE.md
            if k.endswith(".md"):
                existing_spec_ids.add(k.split("/")[-1].replace(".md", ""))
    except Exception:
        existing_spec_ids = set()

    all_domains = scanner.discover_feature_domains()
    known_spec_ids = {d["spec_id"] for d in all_domains}
    new_domains = [d for d in all_domains if d["spec_id"] not in existing_spec_ids]

    for domain in new_domains:
        spec_id = domain["spec_id"]
        # Check if any of this domain's entry files were among the changed files
        domain_files = set(domain["entry_files"])
        if not domain_files.intersection(set(changed_relative)):
            continue  # Not triggered by this commit
        try:
            log_message(f"Phase 2: New domain detected — creating spec for {spec_id}")
            spec_md, spec_meta = scanner.generate_spec(domain, use_llm=True)
            success = scanner.upload_spec(spec_id, spec_md, spec_meta)
            if success:
                regen_timestamps[spec_id] = now
                log_message(f"Phase 2: Created new spec {spec_id}.")
        except Exception as e:
            log_message(f"Phase 2: Error creating new spec {spec_id}: {e}")

    # Persist updated regen timestamps
    try:
        os.makedirs(".cckb", exist_ok=True)
        with open(regen_ts_path, "w") as f:
            json.dump(regen_timestamps, f, indent=2)
    except Exception as e:
        log_message(f"Phase 2: Could not save regen_timestamps: {e}")

def main():
    parser = argparse.ArgumentParser(description="CCKB Maintenance Crawler & AST Self-Healer")
    parser.add_argument("--install", action="store_true", help="Install post-commit hook in the repository")
    parser.add_argument("--post-commit", action="store_true", help="Run the post-commit handler (detached/background)")
    
    args = parser.parse_args()
    
    if args.install:
        success = install_hook()
        sys.exit(0 if success else 1)
    elif args.post_commit:
        process_post_commit()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
