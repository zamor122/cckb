import pytest

@pytest.fixture(autouse=True)
def disable_ollama_by_default(monkeypatch):
    """Disable Ollama and Gemini by default in all tests to keep them fast and deterministic."""
    monkeypatch.setattr("codebase_scanner._is_ollama_online", lambda: False)
    # Also patch unified llm_client check
    try:
        import llm_client
        monkeypatch.setattr(llm_client, "is_llm_online", lambda: False)
    except ImportError:
        pass
