#!/usr/bin/env python3
"""
codebase_scanner.py - Discovers feature domains in a repository and generates
structured feature specs (Markdown + meta.json) for the CCKB knowledge base.

Supports: Next.js (App Router), FastAPI, Flask, and generic Python repos.
Uses Ollama for natural-language spec prose; falls back to static templates if offline.

Usage:
    from codebase_scanner import CodebaseScanner

    scanner = CodebaseScanner("/path/to/repo")
    domains = scanner.discover_feature_domains()
    for domain in domains:
        spec_md, spec_meta = scanner.generate_spec(domain)
        scanner.upload_spec(domain["spec_id"], spec_md, spec_meta)
"""

import os
import re
import ast
import json
import glob
import hashlib
import urllib.request
from pathlib import Path
from typing import Optional

import boto3
import botocore.exceptions

# ─────────────────────────────────────────────
# MinIO helpers
# ─────────────────────────────────────────────

def _load_cckb_env(repo_path: str) -> dict:
    env_path = os.path.join(repo_path, ".cckb", ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars


def _get_s3_client(repo_path: str):
    env = _load_cckb_env(repo_path)
    minio_user = env.get("MINIO_ROOT_USER") or os.getenv("MINIO_ROOT_USER", "minioadmin")
    minio_pass = env.get("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
    return boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        aws_access_key_id=minio_user,
        aws_secret_access_key=minio_pass,
        region_name="us-east-1",
    )


def _is_ollama_online() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────
# Spec ID normalisation
# ─────────────────────────────────────────────

def _to_spec_id(name: str) -> str:
    """Convert a human label to a SCREAMING-KEBAB-CASE spec ID."""
    name = re.sub(r"[^a-zA-Z0-9\s\-_]", "", name)
    name = re.sub(r"[\s_]+", "-", name.strip())
    return name.upper()


# ─────────────────────────────────────────────
# TypeScript / JavaScript import extractor
# ─────────────────────────────────────────────

def _extract_ts_imports(source: str) -> list[dict]:
    """
    Extract named + default imports from TypeScript/JavaScript source.
    Returns list of { "from": str, "names": [str] }.
    """
    imports = []
    # import { A, B } from "path"  or  import X from "path"
    pattern = re.compile(
        r'import\s+(?:(?:\{([^}]+)\}|(\w+))\s+from\s+)?["\']([^"\']+)["\']',
        re.MULTILINE,
    )
    for m in pattern.finditer(source):
        named_group = m.group(1)
        default_name = m.group(2)
        from_path = m.group(3)
        names = []
        if named_group:
            names = [n.strip().split(" as ")[0] for n in named_group.split(",") if n.strip()]
        if default_name:
            names.append(default_name)
        if from_path:
            imports.append({"from": from_path, "names": names})
    return imports


def _extract_ts_exports(source: str) -> list[str]:
    """Return exported function / const / class names from TS source."""
    names = []
    # export async function X or export function X
    for m in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)", source):
        names.append(m.group(1))
    # export const X =
    for m in re.finditer(r"export\s+const\s+(\w+)\s*[=:]", source):
        names.append(m.group(1))
    # export class X
    for m in re.finditer(r"export\s+class\s+(\w+)", source):
        names.append(m.group(1))
    return names


def _detect_auth_pattern_ts(source: str) -> Optional[str]:
    patterns = {
        "requireAuth + verifyUserIdMatch": "requireAuth" in source and "verifyUserIdMatch" in source,
        "requireAuth": "requireAuth" in source,
        "getServerSession": "getServerSession" in source,
        "auth()": bool(re.search(r"\bauth\(\)", source)),
        "verifyToken": "verifyToken" in source,
    }
    for label, matched in patterns.items():
        if matched:
            return label
    return None


def _detect_http_methods_ts(source: str) -> list[str]:
    methods = []
    for method in ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]:
        if re.search(rf"export\s+async\s+function\s+{method}\b", source):
            methods.append(method)
    return methods


# ─────────────────────────────────────────────
# Python import / function extractor
# ─────────────────────────────────────────────

def _extract_py_imports(source: str) -> list[dict]:
    imports = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({"from": alias.name, "names": [alias.asname or alias.name]})
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
                imports.append({"from": module, "names": names})
    except SyntaxError:
        # Regex fallback
        for m in re.finditer(r"^from\s+([\w.]+)\s+import\s+(.+)$", source, re.MULTILINE):
            names = [n.strip() for n in m.group(2).split(",")]
            imports.append({"from": m.group(1), "names": names})
        for m in re.finditer(r"^import\s+([\w.]+)", source, re.MULTILINE):
            imports.append({"from": m.group(1), "names": [m.group(1)]})
    return imports


def _extract_py_functions(source: str) -> list[str]:
    names = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
    except SyntaxError:
        for m in re.finditer(r"^(?:async\s+)?def\s+(\w+)\s*\(", source, re.MULTILINE):
            names.append(m.group(1))
    return names


