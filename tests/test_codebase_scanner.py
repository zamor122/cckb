#!/usr/bin/env python3
"""
tests/test_codebase_scanner.py

Unit tests for codebase_scanner.py.
Run with:  ../.venv/bin/pytest tests/test_codebase_scanner.py -v
"""

import os
import sys
import json
import tempfile
import shutil
import pytest

# Ensure the parent directory is on the path so we can import codebase_scanner
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codebase_scanner import (
    CodebaseScanner,
    _to_spec_id,
    _extract_ts_imports,
    _extract_ts_exports,
    _detect_auth_pattern_ts,
    _detect_http_methods_ts,
    _extract_py_imports,
    _extract_py_functions,
    _detect_auth_pattern_py,
    _classify_dependency,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def nextjs_repo(tmp_path):
    """Create a minimal Next.js repo structure for testing."""
    # next.config.ts (marks it as Next.js)
    (tmp_path / "next.config.ts").write_text("export default {};")

    # package.json
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14.0.0", "react": "18.0.0"}})
    )

    # src/app/api/tailor/route.ts
    tailor_dir = tmp_path / "src" / "app" / "api" / "tailor"
    tailor_dir.mkdir(parents=True)
    (tailor_dir / "route.ts").write_text("""
import { NextRequest, NextResponse } from "next/server";
import { requireAuth, verifyUserIdMatch } from "@/app/utils/auth";
import { generateWithFallback } from "@/app/services/model-fallback";

export const runtime = "nodejs";

export async function POST(req: NextRequest) {
  const authResult = await requireAuth(req, {});
  if ("error" in authResult) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  return NextResponse.json({ ok: true });
}
""")

    # src/app/api/auth/route.ts
    auth_dir = tmp_path / "src" / "app" / "api" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "route.ts").write_text("""
import { NextRequest, NextResponse } from "next/server";
import { getServerSession } from "next-auth";

export async function GET(req: NextRequest) {
  return NextResponse.json({ session: null });
}

export async function POST(req: NextRequest) {
  return NextResponse.json({ ok: true });
}
""")

    # src/app/utils/auth.ts
    utils_dir = tmp_path / "src" / "app" / "utils"
    utils_dir.mkdir(parents=True)
    (utils_dir / "auth.ts").write_text("""
export function requireAuth(req: any, opts: any) {
  return { userId: "user-123" };
}
export function verifyUserIdMatch(a: string, b: string) {
  return {};
}
""")

    # src/app/services/model-fallback.ts
    services_dir = tmp_path / "src" / "app" / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "model-fallback.ts").write_text("""
export async function generateWithFallback(prompt: string, model: string) {
  return { text: "result" };
}
""")

    # .cckb directory (for env)
    cckb_dir = tmp_path / ".cckb"
    cckb_dir.mkdir()
    (cckb_dir / ".env").write_text(
        "MINIO_ROOT_USER=minioadmin\nMINIO_ROOT_PASSWORD=minioadmin123\n"
    )

    return tmp_path


@pytest.fixture
def python_repo(tmp_path):
    """Create a minimal FastAPI repo structure for testing."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\nuvicorn==0.23.0\n")

    routes_dir = tmp_path / "routes"
    routes_dir.mkdir()
    (routes_dir / "auth.py").write_text("""
from fastapi import APIRouter, Depends
from utils.security import get_current_user

router = APIRouter()

@router.post("/login")
async def login(user=Depends(get_current_user)):
    return {"token": "abc"}
""")
    (routes_dir / "tailor.py").write_text("""
from fastapi import APIRouter
from services.llm import generate

router = APIRouter()

@router.post("/tailor")
async def tailor_resume(resume: str, job_description: str):
    result = await generate(resume, job_description)
    return {"tailored": result}
