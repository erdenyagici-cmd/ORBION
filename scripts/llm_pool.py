"""
ORBION — provider-agnostic LLM pool with automatic failover.

Order: whatever providers you configure, tried in priority order.
If one is down / out of quota / rate-limited, ORBION quietly moves to the next.
Local Ollama is the last resort and is NEVER used for "sensitive" consumers.

Every provider here speaks the OpenAI Chat Completions schema, so adding a new
one is just a base_url + key + model. No vendor SDKs, no lock-in.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str
    priority: int = 10
    sensitive_ok: bool = True   # may handle sensitive consumers (health, etc.)
    local: bool = False
    # runtime health
    cooldown_until: float = 0.0
    last_error: str = ""
    ok_calls: int = 0
    err_calls: int = 0


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load_providers() -> list[Provider]:
    """Read providers from the environment. Missing keys = provider skipped."""
    out: list[Provider] = []

    # 1) Optional primary gateway (OpenAI-compatible)
    if _env("EVREN_API_KEY") and _env("EVREN_BASE_URL"):
        out.append(Provider(
            name="primary",
            base_url=_env("EVREN_BASE_URL").rstrip("/"),
            model=_env("EVREN_MODEL", "gpt-4o-mini"),
            api_key=_env("EVREN_API_KEY"),
            priority=1,
        ))

    # 2) Google Gemini (OpenAI-compatible endpoint)
    if _env("GEMINI_API_KEY"):
        out.append(Provider(
            name="gemini",
            base_url=_env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/"),
            model=_env("GEMINI_MODEL", "gemini-2.0-flash"),
            api_key=_env("GEMINI_API_KEY"),
            priority=2,
        ))

    # 3) Groq
    if _env("GROQ_API_KEY"):
        out.append(Provider(
            name="groq",
            base_url=_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            model=_env("GROQ_MODEL", "llama-3.1-8b-instant"),
            api_key=_env("GROQ_API_KEY"),
            priority=3,
        ))

    # 4) OpenRouter (optional, has free models)
    if _env("OPENROUTER_API_KEY"):
        out.append(Provider(
            name="openrouter",
            base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            model=_env("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
            api_key=_env("OPENROUTER_API_KEY"),
            priority=4,
        ))

    # 5) Local Ollama (never for sensitive consumers)
    out.append(Provider(
        name="ollama",
        base_url=_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/v1",
        model=_env("OLLAMA_MODEL", "qwen2.5:4b"),
        api_key="ollama",
        priority=99,
        sensitive_ok=False,
        local=True,
    ))

    out.sort(key=lambda p: p.priority)
    return out


def _post(url: str, body: dict, headers: dict, timeout: int = 120) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_quota_error(code: int, body: str) -> bool:
    if code in (401, 403, 429):
        return True
    low = (body or "").lower()
    return any(s in low for s in ("quota", "rate limit", "exceeded", "insufficient"))


class LLMPool:
    """A tiny failover pool over OpenAI-compatible providers."""

    COOLDOWN = 180  # seconds a failing provider is skipped

    def __init__(self, providers: Optional[list[Provider]] = None):
        self.providers = providers if providers is not None else load_providers()

    def _usable(self, p: Provider, sensitive: bool) -> bool:
        if p.cooldown_until > time.time():
            return False
        if sensitive and not p.sensitive_ok:
            return False
        return True

    def chat(
        self,
        messages: list[dict],
        *,
        consumer: str = "general",   # "general" | "sensitive"
        max_tokens: int = 800,
        timeout: int = 120,
    ) -> tuple[str, dict]:
        """
        Try providers in priority order.
        Returns (text, meta). meta = {"provider": name, "ok": bool, "error": ...}
        Sensitive consumers never touch the local model.
        """
        sensitive = consumer == "sensitive"
        last_err = "no provider available"

        for p in self.providers:
            if not self._usable(p, sensitive):
                continue
            try:
                res = _post(
                    f"{p.base_url}/chat/completions",
                    {"model": p.model, "messages": messages, "max_tokens": max_tokens},
                    {"Authorization": f"Bearer {p.api_key}"},
                    timeout=timeout,
                )
                text = (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if text:
                    p.ok_calls += 1
                    return text, {"provider": p.name, "ok": True}
                last_err = "empty response"
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "ignore")[:400]
                except Exception:
                    pass
                last_err = f"HTTP {exc.code}"
                p.err_calls += 1
                p.last_error = last_err
                if _is_quota_error(exc.code, body) or exc.code >= 500:
                    p.cooldown_until = time.time() + self.COOLDOWN
                continue
            except Exception as exc:  # network, timeout, malformed
                last_err = type(exc).__name__
                p.err_calls += 1
                p.last_error = last_err
                p.cooldown_until = time.time() + 60
                continue

        # Honest failure — never fabricate an answer.
        return "", {"provider": None, "ok": False, "error": last_err}

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": p.name,
                "model": p.model,
                "priority": p.priority,
                "local": p.local,
                "sensitive_ok": p.sensitive_ok,
                "healthy": p.cooldown_until <= now,
                "cooldown_left": max(0, int(p.cooldown_until - now)),
                "ok_calls": p.ok_calls,
                "err_calls": p.err_calls,
                "last_error": p.last_error,
            }
            for p in self.providers
        ]
