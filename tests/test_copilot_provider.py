"""Copilot LLM provider selection — Ollama Cloud / OpenAI-compatible (#59)."""

from __future__ import annotations

import httpx
import pytest
from cfactory.config import Settings
from cfactory.copilot import service as svc


def test_make_runner_defaults_to_claude(monkeypatch):
    # Don't actually build the SDK runner — just confirm which branch is taken.
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(svc, "_default_runner", lambda model: ("claude", model))
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *a: ("openai", a))
    assert svc.make_runner(Settings(_env_file=None, copilot_provider="claude"))[0] == "claude"


@pytest.mark.parametrize("provider", ["ollama", "openai", "openai_compatible"])
def test_make_runner_selects_openai_compatible(monkeypatch, provider):
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(svc, "_default_runner", lambda model: ("claude", model))
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *a: ("openai", a))
    runner = svc.make_runner(Settings(_env_file=None, copilot_provider=provider))
    assert runner[0] == "openai"


@pytest.mark.parametrize("gateway_url", ["", "   "])
def test_make_runner_without_gateway_uses_provider_credentials(monkeypatch, gateway_url):
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *args: args)
    settings = Settings(
        _env_file=None,
        copilot_provider="ollama",
        ollama_cloud_base_url="https://provider.example/v1",
        ollama_api_key="provider-marker",
        model_gateway_url=gateway_url,
        model_gateway_token="gateway-token",  # noqa: S106 — fake test credential
    )
    assert svc.make_runner(settings) == (
        "https://provider.example/v1",
        "provider-marker",
        settings.copilot_model,
    )


@pytest.mark.parametrize(
    ("gateway_url", "gateway_token"),
    [
        ("https://gateway.example", "gateway-token"),
        ("  https://gateway.example/  ", "gateway-token\n"),
    ],
)
def test_make_runner_uses_gateway(monkeypatch, gateway_url, gateway_token):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    runner = svc.make_runner(
        Settings(
            _env_file=None,
            copilot_provider="ollama",
            ollama_api_key="provider-marker",
            model_gateway_url=gateway_url,
            model_gateway_token=gateway_token,
        )
    )
    assert runner("question", "board", "system") == "answer"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://gateway.example/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer gateway-token"
    assert "provider-marker" not in repr(seen[0].headers)


@pytest.mark.parametrize("gateway_token", [None, "", "  "])
def test_make_runner_without_gateway_token_sends_nothing(monkeypatch, gateway_token):
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200))

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    runner = svc.make_runner(
        Settings(
            _env_file=None,
            copilot_provider="ollama",
            ollama_api_key="provider-marker",
            model_gateway_url="https://gateway.example",
            model_gateway_token=gateway_token,
        )
    )
    with pytest.raises(RuntimeError, match="model gateway token is not configured"):
        runner("question", "board", "system")
    assert seen == []


def test_make_runner_does_not_fall_back_after_gateway_error(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    runner = svc.make_runner(
        Settings(
            _env_file=None,
            copilot_provider="ollama",
            ollama_api_key="provider-marker",
            model_gateway_url="https://gateway.example",
            model_gateway_token="gateway-token",  # noqa: S106 — fake test credential
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        runner("question", "board", "system")
    assert len(seen) == 1
    assert seen[0].url.host == "gateway.example"


def test_make_runner_claude_ignores_gateway(monkeypatch):
    monkeypatch.setattr(svc, "_default_runner", lambda model: ("claude", model))
    monkeypatch.setattr(svc, "_openai_compatible_runner", lambda *args: ("openai", args))
    settings = Settings(
        _env_file=None,
        copilot_provider="claude",
        ollama_api_key="provider-marker",
        model_gateway_url="https://gateway.example",
        model_gateway_token="gateway-token",  # noqa: S106 — fake test credential
    )
    assert svc.make_runner(settings) == ("claude", settings.copilot_model)


def test_bare_gateway_environment_variables_do_not_enable_flag(monkeypatch):
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("MODEL_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("MODEL_GATEWAY_TOKEN", "gateway-token")
    settings = Settings(_env_file=None, ollama_api_key=None)
    assert settings.model_gateway_url == ""
    assert settings.model_gateway_token is None


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


def test_provider_status_claude_is_passive(monkeypatch):
    # Explicit ollama_api_key=None so an ambient OLLAMA_API_KEY env can't leak in.
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_TOKEN", raising=False)
    st = svc.provider_status(
        Settings(_env_file=None, copilot_provider="claude", ollama_api_key=None)
    )
    assert st["provider"] == "claude"
    assert st["reachable"] is None  # no network probe: Claude provider, no key set


def test_provider_status_probes_models_for_ollama(monkeypatch):
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("CFACTORY_MODEL_GATEWAY_TOKEN", raising=False)

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
        Settings(
            _env_file=None,
            copilot_provider="ollama",
            ollama_api_key="k",
            ollama_cloud_base_url="https://ollama.com/v1",
        )
    )
    assert st["provider"] == "ollama" and st["reachable"] is True
    assert "gpt-oss:120b" in st["models"]


@pytest.mark.parametrize("provider", ["ollama", "claude"])
def test_provider_status_gateway_health_check(monkeypatch, provider):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    result = svc.provider_status(
        Settings(
            _env_file=None,
            copilot_provider=provider,
            ollama_api_key="provider-marker",
            model_gateway_url="  https://gateway.example/  ",
            model_gateway_token="gateway-token\n",  # noqa: S106 — fake test credential
        )
    )
    assert len(seen) == 1
    assert str(seen[0].url) == "https://gateway.example/healthz"
    assert "Authorization" not in seen[0].headers
    assert result == {
        "provider": provider,
        "model": "claude-opus-4-8",
        "gateway": True,
        "has_key": True,
        "reachable": True,
    }
    assert "gateway-token" not in repr(result)
    assert "provider-marker" not in repr(result)


@pytest.mark.parametrize("failure", ["status", "connect"])
def test_provider_status_gateway_failure(monkeypatch, failure):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if failure == "connect":
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    result = svc.provider_status(
        Settings(
            _env_file=None,
            copilot_provider="ollama",
            ollama_api_key="provider-marker",
            model_gateway_url="https://gateway.example",
            model_gateway_token="gateway-token",  # noqa: S106 — fake test credential
        )
    )
    assert len(seen) == 1
    assert seen[0].url.path == "/healthz"
    assert "Authorization" not in seen[0].headers
    assert result["reachable"] is False
    assert result["error"] == "gateway probe failed"
    assert "models" not in result and "base_url" not in result
    assert "gateway-token" not in repr(result)
    assert "provider-marker" not in repr(result)


def test_gateway_token_hidden_from_settings_repr():
    settings = Settings(
        _env_file=None,
        ollama_api_key=None,
        model_gateway_url="",
        model_gateway_token="tok-123",  # noqa: S106 — fake test credential
    )
    assert "tok-123" not in repr(settings)