""")

    return tmp_path


# ─────────────────────────────────────────────
# Unit tests: helper functions
# ─────────────────────────────────────────────

class TestToSpecId:
    def test_simple(self):
        assert _to_spec_id("tailor") == "TAILOR"

    def test_kebab(self):
        assert _to_spec_id("resume-tailor") == "RESUME-TAILOR"

    def test_spaces(self):
        assert _to_spec_id("my feature name") == "MY-FEATURE-NAME"

    def test_special_chars_stripped(self):
        assert _to_spec_id("auth/sso!") == "AUTHSSO"


class TestTsImportExtractor:
    def test_named_imports(self):
        src = 'import { foo, bar } from "@/app/utils/auth";'
        imports = _extract_ts_imports(src)
        assert len(imports) == 1
        assert imports[0]["from"] == "@/app/utils/auth"
        assert "foo" in imports[0]["names"]
        assert "bar" in imports[0]["names"]

    def test_default_import(self):
        src = 'import NextAuth from "next-auth";'
        imports = _extract_ts_imports(src)
        assert len(imports) == 1
        assert imports[0]["from"] == "next-auth"
        assert "NextAuth" in imports[0]["names"]

    def test_multiple_imports(self):
        src = """
import { a } from "@/lib/db";
import { b, c } from "@/utils/helper";
import React from "react";
"""
        imports = _extract_ts_imports(src)
        assert len(imports) == 3

    def test_no_imports(self):
        assert _extract_ts_imports("const x = 1;") == []


class TestTsExportExtractor:
    def test_async_function(self):
        src = "export async function POST(req) { return; }"
        assert "POST" in _extract_ts_exports(src)

    def test_const_export(self):
        src = "export const runtime = 'nodejs';"
        assert "runtime" in _extract_ts_exports(src)

    def test_class_export(self):
        src = "export class MyService {}"
        assert "MyService" in _extract_ts_exports(src)


class TestAuthPatternDetection:
    def test_requires_auth_pattern(self):
        src = """
const authResult = await requireAuth(req, { accessToken });
const verifyResult = verifyUserIdMatch(authResult.userId, userId);
"""
        assert _detect_auth_pattern_ts(src) == "requireAuth + verifyUserIdMatch"

    def test_server_session(self):
        src = "const session = await getServerSession(authOptions);"
        assert _detect_auth_pattern_ts(src) == "getServerSession"

    def test_no_auth(self):
        src = "export async function GET() { return Response.json({}); }"
        assert _detect_auth_pattern_ts(src) is None

    def test_fastapi_depends(self):
        src = "async def endpoint(user=Depends(get_current_user)):"
        assert _detect_auth_pattern_py(src) == "Depends(get_current_user)"


class TestHttpMethodDetection:
    def test_post_only(self):
        src = "export async function POST(req: NextRequest) { return; }"
        assert _detect_http_methods_ts(src) == ["POST"]

    def test_get_and_post(self):
        src = """
