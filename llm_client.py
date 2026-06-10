#!/usr/bin/env python3
"""
llm_client.py — Unified LLM client loader for CCKB.

Provides a single `get_llm(timeout)` factory function that dynamically chooses
between Google Gemini (ChatGoogleGenerativeAI) and local Ollama (OllamaLLM) based
on the availability of the `GEMINI_API_KEY` environment variable.
"""

import os
from typing import Any

class GeminiWrapper:
    """
    Wrapper around ChatGoogleGenerativeAI to match the simple string-based `.invoke(prompt)`
    signature of langchain_ollama's OllamaLLM.
    """
    def __init__(self, chat_model: Any):
        self.chat_model = chat_model

    def invoke(self, prompt: str, *args, **kwargs) -> str:
        res = self.chat_model.invoke(prompt, *args, **kwargs)
        # Chat models return an AIMessage; we return its string content.
        if hasattr(res, "content"):
            return res.content
        return str(res)


def get_llm(timeout: int = 30) -> Any:
    """
    Return a LangChain-compatible LLM instance.
    Uses ChatGoogleGenerativeAI if GEMINI_API_KEY is available (in env or .cckb/.env),
    otherwise falls back to local OllamaLLM.
    """
    # Try reading from env
    api_key = os.environ.get("GEMINI_API_KEY")

    # If missing, try loading from .cckb/.env
    if not api_key:
        cckb_env = os.path.join(".cckb", ".env")
        if os.path.exists(cckb_env):
            try:
                with open(cckb_env, "r") as f:
                    for line in f:
                        if "GEMINI_API_KEY" in line and "=" in line:
                            api_key = line.split("=", 1)[1].strip()
                            # Set it in environ for downstream libraries
                            os.environ["GEMINI_API_KEY"] = api_key
                            break
            except Exception:
                pass

    if api_key:
        # Switch to Gemini API
        from langchain_google_genai import ChatGoogleGenerativeAI
        model_name = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        chat_model = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.2,
        )
        return GeminiWrapper(chat_model)
    else:
        # Fall back to local Ollama
        from langchain_ollama import OllamaLLM
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        return OllamaLLM(
            model="llama3.2:3b",
            base_url=base_url,
            timeout=timeout
        )


def is_llm_online() -> bool:
    """Return True if either Gemini is configured or local Ollama is reachable."""
    # Check Gemini first
    if os.environ.get("GEMINI_API_KEY"):
        return True
    
    # Check if .cckb/.env contains GEMINI_API_KEY
    cckb_env = os.path.join(".cckb", ".env")
    if os.path.exists(cckb_env):
        try:
            with open(cckb_env, "r") as f:
                for line in f:
                    if "GEMINI_API_KEY" in line:
                        return True
        except Exception:
            pass

    # Check Ollama
    import socket
    try:
        # Try connecting to Ollama's port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 11434))
        s.close()
        return True
    except Exception:
        return False