def _detect_auth_pattern_py(source: str) -> Optional[str]:
    patterns = {
        "Depends(get_current_user)": "get_current_user" in source and "Depends" in source,
        "login_required": "login_required" in source,
        "jwt_required": "jwt_required" in source,
        "verify_token": "verify_token" in source,
        "requireAuth": "requireAuth" in source,
    }
    for label, matched in patterns.items():
        if matched:
            return label
    return None


# ─────────────────────────────────────────────
# Dependency classifier
# ─────────────────────────────────────────────

def _classify_dependency(import_path: str, names: list[str], framework: str) -> dict:
    """Return a dependency dict with type, name, and (inferred) file."""
    # Internal path aliases
    if import_path.startswith("@/") or import_path.startswith("./") or import_path.startswith("../"):
        # Map @/app/utils/auth → src/app/utils/auth.ts
        file_path = import_path.replace("@/", "src/").lstrip("./")
        # Try to guess extension
        for ext in [".ts", ".tsx", ".js", ".jsx", ".py"]:
            candidate = file_path + ext
            if os.path.exists(candidate):
                file_path = candidate
                break
        # Classify type
        dep_type = "util"
        if "/api/" in file_path:
            dep_type = "api_route"
        elif "/service" in file_path or "/provider" in file_path:
            dep_type = "service"
        elif "/config" in file_path:
            dep_type = "config"
        elif "/lib/" in file_path or "/supabase" in file_path:
            dep_type = "database"
        elif "/prompt" in file_path:
            dep_type = "prompt"
        elif "/type" in file_path:
            dep_type = "type"
        elif "/hook" in file_path:
            dep_type = "hook"
        elif "/component" in file_path:
            dep_type = "component"
        return {
            "type": dep_type,
            "name": ", ".join(names[:3]) if names else import_path,
            "file": file_path,
        }
    else:
        return {
            "type": "external_package",
            "name": import_path,
            "file": "",
        }


# ─────────────────────────────────────────────
# Codebase-Agnostic Helpers
# ─────────────────────────────────────────────

def _extract_generic_imports(source: str, file_ext: str) -> list[dict]:
    """
    Extract imports/requires/uses/includes from source file based on extension.
    Returns list of { "from": str, "names": [str] }.
    """
    imports = []
    file_ext = file_ext.lower()
    
    # 1. TypeScript / JavaScript (.ts, .tsx, .js, .jsx)
    if file_ext in (".ts", ".tsx", ".js", ".jsx"):
        pattern = re.compile(
            r'import\s+(?:(?:\{([^}]+)\}|(\w+))\s+from\s+)?["\']([^"\']+)["\']',
            re.MULTILINE
        )
        for m in pattern.finditer(source):
            named_group = m.group(1)
            default_name = m.group(2)
            from_path = m.group(3)
            names = []
            if named_group:
                names = [n.strip().split(" as ")[0] for n in named_group.split(",") if n.strip()]
            if default_name:
                names.append(default_name)
            if from_path:
                imports.append({"from": from_path, "names": names})
        
        # Also catch require("path")
        require_pattern = re.compile(r'require\s*\(\s*["\']([^"\']+)["\']\s*\)')
        for m in require_pattern.finditer(source):
            from_path = m.group(1)
            imports.append({"from": from_path, "names": []})

    # 2. Python (.py)
    elif file_ext == ".py":
        # from module import name
        from_import = re.compile(r'^\s*from\s+([\w\.]+)\s+import\s+(.+)$', re.MULTILINE)
        for m in from_import.finditer(source):
            names = [n.strip() for n in m.group(2).split(",")]
            imports.append({"from": m.group(1), "names": names})
        # import module
        plain_import = re.compile(r'^\s*import\s+([\w\.]+)', re.MULTILINE)
        for m in plain_import.finditer(source):
            imports.append({"from": m.group(1), "names": [m.group(1)]})

    # 3. Java / Kotlin (.java, .kt)
    elif file_ext in (".java", ".kt"):
        # import package.Class;
        java_import = re.compile(r'^\s*import\s+([\w\.]+);', re.MULTILINE)
        for m in java_import.finditer(source):
            path = m.group(1)
            # Use class name as import name
            name = path.split(".")[-1]
            imports.append({"from": path, "names": [name]})

    # 4. Rust (.rs)
    elif file_ext == ".rs":
        # use path::to::Module; or use path::to::{A, B};
        rust_use = re.compile(r'^\s*use\s+([\w\::\*\{\},\s]+);', re.MULTILINE)
        for m in rust_use.finditer(source):
            path_raw = m.group(1).strip()
            path = path_raw.replace(" ", "")
            name = path.split("::")[-1]
            imports.append({"from": path, "names": [name]})
            
        # mod module_name;
        rust_mod = re.compile(r'^\s*mod\s+(\w+);', re.MULTILINE)
        for m in rust_mod.finditer(source):
            name = m.group(1)
            imports.append({"from": f"./{name}", "names": [name]})

    # 5. Ruby (.rb)
    elif file_ext == ".rb":
        # require 'path' or require_relative 'path'
        ruby_req = re.compile(r'(?:require|require_relative)\s+["\']([^"\']+)["\']')
        for m in ruby_req.finditer(source):
            path = m.group(1)
            imports.append({"from": path, "names": [os.path.basename(path)]})

    # 6. Go (.go)
    elif file_ext == ".go":
        # single line: import "path"
        go_single = re.compile(r'import\s+"([^"]+)"')
        for m in go_single.finditer(source):
            path = m.group(1)
            imports.append({"from": path, "names": [path.split("/")[-1]]})
        # multi line: import ( ... )
        go_multi = re.compile(r'import\s+\(\s*([\s\S]*?)\s*\)')
        for m in go_multi.finditer(source):
            lines = m.group(1).splitlines()
            for line in lines:
                line = line.strip().strip('"')
                if line:
                    imports.append({"from": line, "names": [line.split("/")[-1]]})

    # 7. C# (.cs)
    elif file_ext == ".cs":
        # using Namespace;
        cs_using = re.compile(r'^\s*using\s+([\w\.]+);', re.MULTILINE)
        for m in cs_using.finditer(source):
            path = m.group(1)
            imports.append({"from": path, "names": [path.split(".")[-1]]})

    # 8. C / C++ (.c, .cpp, .h, .hpp)
    elif file_ext in (".c", ".cpp", ".h", ".hpp"):
        # #include "path" or #include <path>
        cpp_include = re.compile(r'^\s*#include\s+["<]([^">]+)[">]', re.MULTILINE)
        for m in cpp_include.finditer(source):
            path = m.group(1)
            imports.append({"from": path, "names": [os.path.basename(path)]})

    return imports