export async function GET(req: NextRequest) { return; }
export async function POST(req: NextRequest) { return; }
"""
        methods = _detect_http_methods_ts(src)
        assert "GET" in methods
        assert "POST" in methods


class TestPyImportExtractor:
    def test_from_import(self):
        src = "from fastapi import APIRouter, Depends"
        imports = _extract_py_imports(src)
        froms = [i["from"] for i in imports]
        assert "fastapi" in froms

    def test_plain_import(self):
        src = "import os"
        imports = _extract_py_imports(src)
        froms = [i["from"] for i in imports]
        assert "os" in froms


class TestPyFunctionExtractor:
    def test_sync_function(self):
        src = "def my_func(a, b): pass"
        assert "my_func" in _extract_py_functions(src)

    def test_async_function(self):
        src = "async def handler(request): pass"
        assert "handler" in _extract_py_functions(src)


class TestClassifyDependency:
    def test_internal_util(self):
        dep = _classify_dependency("@/app/utils/auth", ["requireAuth"], "nextjs")
        assert dep["type"] == "util"
        assert "auth" in dep["file"]

    def test_internal_service(self):
        dep = _classify_dependency("@/app/services/model-fallback", ["generateWithFallback"], "nextjs")
        assert dep["type"] == "service"

    def test_internal_api(self):
        dep = _classify_dependency("@/app/api/mcp-tools/keyword-extractor", [], "nextjs")
        assert dep["type"] == "api_route"

    def test_external_package(self):
        dep = _classify_dependency("next-auth", ["getServerSession"], "nextjs")
        assert dep["type"] == "external_package"


# ─────────────────────────────────────────────
# Integration tests: CodebaseScanner
# ─────────────────────────────────────────────

class TestFrameworkDetection:
    def test_nextjs_detected(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        assert scanner.framework == "nextjs"

    def test_fastapi_detected(self, python_repo):
        scanner = CodebaseScanner(str(python_repo))
        assert scanner.framework == "fastapi"


class TestDomainDiscovery:
    def test_nextjs_finds_api_routes(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        spec_ids = [d["spec_id"] for d in domains]
        # Should find TAILOR and AUTH at minimum
        assert "TAILOR" in spec_ids
        assert "AUTH" in spec_ids

    def test_domains_have_required_keys(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        for d in domains:
            assert "spec_id" in d
            assert "display_name" in d
            assert "entry_files" in d
            assert "related_files" in d
            assert "framework" in d

    def test_python_finds_route_files(self, python_repo):
        scanner = CodebaseScanner(str(python_repo))
        domains = scanner.discover_feature_domains()
        assert len(domains) >= 1

    def test_max_domains_respected(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo), max_domains=1)
        domains = scanner.discover_feature_domains()
        assert len(domains) <= 1


class TestDependencyExtraction:
    def test_extracts_internal_deps(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        tailor_domain = next((d for d in domains if d["spec_id"] == "TAILOR"), None)
        assert tailor_domain is not None
        deps = scanner.extract_dependencies(tailor_domain)
        dep_files = [d["file"] for d in deps]
        # Should have found the auth import
        assert any("auth" in f for f in dep_files)

    def test_caps_at_30_deps(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        # Create a file with many imports
        many_imports_path = nextjs_repo / "src" / "app" / "api" / "tailor" / "route.ts"
        lines = [f'import {{ fn{i} }} from "@/app/utils/util{i}";' for i in range(50)]
        many_imports_path.write_text("\n".join(lines))

        domains = scanner.discover_feature_domains()
        tailor = next((d for d in domains if d["spec_id"] == "TAILOR"), None)
        if tailor:
            deps = scanner.extract_dependencies(tailor)
            assert len(deps) <= 30


class TestSpecGeneration:
    def test_static_spec_has_required_sections(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        tailor_domain = next((d for d in domains if d["spec_id"] == "TAILOR"), None)
        assert tailor_domain is not None

        # Use static only (no LLM needed in tests)
        spec_md, spec_meta = scanner.generate_spec(tailor_domain, use_llm=False)

        assert "# Feature Spec: TAILOR" in spec_md
        assert "## Entry Points" in spec_md
        assert "## Key Dependencies" in spec_md

    def test_meta_has_required_fields(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        domain = domains[0]
        _, spec_meta = scanner.generate_spec(domain, use_llm=False)

        assert "spec_id" in spec_meta
        assert "feature_name" in spec_meta
        assert "dependencies" in spec_meta
        assert "entry_files" in spec_meta
        assert "tags" in spec_meta
        assert spec_meta.get("auto_generated") is True

    def test_meta_is_json_serialisable(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        _, spec_meta = scanner.generate_spec(domains[0], use_llm=False)
        # Should not raise
        json_str = json.dumps(spec_meta)
        assert len(json_str) > 0

    def test_auth_pattern_included_when_present(self, nextjs_repo):
        scanner = CodebaseScanner(str(nextjs_repo))
        domains = scanner.discover_feature_domains()
        tailor = next((d for d in domains if d["spec_id"] == "TAILOR"), None)
        assert tailor is not None
        spec_md, _ = scanner.generate_spec(tailor, use_llm=False)
        # The tailor route has requireAuth, so the auth section should appear
        assert "Auth Pattern" in spec_md or "requireAuth" in spec_md


class TestTreeMapGeneration:
    def test_tree_map_excludes_git_and_venv(self, tmp_path):
        # Create fake directories and files
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("git config")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "some-pkg" / "index.js").write_text("index")
        
        # Source files
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.rs").write_text("fn main() {}")
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"test\"")
        
        scanner = CodebaseScanner(str(tmp_path))
        tree_map = scanner._generate_tree_map()
        
        # Should contain src/main.rs and Cargo.toml, but NOT .git or node_modules
        assert "main.rs" in tree_map
        assert "Cargo.toml" in tree_map
        assert "node_modules" not in tree_map
        assert "git config" not in tree_map


class TestGenericImportExtraction:
    def test_java_import(self):
        from codebase_scanner import _extract_generic_imports
        src = "import com.example.service.UserService;\nimport java.util.List;"
        imports = _extract_generic_imports(src, ".java")
        assert len(imports) == 2
        assert imports[0]["from"] == "com.example.service.UserService"
        assert imports[0]["names"] == ["UserService"]

    def test_rust_import(self):
        from codebase_scanner import _extract_generic_imports
        src = "use crate::db::Pool;\nuse std::collections::HashMap;"
        imports = _extract_generic_imports(src, ".rs")
        assert len(imports) == 2
        assert imports[0]["from"] == "crate::db::Pool"
        assert imports[0]["names"] == ["Pool"]

    def test_ruby_import(self):
        from codebase_scanner import _extract_generic_imports
        src = "require 'sinatra'\nrequire_relative 'controllers/auth'"
        imports = _extract_generic_imports(src, ".rb")
        assert len(imports) == 2
        assert imports[0]["from"] == "sinatra"
        assert imports[1]["from"] == "controllers/auth"

    def test_go_import(self):
        from codebase_scanner import _extract_generic_imports
        src = """package main
