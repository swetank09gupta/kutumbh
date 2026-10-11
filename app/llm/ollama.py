"""Minimal local-Ollama client shared by the categoriser and the advisor (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")


def available_models(timeout: float = 5) -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception as exc:
        logger.debug("Ollama /api/tags failed: %s", exc)
        return []


def pick_model(*preferred: Optional[str]) -> Optional[str]:
    """
    First preferred model that is actually installed ('llama3.2' also matches 'llama3.2:latest'),
    else any installed model. The advisor used to request an uninstalled model and fail with a
    404; resolving against /api/tags makes that impossible.
    """
    installed = available_models()
    if not installed:
        return None
    for want in preferred:
        if not want:
            continue
        for name in installed:
            if name == want or name == f"{want}:latest":
                return name
    return installed[0]


def generate_json(prompt: str, model: str, timeout: float = 180, num_predict: int = 700) -> Optional[dict[str, Any]]:
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "format": "json", "keep_alive": -1,
        "options": {"temperature": 0, "num_predict": num_predict},
    }).encode()
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(json.loads(r.read()).get("response", "{}"))
    except Exception as exc:
        logger.warning("Ollama generate failed (%s): %s", model, exc)
        return None