def _detect_http_methods_generic(source: str) -> list[str]:
    """
    Codebase-agnostic HTTP method detector.
    """
    methods = []
    for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
        patterns = [
            rf"\b{method}\b",
            rf"@{method}Mapping",
            rf"#\[{method.lower()}\(",
            rf"\.{method}\(",
        ]
        for pattern in patterns:
            if re.search(pattern, source, re.IGNORECASE if "Mapping" in pattern else 0):
                methods.append(method)
                break
    return methods


def _detect_auth_pattern_generic(source: str) -> Optional[str]:
    """
    Codebase-agnostic Auth pattern detector.
    """
    patterns = {
        "Depends(get_current_user)": "get_current_user" in source and "Depends" in source,
        "requireAuth + verifyUserIdMatch": "requireAuth" in source and "verifyUserIdMatch" in source,
        "requireAuth": "requireAuth" in source,
        "getServerSession": "getServerSession" in source,
        "auth()": bool(re.search(r"\bauth\(\)", source)),
        "login_required": "login_required" in source,
        "jwt_required": "jwt_required" in source,
        "verifyToken": "verifyToken" in source,
        "authorize": "authorize" in source or "authorized" in source,
        "authenticate": "authenticate" in source or "authenticated" in source,
    }
    for label, matched in patterns.items():
        if matched:
            return label
    return None


