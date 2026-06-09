import pytest

@pytest.fixture(autouse=True)
def disable_ollama_by_default(monkeypatch):
    """Disable Ollama by default in all tests to keep them fast and deterministic."""
    # Patch the scanner's online check
    monkeypatch.setattr("codebase_scanner._is_ollama_online", lambda: False)
