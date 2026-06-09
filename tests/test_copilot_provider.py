"""Copilot LLM provider selection — Ollama Cloud / OpenAI-compatible (#59)."""

from __future__ import annotations

import httpx
import pytest

from cfactory.config import Settings
from cfactory.copilot import service as svc


def test_make_runner_defaults_to_claude(monkeypatch):
    # Don't actually build the SDK runner — just confirm which branch is taken.
    monkeypatch.setattr(svc, "_default_runner", lambda model: ("claude", model))
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *a: ("openai", a))
    assert svc.make_runner(Settings(copilot_provider="claude"))[0] == "claude"


@pytest.mark.parametrize("provider", ["ollama", "openai", "openai_compatible"])
def test_make_runner_selects_openai_compatible(monkeypatch, provider):
    monkeypatch.setattr(svc, "_default_runner", lambda model: ("claude", model))
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *a: ("openai", a))
    runner = svc.make_runner(Settings(copilot_provider=provider))
    assert runner[0] == "openai"


def test_openai_compatible_runner_sends_bearer_and_parses(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = httpx.Response(200)  # placeholder
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "feature #142 is stuck in test"}}]}
        )

    transport = httpx.MockTransport(handler)
    real_post = httpx.post

    def fake_post(url, **kwargs):  # route through the mock transport
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    try:
        runner = svc._openai_compatible_runner("https://ollama.com/v1", "sekret", "gpt-oss:120b")
        out = runner("where is #142", "board...", "system")
    finally:
        monkeypatch.setattr(httpx, "post", real_post)

    assert out == "feature #142 is stuck in test"
    assert seen["url"] == "https://ollama.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sekret"


def test_provider_status_claude_is_passive():
    # Explicit ollama_api_key=None so an ambient OLLAMA_API_KEY env can't leak in.
    st = svc.provider_status(Settings(copilot_provider="claude", ollama_api_key=None))
    assert st["provider"] == "claude"
    assert st["reachable"] is None  # no network probe: Claude provider, no key set


def test_provider_status_probes_models_for_ollama(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers.get("Authorization") == "Bearer k"
        return httpx.Response(200, json={"data": [{"id": "gpt-oss:120b"}, {"id": "glm-4.6"}]})

    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    st = svc.provider_status(
        Settings(copilot_provider="ollama", ollama_api_key="k",
                 ollama_cloud_base_url="https://ollama.com/v1")
    )
    assert st["provider"] == "ollama" and st["reachable"] is True
    assert "gpt-oss:120b" in st["models"]
