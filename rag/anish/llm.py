"""LLM client — Groq (default) or Google Gemini free tier."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


def load_dotenv_file(path: Path | None = None) -> None:
    """Minimal .env loader (avoids requiring python-dotenv when pip is broken)."""
    env_path = path or Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv_file()


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str


def load_llm_config() -> LLMConfig:
    provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    if provider == "gemini":
        key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
        model = os.getenv("LLM_MODEL") or "gemini-flash-latest"
        if not key:
            raise RuntimeError(
                "LLM_PROVIDER=gemini requires GOOGLE_API_KEY (free at https://aistudio.google.com/apikey)."
            )
        return LLMConfig(provider="gemini", model=model, api_key=key)

    # default: groq
    key = os.getenv("GROQ_API_KEY") or ""
    model = os.getenv("LLM_MODEL") or "llama-3.3-70b-versatile"
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is required (free at https://console.groq.com/keys). "
            "Or set LLM_PROVIDER=gemini and GOOGLE_API_KEY."
        )
    return LLMConfig(provider="groq", model=model, api_key=key)


def chat(system: str, user: str, *, temperature: float = 0.0) -> str:
    cfg = load_llm_config()
    if cfg.provider == "gemini":
        return _gemini_chat(cfg, system, user, temperature=temperature)
    return _groq_chat(cfg, system, user, temperature=temperature)


def _groq_chat(cfg: LLMConfig, system: str, user: str, *, temperature: float) -> str:
    payload = {
        "model": cfg.model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq API error {e.code}: {body}") from e
    return data["choices"][0]["message"]["content"].strip()


def _gemini_chat(cfg: LLMConfig, system: str, user: str, *, temperature: float) -> str:
    fallbacks = [cfg.model]
    for alt in ("gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash"):
        if alt not in fallbacks:
            fallbacks.append(alt)

    last_err: Exception | None = None
    for model in fallbacks:
        try:
            return _gemini_request(model, cfg.api_key, system, user, temperature=temperature)
        except RuntimeError as e:
            if "404" in str(e) and model != fallbacks[-1]:
                last_err = e
                continue
            raise
    raise last_err or RuntimeError("Gemini request failed")


def _gemini_request(
    model: str, api_key: str, system: str, user: str, *, temperature: float
) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    gen_cfg: dict = {"temperature": temperature}
    if model.startswith("gemini-2.5"):
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen_cfg,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {e.code}: {body}") from e
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()