import (
    "fmt"
    "net/http"
)
import "github.com/gin-gonic/gin"
"""
        imports = _extract_generic_imports(src, ".go")
        froms = [imp["from"] for imp in imports]
        assert "fmt" in froms
        assert "net/http" in froms
        assert "github.com/gin-gonic/gin" in froms


class TestLLMDomainDiscovery:
    def test_llm_discovery_success(self, monkeypatch, tmp_path):
        # Mock _is_ollama_online to return True
        monkeypatch.setattr("codebase_scanner._is_ollama_online", lambda: True)
        
        # Create mock entry files so path verification passes
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "auth.rs").write_text("fn main() {}")
        (tmp_path / "src" / "db.rs").write_text("")

        # Mock OllamaLLM.invoke to return a valid JSON array
        mock_response = """
        [
          {
            "spec_id": "SANDBOX-AUTH",
            "display_name": "Sandbox Auth",
            "entry_files": ["src/auth.rs"],
            "related_files": ["src/db.rs"],
            "framework": "Actix",
            "language": "Rust"
          }
        ]
        """
        
        class MockLLM:
            def __init__(self, *args, **kwargs):
                pass
            def invoke(self, prompt):
                return mock_response
                
        # Patch langchain_ollama.OllamaLLM
        import sys
        import types
        mock_ollama_mod = types.ModuleType("langchain_ollama")
        mock_ollama_mod.OllamaLLM = MockLLM
        sys.modules["langchain_ollama"] = mock_ollama_mod
        
        scanner = CodebaseScanner(str(tmp_path))
        domains = scanner._discover_domains_with_llm()
        
        assert len(domains) == 1
        assert domains[0]["spec_id"] == "SANDBOX-AUTH"
        assert domains[0]["entry_files"] == ["src/auth.rs"]
        assert domains[0]["language"] == "Rust"