def _classify_dependency_generic(import_path: str, names: list[str], repo_path: str) -> dict:
    """
    Codebase-agnostic dependency classifier.
    Checks if the imported path maps to a local file in the repository.
    """
    clean_path = import_path.replace("@/", "src/").lstrip("./").lstrip("../")
    
    # Try direct file match
    for ext in [".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".rs", ".rb", ".go", ".cs", ".cpp", ".h"]:
        candidate = os.path.join(repo_path, clean_path + ext)
        if os.path.exists(candidate):
            return {
                "type": "util" if "/util" in clean_path else "service",
                "name": ", ".join(names) if names else import_path,
                "file": os.path.relpath(candidate, repo_path),
            }

    # Namespace match (e.g. crate::db::connection or com.example.service.AuthService)
    base_name = import_path.split("::")[-1].split(".")[-1].split("/")[-1].strip("*")
    if base_name:
        matches = []
        for ext in [".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".rs", ".rb", ".go", ".cs", ".cpp", ".h"]:
            pattern = os.path.join(repo_path, "**", f"{base_name}{ext}")
            for p in glob.glob(pattern, recursive=True):
                if not any(ignored in p for ignored in ["/node_modules/", "/.venv/", "/target/", "/bin/", "/obj/", "/dist/", "/build/", "/__pycache__/"]):
                    matches.append(p)
                    break
        
        if matches:
            local_file = matches[0]
            rel_file = os.path.relpath(local_file, repo_path)
            
            dep_type = "service"
            rel_lower = rel_file.lower()
            if "util" in rel_lower or "helper" in rel_lower:
                dep_type = "util"
            elif "controller" in rel_lower or "route" in rel_lower or "handler" in rel_lower or "api" in rel_lower:
                dep_type = "api_route"
            elif "model" in rel_lower or "schema" in rel_lower or "db" in rel_lower or "entity" in rel_lower:
                dep_type = "database"
            elif "config" in rel_lower:
                dep_type = "config"
                
            return {
                "type": dep_type,
                "name": base_name,
                "file": rel_file,
            }
            
    return {
        "type": "external_package",
        "name": import_path,
        "file": "",
    }


# ─────────────────────────────────────────────
# Main Scanner Class
# ─────────────────────────────────────────────

class CodebaseScanner:
    """
    Discovers feature domains in a repository and generates CCKB feature specs.

    Parameters
    ----------
    repo_path : str
        Absolute path to the repository root.
    max_domains : int
        Maximum number of feature domains to discover (default 10).
    """

    BUCKET = "cckb-data"

    def __init__(self, repo_path: str, max_domains: int = 10):
        self.repo_path = os.path.abspath(repo_path)
        self.max_domains = max_domains
        self.framework = self._detect_framework()

    # ── Framework detection ──────────────────

    def _detect_framework(self) -> str:
        root = self.repo_path
        if os.path.exists(os.path.join(root, "next.config.ts")) or \
           os.path.exists(os.path.join(root, "next.config.js")) or \
           os.path.exists(os.path.join(root, "next.config.mjs")):
            return "nextjs"
        if os.path.exists(os.path.join(root, "package.json")):
            try:
                with open(os.path.join(root, "package.json")) as f:
                    pkg = json.load(f)
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    if "next" in deps:
                        return "nextjs"
            except Exception:
                pass
            return "nodejs"
        if os.path.exists(os.path.join(root, "requirements.txt")) or \
           os.path.exists(os.path.join(root, "pyproject.toml")):
            # Check for FastAPI or Flask
            for req_file in ["requirements.txt", "pyproject.toml"]:
                req_path = os.path.join(root, req_file)
                if os.path.exists(req_path):
                    with open(req_path) as f:
                        content = f.read().lower()
                        if "fastapi" in content:
                            return "fastapi"
                        if "flask" in content:
                            return "flask"
            return "python"
        return "generic"

    # ── Domain discovery ─────────────────────

    def _generate_tree_map(self) -> str:
        """
        Generate a tree-like text representation of the repository's files.
        Excludes build, dependencies, and cache directories.
        Caps output to 150 files.
        """
        ignore_dirs = {
            ".git", "node_modules", ".venv", "venv", ".cckb", "target", "bin",
            "obj", "out", "dist", "build", "gradle", ".idea", ".vscode", "__pycache__"
        }
        lines = []
        file_count = 0
        max_files = 150
        
        # Walk up to 3 levels deep
        for root, dirs, files in os.walk(self.repo_path):
            # Prune ignored directories in-place
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            
            # Check depth relative to repo_path
            depth = len(Path(root).relative_to(self.repo_path).parts)
            if depth > 3:
                continue
                
            indent = "  " * depth
            rel_root = os.path.relpath(root, self.repo_path)
            if rel_root != ".":
                lines.append(f"{indent}[Dir] {os.path.basename(root)}/")
                
            # Filter and sort files
            for file in sorted(files):
                if file.startswith(".") or file.endswith((".pyc", ".png", ".jpg", ".jpeg", ".ico", ".svg", ".zip", ".tar.gz", ".lock")):
                    continue
                if file_count >= max_files:
                    if file_count == max_files:
                        lines.append(f"{indent}  ... (max file limit reached)")
                        file_count += 1
                    continue
                
                lines.append(f"{indent}  - {file}")
                file_count += 1
                
        # Also check for key root config files and append their names/contents if small
        config_files = ["package.json", "Cargo.toml", "Gemfile", "go.mod", "pom.xml", "build.gradle", "requirements.txt"]
        config_lines = []
        for cf in config_files:
            cf_path = os.path.join(self.repo_path, cf)
            if os.path.exists(cf_path):
                config_lines.append(f"\n--- Root Configuration File: {cf} ---")
                try:
                    with open(cf_path, "r", errors="replace") as f:
                        content = f.read(2048) # Read first 2KB
                        config_lines.append(content)
                        if len(content) >= 2048:
                            config_lines.append("... [truncated]")
                except Exception as e:
                    config_lines.append(f"[Error reading file: {e}]")
                    
        return "\n".join(lines) + "\n" + "\n".join(config_lines)

    def _discover_domains_with_llm(self) -> list[dict]:
        """
        Use Ollama to dynamically analyze the repo structure and config files,
        returning a list of discovered domains and their entry points.
        """
        if not _is_ollama_online():
            return []
            
        try:
            from langchain_ollama import OllamaLLM
        except ImportError:
            return []
            
        tree_map = self._generate_tree_map()
        
        prompt = f"""You are a senior system architect analyzing a cloned repository to map its logical feature domains and entry points.
Below is the directory structure and main configuration files of the repository:

{tree_map}

Based on this, identify up to {self.max_domains} main feature domains/routes/services.
For each domain, define:
1. `spec_id`: A SCREAMING-KEBAB-CASE unique identifier (e.g. USER-AUTHENTICATION, BILLING-SYSTEM).
2. `display_name`: A clear, clean title name (e.g. User Authentication, Billing System).
3. `entry_files`: A list of relative paths (from repo root) to the actual files in the directory structure that act as entry points.
4. `related_files`: A list of relative paths (from repo root) to the actual files in the directory structure implementing business logic, models, or services.
5. `framework`: Inferred framework name (e.g. Spring Boot, Rails, Actix, Express, Next.js, or "none").
6. `language`: Primary programming language (e.g. Java, Ruby, Rust, Go, Python, TypeScript).

WARNING: Only output relative file paths that are EXACTLY present in the directory structure listed above. Do not invent files or copy the placeholder paths from the example.

Respond strictly with a JSON array containing these objects. Do not include markdown formatting like ```json or any other text before/after the JSON.
Example Response format:
[
  {{
    "spec_id": "SPEC-ID-HERE",
    "display_name": "Clean Display Name",
    "entry_files": ["path/to/actual/entry/file.ext"],
    "related_files": ["path/to/actual/related/file.ext"],
    "framework": "FrameworkName",
    "language": "LanguageName"
  }}
]
"""
        try:
            llm = OllamaLLM(model="llama3.2:3b", base_url="http://127.0.0.1:11434", timeout=45)
            response_text = llm.invoke(prompt).strip()
            
            # Clean response text in case LLM outputs markdown code blocks
            cleaned_text = response_text
            if "```json" in cleaned_text:
                cleaned_text = cleaned_text.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_text:
                cleaned_text = cleaned_text.split("```")[1].split("```")[0].strip()
                
            domains = json.loads(cleaned_text)
            
            # Basic validation of elements with self-healing path resolution
            validated_domains = []
            for d in domains:
                if isinstance(d, dict) and "spec_id" in d and "entry_files" in d:
                    entry_files = []
                    for f in d.get("entry_files", []):
                        if not isinstance(f, str):
                            continue
                        f = f.strip().replace("\\", "/")
                        if os.path.exists(os.path.join(self.repo_path, f)):
                            entry_files.append(f)
                        else:
                            # Search recursively for the filename in case the LLM guessed the path structure wrong
                            base = os.path.basename(f)
                            matches = glob.glob(os.path.join(self.repo_path, "**", base), recursive=True)
                            valid_matches = [
                                os.path.relpath(p, self.repo_path)
                                for p in matches
                                if not any(ignored in p for ignored in ["/node_modules/", "/.venv/", "/target/", "/bin/", "/obj/", "/dist/", "/build/", "/__pycache__/", "/.git/"])
                            ]
                            if valid_matches:
                                entry_files.append(valid_matches[0])
                                
                    related_files = []
                    for f in d.get("related_files", []):
                        if not isinstance(f, str):
                            continue
                        f = f.strip().replace("\\", "/")
                        if os.path.exists(os.path.join(self.repo_path, f)):
                            related_files.append(f)
                        else:
                            base = os.path.basename(f)
                            matches = glob.glob(os.path.join(self.repo_path, "**", base), recursive=True)
                            valid_matches = [
                                os.path.relpath(p, self.repo_path)
                                for p in matches
                                if not any(ignored in p for ignored in ["/node_modules/", "/.venv/", "/target/", "/bin/", "/obj/", "/dist/", "/build/", "/__pycache__/", "/.git/"])
                            ]
                            if valid_matches:
                                related_files.append(valid_matches[0])
                                
                    if entry_files:
                        d["entry_files"] = entry_files
                        d["related_files"] = related_files
                        d["framework"] = d.get("framework") or self.framework
                        validated_domains.append(d)
                        
            return validated_domains[:self.max_domains]
        except Exception as e:
            print(f"  [CodebaseScanner] Warning: LLM domain discovery failed or returned invalid JSON: {e}", flush=True)
            return []

    def discover_feature_domains(self) -> list[dict]:
        """
        Discover feature domains in the repository.
        First attempts dynamic LLM-driven discovery. If offline/fails,
        falls back to static framework heuristics.
        """
        # Try dynamic LLM discovery first
        domains = self._discover_domains_with_llm()
        if domains:
            return domains

        # Fallback to static rules
        if self.framework == "nextjs":
            return self._discover_nextjs_domains()
        elif self.framework in ("fastapi", "flask", "python"):
            return self._discover_python_domains()
        else:
            return self._discover_generic_domains()

    def _discover_nextjs_domains(self) -> list[dict]:
        domains = []
        api_base = os.path.join(self.repo_path, "src", "app", "api")

        if not os.path.isdir(api_base):
            # Try without src/
            api_base = os.path.join(self.repo_path, "app", "api")

        if os.path.isdir(api_base):
            # Each subdirectory of /api that contains a route.ts is a domain
            for entry in sorted(os.scandir(api_base), key=lambda e: e.name):
                if not entry.is_dir():
                    continue
                group_name = entry.name
                # Find route files (may be nested)
                route_files = glob.glob(
                    os.path.join(entry.path, "**", "route.ts"), recursive=True
                ) + glob.glob(os.path.join(entry.path, "**", "route.tsx"), recursive=True)

                if not route_files:
                    continue

                entry_files = [os.path.relpath(f, self.repo_path) for f in route_files]
                related_files = self._find_related_files_ts(route_files)

                spec_id = _to_spec_id(group_name)
                domains.append({
                    "spec_id": spec_id,
                    "display_name": group_name.replace("-", " ").replace("_", " ").title(),
                    "entry_files": entry_files,
                    "related_files": related_files,
                    "framework": self.framework,
                })

                if len(domains) >= self.max_domains:
                    break

        # Also add a domain for the services layer if it exists
        services_path = os.path.join(self.repo_path, "src", "app", "services")
        if os.path.isdir(services_path) and len(domains) < self.max_domains:
            service_files = glob.glob(os.path.join(services_path, "**", "*.ts"), recursive=True)
            if service_files:
                entry_files = [os.path.relpath(f, self.repo_path) for f in service_files[:5]]
                domains.append({
                    "spec_id": "AI-SERVICES",
                    "display_name": "AI Services Layer",
                    "entry_files": entry_files,
                    "related_files": [],
                    "framework": self.framework,
                })

        # Config domain
        config_path = os.path.join(self.repo_path, "src", "app", "config")
        if os.path.isdir(config_path) and len(domains) < self.max_domains:
            config_files = glob.glob(os.path.join(config_path, "*.ts"))
            if config_files:
                entry_files = [os.path.relpath(f, self.repo_path) for f in config_files]
                domains.append({
                    "spec_id": "APP-CONFIG",
                    "display_name": "App Configuration",
                    "entry_files": entry_files,
                    "related_files": [],
                    "framework": self.framework,
                })

        return domains[: self.max_domains]

    def _discover_python_domains(self) -> list[dict]:
        domains = []
        root = self.repo_path

        # Look for router files / blueprint files
        py_files = glob.glob(os.path.join(root, "**", "*.py"), recursive=True)
        py_files = [f for f in py_files if ".venv" not in f and "__pycache__" not in f]

        # Heuristic: files named routes.py, router.py, views.py, api.py, handlers.py
        route_keywords = {"routes", "router", "views", "api", "handlers", "endpoints"}
        route_files = [
            f for f in py_files
            if os.path.splitext(os.path.basename(f))[0].lower() in route_keywords
        ]

        if not route_files:
            # Fall back: any .py file in a routes/ or api/ directory
            route_files = [
                f for f in py_files
                if any(seg in f.split(os.sep) for seg in ["routes", "api", "views", "handlers"])
            ]

        for rf in sorted(route_files)[: self.max_domains]:
            rel = os.path.relpath(rf, root)
            stem = os.path.splitext(os.path.basename(rf))[0]
            spec_id = _to_spec_id(stem)
            domains.append({
                "spec_id": spec_id,
                "display_name": stem.replace("_", " ").title(),
                "entry_files": [rel],
                "related_files": [],
                "framework": self.framework,
            })

        # If no route files found, treat each top-level module as a domain
        if not domains:
            top_py = [f for f in py_files if f.count(os.sep) - root.count(os.sep) <= 1]
            for tf in sorted(top_py)[: self.max_domains]:
                rel = os.path.relpath(tf, root)
                stem = os.path.splitext(os.path.basename(tf))[0]
                spec_id = _to_spec_id(stem)
                domains.append({
                    "spec_id": spec_id,
                    "display_name": stem.replace("_", " ").title(),
                    "entry_files": [rel],
                    "related_files": [],
                    "framework": self.framework,
                })

        return domains[: self.max_domains]

    def _discover_generic_domains(self) -> list[dict]:
        """Best-effort domain discovery for unknown frameworks."""
        domains = []
        root = self.repo_path
        # Top-level directories that contain source files
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__", ".venv", "dist", "build"}:
                continue
            src_files = glob.glob(os.path.join(entry.path, "**", "*.*"), recursive=True)
            src_files = [f for f in src_files if os.path.splitext(f)[1] in {".ts", ".tsx", ".js", ".py"}]
            if src_files:
                entry_files = [os.path.relpath(src_files[0], root)]
                spec_id = _to_spec_id(entry.name)
                domains.append({
                    "spec_id": spec_id,
                    "display_name": entry.name.replace("-", " ").replace("_", " ").title(),
                    "entry_files": entry_files,
                    "related_files": [],
                    "framework": self.framework,
                })
            if len(domains) >= self.max_domains:
                break
        return domains

    # ── Related file discovery ───────────────

    def _find_related_files_ts(self, entry_files: list[str]) -> list[str]:
        """
        Read entry files and follow internal @/ imports one level deep.
        Returns a de-duplicated, sorted list of relative paths.
        """
        related = set()
        for abs_path in entry_files:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue

            for imp in _extract_ts_imports(source):
                from_path = imp["from"]
                if from_path.startswith("@/"):
                    # Map to filesystem path
                    rel = from_path.replace("@/", "src/")
                    for ext in [".ts", ".tsx", ".js", ".jsx", ""]:
                        candidate = os.path.join(self.repo_path, rel + ext)
                        if os.path.exists(candidate):
                            related.add(os.path.relpath(candidate, self.repo_path))
                            break
        return sorted(related)

    # ── Dependency extraction ────────────────

    def extract_dependencies(self, domain: dict) -> list[dict]:
        """
        Read all entry + related files and extract structured dependencies.
        """
        all_files = domain["entry_files"] + domain["related_files"]
        seen = set()
        deps = []

        for rel_path in all_files:
            abs_path = os.path.join(self.repo_path, rel_path)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue

            ext = os.path.splitext(rel_path)[1]
            imports = _extract_generic_imports(source, ext)
            for imp in imports:
                dep = _classify_dependency_generic(imp["from"], imp["names"], self.repo_path)
                key = dep["file"] or dep["name"]
                if key not in seen:
                    seen.add(key)
                    deps.append(dep)

        # Add explicit entry point HTTP method routes if found
        for rel_path in domain["entry_files"]:
            abs_path = os.path.join(self.repo_path, rel_path)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue

            methods = _detect_http_methods_generic(source)
            for method in methods:
                key = f"HTTP:{method}:{rel_path}"
                if key not in seen:
                    seen.add(key)
                    deps.insert(0, {"type": "api_route", "name": f"{method} /{rel_path}", "file": rel_path})

        return deps[:30]  # cap at 30 to keep meta.json manageable

    # ── Static spec builder (no LLM) ────────

    def _build_static_spec(self, domain: dict, deps: list[dict]) -> str:
        """
        Build a spec markdown document using only static analysis.
        Produces all structured sections but no natural-language Overview prose.
        """
        spec_id = domain["spec_id"]
        display_name = domain["display_name"]
        entry_files = domain["entry_files"]
        related_files = domain["related_files"]

        lines = [
            f"# Feature Spec: {spec_id}",
            f"## Feature ID: {spec_id}",
            f"## Feature Name: {display_name}",
            "",
            "---",
            "",
            "## Overview",
            f"Auto-generated spec for the **{display_name}** feature domain.",
            f"Framework: `{domain['framework']}`",
            "",
            "---",
            "",
            "## Entry Points",
            "",
            "| File | Description |",
            "|---|---|",
        ]
        for ef in entry_files:
            lines.append(f"| `{ef}` | Entry point |")

        lines += [
            "",
            "---",
            "",
            "## Related Files",
            "",
        ]
        if related_files:
            for rf in related_files[:10]:
                lines.append(f"- `{rf}`")
        else:
            lines.append("_No related files detected._")

        lines += [
            "",
            "---",
            "",
            "## Key Dependencies",
            "",
            "| Type | Name | File |",
            "|---|---|---|",
        ]
        internal_deps = [d for d in deps if d.get("type") != "external_package"]
        for dep in internal_deps[:20]:
            lines.append(f"| {dep['type']} | {dep['name']} | `{dep['file']}` |")

        # Auth pattern detection
        auth_pattern = None
        for rel_path in entry_files:
            abs_path = os.path.join(self.repo_path, rel_path)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
                auth_pattern = _detect_auth_pattern_generic(source)
                if auth_pattern:
                    break
            except OSError:
                pass

        if auth_pattern:
            lines += [
                "",
                "---",
                "",
                "## Auth Pattern",
                "",
                f"Detected: `{auth_pattern}`",
            ]

        return "\n".join(lines) + "\n"

    # ── Ollama-assisted spec builder ─────────

    def _build_llm_spec(self, domain: dict, deps: list[dict], static_md: str) -> str:
        """
        Use Ollama to enhance the static spec with natural-language Overview
        and richer Processing Steps narrative.
        """
        try:
            from langchain_ollama import OllamaLLM  # type: ignore
        except ImportError:
            return static_md

        # Read code snippet from first entry file
        code_context = ""
        first_entry = ""
        if domain.get("entry_files"):
            first_entry = domain["entry_files"][0]
            abs_entry = os.path.join(self.repo_path, first_entry)
            if os.path.exists(abs_entry):
                try:
                    with open(abs_entry, "r", encoding="utf-8", errors="replace") as f:
                        lines = [f.readline() for _ in range(150)]
                        code_context = "\n--- Code Snippet (First 150 lines) ---\n" + "".join(lines)
                except Exception:
                    pass

        entry_list = "\n".join(f"  - {f}" for f in domain["entry_files"])
        dep_summary = "\n".join(
            f"  - [{d['type']}] {d['name']} → {d['file']}"
            for d in deps[:15]
            if d.get("type") != "external_package"
        )

        prompt = f"""You are a senior engineer writing an internal feature specification document.
Repository framework: {domain['framework']}
Feature domain: {domain['display_name']}

Entry files:
{entry_list}

Key dependencies:
{dep_summary}
{code_context}

Write a concise but thorough feature specification. Focus on:
1. A 2-3 sentence Overview describing what this feature does and its role in the system.
2. Processing Steps — numbered list of what happens when this feature is invoked (use file references like `{first_entry}`).
3. Any notable patterns (auth guards, caching, streaming, background tasks).

Format as Markdown. Be specific and technical. Do not add sections that aren't relevant.
Keep total length under 800 words."""

        try:
            llm = OllamaLLM(model="llama3.2:3b", base_url="http://127.0.0.1:11434", timeout=45)
            llm_content = llm.invoke(prompt)

            # Splice the LLM content into the static spec after the Overview header
            enhanced = static_md.replace(
                f"Auto-generated spec for the **{domain['display_name']}** feature domain.\nFramework: `{domain['framework']}`",
                llm_content.strip(),
            )
            return enhanced
        except Exception:
            return static_md

    # ── Public: generate_spec ────────────────

    def generate_spec(self, domain: dict, use_llm: bool = True) -> tuple[str, dict]:
        """
        Generate (spec_md, spec_meta) for a given domain.

        Parameters
        ----------
        domain  : dict returned by discover_feature_domains()
        use_llm : bool — attempt Ollama enhancement (falls back if offline)

        Returns
        -------
        spec_md   : str  — Markdown spec content
        spec_meta : dict — Metadata suitable for JSON serialisation
        """
        deps = self.extract_dependencies(domain)
        static_md = self._build_static_spec(domain, deps)

        if use_llm and _is_ollama_online():
            spec_md = self._build_llm_spec(domain, deps, static_md)
        else:
            spec_md = static_md

        # Build tags from dep types + framework
        tags = [domain["framework"]]
        type_counts: dict[str, int] = {}
        for d in deps:
            t = d.get("type", "")
            type_counts[t] = type_counts.get(t, 0) + 1
        tags += list(type_counts.keys())
        tags += [domain["display_name"].lower().replace(" ", "-")]

        spec_meta = {
            "spec_id": domain["spec_id"],
            "feature_name": domain["display_name"],
            "description": f"Auto-generated spec for {domain['display_name']} ({domain['framework']} framework).",
            "dependencies": [d for d in deps if d.get("type") != "external_package"],
            "entry_files": domain["entry_files"],
            "related_files": domain["related_files"],
            "framework": domain["framework"],
            "tags": tags,
            "auto_generated": True,
        }

        return spec_md, spec_meta

    # ── Public: upload_spec ──────────────────

    def upload_spec(self, spec_id: str, spec_md: str, spec_meta: dict) -> bool:
        """
        Upload spec_md and spec_meta to MinIO.
        Returns True on success, False on failure.
        """
        try:
            s3 = _get_s3_client(self.repo_path)
            s3.put_object(
                Bucket=self.BUCKET,
                Key=f"features/{spec_id}.md",
                Body=spec_md.encode("utf-8"),
                ContentType="text/markdown",
            )
            s3.put_object(
                Bucket=self.BUCKET,
                Key=f"metadata/{spec_id}.meta.json",
                Body=json.dumps(spec_meta, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
            return True
        except Exception as e:
            print(f"  [CodebaseScanner] Upload failed for {spec_id}: {e}", flush=True)
            return False

    # ── Public: find_specs_for_files ─────────

    def find_specs_for_files(self, changed_files: list[str]) -> list[str]:
        """
        Given a list of changed relative file paths, return spec IDs whose
        dependency maps reference any of those paths.
        Used by the crawler to determine which specs need regeneration.
        """
        try:
            s3 = _get_s3_client(self.repo_path)
            response = s3.list_objects_v2(Bucket=self.BUCKET, Prefix="metadata/")
        except Exception:
            return []

        impacted = []
        changed_set = set(changed_files)

        for item in response.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".meta.json"):
                continue
            try:
                obj = s3.get_object(Bucket=self.BUCKET, Key=key)
                meta = json.loads(obj["Body"].read().decode("utf-8"))
                # Check entry_files and dependency file fields
                tracked_files = set(meta.get("entry_files", []) + meta.get("related_files", []))
                for dep in meta.get("dependencies", []):
                    if dep.get("file"):
                        tracked_files.add(dep["file"])
                if tracked_files & changed_set:
                    impacted.append(meta["spec_id"])
            except Exception:
                continue

        return list(dict.fromkeys(impacted))  # deduplicated, order preserved


# ─────────────────────────────────────────────
# CLI entry point (for manual testing)
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CCKB Codebase Scanner")
    parser.add_argument("--repo", default=os.getcwd(), help="Path to repository root")
    parser.add_argument("--dry-run", action="store_true", help="Discover domains without uploading")
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama, use static templates only")
    args = parser.parse_args()

    scanner = CodebaseScanner(args.repo)
    print(f"Framework detected: {scanner.framework}")
    domains = scanner.discover_feature_domains()
    print(f"Discovered {len(domains)} feature domain(s):\n")

    for domain in domains:
        print(f"  [{domain['spec_id']}] {domain['display_name']}")
        print(f"    Entry files: {domain['entry_files'][:2]}")

        if not args.dry_run:
            print(f"    Generating spec...", end=" ", flush=True)
            spec_md, spec_meta = scanner.generate_spec(domain, use_llm=not args.no_llm)
            success = scanner.upload_spec(domain["spec_id"], spec_md, spec_meta)
            print("✓ uploaded" if success else "✗ failed")
        print()

    if args.dry_run:
        print("[DRY RUN] No uploads performed.")


if __name__ == "__main__":
    main()
