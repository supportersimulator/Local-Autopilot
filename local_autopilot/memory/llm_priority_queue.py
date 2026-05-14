"""Minimal LLM Priority Queue shim for Local Autopilot.

Aaron's superrepo ships a 2000-line `memory/llm_priority_queue.py` that
wires every Synaptic call into Redis pub/sub, NATS, gains-gate counters,
JetStream, a real priority queue, and four fallback providers. Local
Autopilot doesn't need any of that — it just needs one thing:

    from memory.llm_priority_queue import llm_generate, Priority
    text = llm_generate(system_prompt='...', user_prompt='...',
                        priority=Priority.ATLAS, profile='s8_synaptic',
                        caller='autopilot')

This shim provides exactly that surface, plus:

    * Three providers: local MLX (default port 5044), DeepSeek HTTPS,
      OpenAI HTTPS. Provider chosen by `LLM_EXTERNAL_PROVIDER` env var.
    * The full `s8_synaptic` profile (matches the superrepo entry).
    * Stdlib-only — `requests` is the one external dep (already in
      `requirements.txt`).
    * Zero-silent-failures: every fallback path increments a named
      counter in `/tmp/local-autopilot-llm-counters.json` and logs to
      `/tmp/local-autopilot-llm.err`.

This file is intentionally simple. If you want the full priority-queue
behaviour (back-pressure, urgent yield, MLX-only on background), use
the superrepo module instead.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("local_autopilot.llm_queue")


# ---------------------------------------------------------------------------
# Priority enum — same numeric values as the superrepo so callers don't break
# ---------------------------------------------------------------------------


class Priority(enum.IntEnum):
    AARON = 1       # webhook / human direct
    ATLAS = 2       # autopilot diagnostics — what we use
    EXTERNAL = 3    # phone bridge etc.
    BACKGROUND = 4  # butler / scheduler


# ---------------------------------------------------------------------------
# Model profiles — copied verbatim from the superrepo so behaviour matches
# ---------------------------------------------------------------------------


MODEL_PROFILES: dict[str, dict] = {
    "coding": {
        "temperature": 0.3, "top_p": 0.9, "max_tokens": 1024,
        "frequency_penalty": 0.1, "presence_penalty": 0.0,
        "repetition_penalty": 1.05,
    },
    "classify": {
        "temperature": 0.2, "top_p": 0.9, "max_tokens": 64,
        "frequency_penalty": 0.3, "presence_penalty": 0.2,
        "repetition_penalty": 1.1,
    },
    "extract": {
        "temperature": 0.3, "top_p": 0.9, "max_tokens": 768,
        "frequency_penalty": 0.3, "presence_penalty": 0.2,
        "repetition_penalty": 1.15,
    },
    "voice": {
        "temperature": 0.6, "top_p": 0.85, "max_tokens": 512,
        "frequency_penalty": 0.4, "presence_penalty": 0.3,
        "repetition_penalty": 1.2,
    },
    "deep": {
        "temperature": 0.5, "top_p": 0.95, "max_tokens": 2048,
        "frequency_penalty": 0.2, "presence_penalty": 0.1,
        "repetition_penalty": 1.1,
    },
    # The profile autopilot uses for every Synaptic call.
    "s8_synaptic": {
        "temperature": 0.7, "top_p": 0.95, "max_tokens": 1500,
        "frequency_penalty": 0.2, "presence_penalty": 0.1,
        "repetition_penalty": 1.1,
    },
    "s2_professor": {
        "temperature": 0.6, "top_p": 0.9, "max_tokens": 1200,
        "frequency_penalty": 0.3, "presence_penalty": 0.2,
        "repetition_penalty": 1.15,
    },
}


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------


MLX_SERVER_URL = os.environ.get("MLX_SERVER_URL", "http://127.0.0.1:5044")
MLX_DEFAULT_MODEL = os.environ.get(
    "MLX_DEFAULT_MODEL", "mlx-community/Qwen3-4B-4bit"
)

# Provider preference. Options:
#   - "local-first"    — try MLX, fall back to DeepSeek (default)
#   - "local-only"     — never call HTTPS providers
#   - "deepseek-first" — call DeepSeek; fall back to MLX if available
#   - "openai-first"   — call OpenAI; fall back to MLX
#   - "deepseek"       — DeepSeek only (no fallback)
#   - "openai"         — OpenAI only
LLM_EXTERNAL_PROVIDER = os.environ.get("LLM_EXTERNAL_PROVIDER", "local-first")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")


# ---------------------------------------------------------------------------
# Counters — ZSF invariant (every fallback is observable)
# ---------------------------------------------------------------------------


_counters_lock = threading.Lock()
_counters: dict[str, int] = {
    "mlx_calls": 0,
    "mlx_errors": 0,
    "deepseek_calls": 0,
    "deepseek_errors": 0,
    "openai_calls": 0,
    "openai_errors": 0,
    "all_providers_failed": 0,
    "fallback_to_secondary": 0,
}

_COUNTERS_PATH = Path(os.environ.get(
    "LLM_COUNTERS_PATH", "/tmp/local-autopilot-llm-counters.json"
))
_ERR_LOG = Path(os.environ.get(
    "LLM_ERR_LOG", "/tmp/local-autopilot-llm.err"
))


def _bump(name: str, note: str = "") -> None:
    with _counters_lock:
        _counters[name] = _counters.get(name, 0) + 1
    if note:
        try:
            _ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _ERR_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{time.time()} {name} {note[:300]}\n")
        except OSError:
            pass
    _flush_counters()


def _flush_counters() -> None:
    try:
        _COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COUNTERS_PATH.write_text(
            json.dumps(_counters, indent=2, sort_keys=True)
        )
    except OSError:
        pass


def get_counters() -> dict[str, int]:
    with _counters_lock:
        return dict(_counters)


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _call_mlx(
    system_prompt: str,
    user_prompt: str,
    profile_cfg: dict,
    timeout_s: float,
) -> Optional[str]:
    """Hit the local MLX server (OpenAI-compatible /v1/chat/completions)."""
    url = MLX_SERVER_URL.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": MLX_DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": profile_cfg.get("temperature", 0.5),
        "top_p": profile_cfg.get("top_p", 0.9),
        "max_tokens": profile_cfg.get("max_tokens", 1024),
    }
    try:
        _bump("mlx_calls")
        r = requests.post(url, json=payload, timeout=timeout_s)
        if r.status_code != 200:
            _bump("mlx_errors", note=f"http_{r.status_code}")
            return None
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            _bump("mlx_errors", note="no_choices")
            return None
        return choices[0]["message"]["content"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        _bump("mlx_errors", note=str(exc))
        return None


def _call_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    profile_cfg: dict,
    timeout_s: float,
    counter_calls: str,
    counter_errors: str,
) -> Optional[str]:
    """Generic OpenAI-compatible chat-completions call (DeepSeek + OpenAI)."""
    if not api_key:
        _bump(counter_errors, note="no_api_key")
        return None
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": profile_cfg.get("temperature", 0.5),
        "top_p": profile_cfg.get("top_p", 0.9),
        "max_tokens": profile_cfg.get("max_tokens", 1024),
    }
    try:
        _bump(counter_calls)
        r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
        if r.status_code != 200:
            _bump(counter_errors, note=f"http_{r.status_code}:{r.text[:200]}")
            return None
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            _bump(counter_errors, note="no_choices")
            return None
        return choices[0]["message"]["content"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        _bump(counter_errors, note=str(exc))
        return None


def _call_deepseek(system_prompt, user_prompt, profile_cfg, timeout_s):
    return _call_openai_compat(
        DEEPSEEK_BASE_URL,
        DEEPSEEK_API_KEY,
        DEEPSEEK_MODEL,
        system_prompt,
        user_prompt,
        profile_cfg,
        timeout_s,
        counter_calls="deepseek_calls",
        counter_errors="deepseek_errors",
    )


def _call_openai(system_prompt, user_prompt, profile_cfg, timeout_s):
    return _call_openai_compat(
        OPENAI_BASE_URL,
        OPENAI_API_KEY,
        OPENAI_MODEL,
        system_prompt,
        user_prompt,
        profile_cfg,
        timeout_s,
        counter_calls="openai_calls",
        counter_errors="openai_errors",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def llm_generate(
    system_prompt: str,
    user_prompt: str,
    *,
    priority: Priority = Priority.ATLAS,
    profile: str = "s8_synaptic",
    caller: str = "local_autopilot",
    timeout_s: float = 90.0,
    **_ignored: object,
) -> Optional[str]:
    """Synchronously generate text via the configured provider chain.

    Returns the response string on success, or `None` on every-provider
    failure. The caller is responsible for inspecting None and reacting
    (the autopilot runner bumps `synaptic_call_errors` and retries up to
    `max_retries` before aborting the cycle).

    `priority` is accepted for compatibility with the superrepo API but
    is not actually used to gate dispatch here — Local Autopilot is a
    single-process tool, there is no contention to schedule.
    """
    profile_cfg = MODEL_PROFILES.get(profile, MODEL_PROFILES["coding"])

    # Build the provider chain per LLM_EXTERNAL_PROVIDER.
    chain: list[str] = []
    pref = LLM_EXTERNAL_PROVIDER.lower().strip()
    if pref == "local-only":
        chain = ["mlx"]
    elif pref == "deepseek":
        chain = ["deepseek"]
    elif pref == "openai":
        chain = ["openai"]
    elif pref == "deepseek-first":
        chain = ["deepseek", "mlx"]
    elif pref == "openai-first":
        chain = ["openai", "mlx"]
    elif pref == "deepseek-first-openai-fallback":
        chain = ["deepseek", "openai", "mlx"]
    else:  # "local-first" (default)
        chain = ["mlx", "deepseek", "openai"]

    last_provider = None
    for prov in chain:
        if prov == "mlx":
            result = _call_mlx(system_prompt, user_prompt, profile_cfg, timeout_s)
        elif prov == "deepseek":
            result = _call_deepseek(system_prompt, user_prompt, profile_cfg, timeout_s)
        elif prov == "openai":
            result = _call_openai(system_prompt, user_prompt, profile_cfg, timeout_s)
        else:
            continue

        last_provider = prov
        if result:
            return result
        _bump("fallback_to_secondary", note=f"from {prov} caller={caller}")

    _bump("all_providers_failed", note=f"chain={chain} caller={caller}")
    return None


# ---------------------------------------------------------------------------
# Smoke test (don't run unless you have providers configured)
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    out = llm_generate(
        system_prompt="You are a terse assistant.",
        user_prompt="Reply with the single token 'ok'.",
        priority=Priority.ATLAS,
        profile="s8_synaptic",
        caller="smoke",
        timeout_s=30.0,
    )
    print(f"providers used: {get_counters()}")
    print(f"result: {out!r}")
