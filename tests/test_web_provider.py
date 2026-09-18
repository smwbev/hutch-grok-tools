"""hutch-grok-tools — behaviour contracts of the ``hutch`` web-search provider. No network.

Fixture ``web_fixture_json_block.json`` is a real relay answer (2026-09-19, ids and encrypted
reasoning scrubbed) where the model returned the requested JSON object; the annotations-only
shape is a self-contained literal (the relay produced no prose answer to record, and the parser
fallback branch must still be pinned).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_x_search_override import _Ctx, _load_plugin  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
FIXTURE_A = PLUGIN_DIR / "tests" / "web_fixture_json_block.json"


@pytest.fixture
def plugin(monkeypatch):
    mod = _load_plugin("hutch_grok_tools_web_under_test")
    monkeypatch.setenv("HUTCH_API_KEY", "hutch-test-key")
    monkeypatch.setenv("HUTCH_BASE_URL", "https://relay.test.example/v1")
    return mod


@pytest.fixture
def provider(plugin):
    ctx = _Ctx()
    plugin.register(ctx)
    assert len(ctx.web_providers) == 1
    return ctx.web_providers[0]


@pytest.fixture
def web_cfg(plugin, monkeypatch):
    """Point ``web.hutch.*`` at a dict without touching any real config file."""
    import importlib
    wp = importlib.import_module(plugin.__name__ + ".web_provider")
    store: dict = {}
    monkeypatch.setattr(wp, "_load_web_config", lambda: store)
    return store


def _httpx_response(status: int, body, *, json_body: bool = True):
    import httpx
    text = json.dumps(body) if json_body else str(body)
    resp = SimpleNamespace(status_code=status, text=text)

    def _json():
        if not json_body:
            raise ValueError("not json")
        return body

    def _raise():
        if status >= 400:
            raise httpx.HTTPStatusError(f"{status}", request=None, response=resp)

    resp.json = _json
    resp.raise_for_status = _raise
    return resp


def _capture_httpx_post(monkeypatch, response):
    import httpx
    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def _fixture_a() -> dict:
    return json.loads(FIXTURE_A.read_text())


def _fixture_b() -> dict:
    """Annotations-only shape (self-contained literal): prose instead of the JSON object, three
    url_citation annotations of which one repeats a URL. Pins the parser's fallback branch."""
    prose = ("Two sources stood out. First, the official documentation covers setup. "
             "Second, the GitHub repository hosts the code and issues. That is all.")
    i1 = prose.index("Second,")          # citation 1 sits right after sentence one
    i2 = prose.index("That is all.")     # citation 2 sits right after sentence two
    return {
        "status": "completed",
        "model": "grok-4.5-build",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{
                "type": "output_text",
                "text": prose,
                "annotations": [
                    {"type": "url_citation", "url": "https://docs.example.org/setup", "start_index": i1 - 1, "end_index": i1, "title": "1"},
                    {"type": "url_citation", "url": "https://github.com/example/repo", "start_index": i2 - 1, "end_index": i2, "title": "2"},
                    {"type": "url_citation", "url": "https://docs.example.org/setup", "start_index": i2 - 1, "end_index": i2, "title": "3"},
                ],
            }]},
        ],
        "usage": {"server_side_tool_usage_details": {"web_search_calls": 2}},
    }


# ---------------------------------------------------------------- registration / identity


def test_provider_is_registered_as_hutch_search_only(provider):
    assert provider.name == "hutch"
    assert provider.supports_search() is True
    assert provider.supports_extract() is False
    assert "Hutch" in provider.display_name


def test_setup_schema_is_relay_keyed_and_has_no_xai_oauth_hook(provider):
    schema = provider.get_setup_schema()
    keys = [e["key"] for e in schema["env_vars"]]
    assert keys == ["HUTCH_API_KEY", "HUTCH_BASE_URL"], "picker readiness must match is_available() (both vars)"
    assert all(e.get("prompt") for e in schema["env_vars"])
    assert "post_setup" not in schema, "xai_grok post_setup would launch an xAI OAuth login"
    assert "extract" in schema["tag"].lower(), "the picker must say this is search-only"


def test_web_half_failure_does_not_roll_back_the_x_search_override(plugin, caplog):
    """The loader disposes every registration when register() raises — a web-provider failure
    must not cost the user the x_search override that already landed."""
    import logging

    class _BrokenCtx(_Ctx):
        def register_web_search_provider(self, provider):
            raise RuntimeError("registry exploded")

    ctx = _BrokenCtx()
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)  # must not raise
    assert len(ctx.calls) == 1 and ctx.calls[0]["name"] == "x_search"
    assert any("web search provider not registered" in r.getMessage() for r in caplog.records)


def test_is_available_requires_both_variables_and_makes_no_network_call(provider, monkeypatch):
    import httpx
    import requests
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("network in is_available"))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("network in is_available"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("network in is_available"))
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("network in is_available"))
    assert provider.is_available() is True
    monkeypatch.delenv("HUTCH_BASE_URL")
    assert provider.is_available() is False
    monkeypatch.delenv("HUTCH_API_KEY")
    assert provider.is_available() is False


# ---------------------------------------------------------------- request shape


def test_search_posts_grok_web_search_to_the_relay(provider, web_cfg, monkeypatch):
    seen = _capture_httpx_post(monkeypatch, _httpx_response(200, _fixture_a()))
    out = provider.search("Hermes Agent Nous Research", limit=3)
    assert out["success"] is True
    assert seen["url"] == "https://relay.test.example/v1/responses"
    assert seen["headers"]["Authorization"] == "Bearer hutch-test-key"
    assert seen["headers"]["User-Agent"].startswith("hutch-grok-tools/")
    body = seen["json"]
    assert body["model"] == "grok-4.5", "bundled grok-build-0.1 is unknown to the relay"
    assert body["tools"] == [{"type": "web_search"}]
    assert body["include"] == ["no_inline_citations"]
    assert "Hermes Agent Nous Research" in body["input"][0]["content"]
    assert "at most 3 results" in body["input"][0]["content"]
    assert seen["timeout"] == 90.0


def test_search_honours_web_hutch_config(provider, web_cfg, monkeypatch):
    seen = _capture_httpx_post(monkeypatch, _httpx_response(200, _fixture_a()))
    web_cfg.update(model="grok-4.6", timeout=30, allowed_domains=["nousresearch.com", "github.com"])
    provider.search("q", limit=2)
    assert seen["json"]["model"] == "grok-4.6"
    assert seen["timeout"] == 30.0
    assert seen["json"]["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["nousresearch.com", "github.com"]}}]


def test_both_domain_filters_are_rejected_before_any_http(provider, web_cfg, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not reach the relay"))
    web_cfg.update(allowed_domains=["a.com"], excluded_domains=["b.com"])
    out = provider.search("q")
    assert out["success"] is False and "cannot both be set" in out["error"]


def test_missing_credentials_fail_without_http(provider, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not reach the relay"))
    monkeypatch.delenv("HUTCH_API_KEY")
    out = provider.search("q")
    assert out["success"] is False and "HUTCH_API_KEY" in out["error"]


# ---------------------------------------------------------------- parsing


def test_real_relay_answer_with_json_block_parses_into_ordered_rows(provider, web_cfg, monkeypatch):
    data = _fixture_a()
    text = "".join(c.get("text", "") for it in data["output"] if it.get("type") == "message" for c in it.get("content", []))
    expected = json.loads(text)["results"]
    assert len(expected) >= 2, "fixture must carry a multi-row JSON block"

    _capture_httpx_post(monkeypatch, _httpx_response(200, data))
    out = provider.search("q", limit=len(expected))
    rows = out["data"]["web"]
    assert out["success"] is True and len(rows) == len(expected)
    for i, (row, exp) in enumerate(zip(rows, expected), start=1):
        assert row["url"] == exp["url"] and row["title"] == exp["title"] and row["description"] == exp["description"]
        assert row["position"] == i
        assert set(row) >= {"url", "title", "description"}

    out = provider.search("q", limit=1)
    assert [r["url"] for r in out["data"]["web"]] == [expected[0]["url"]], "limit must cut the rows"


def test_prose_answer_falls_back_to_deduplicated_annotations(provider, web_cfg, monkeypatch):
    data = _fixture_b()
    _capture_httpx_post(monkeypatch, _httpx_response(200, data))
    out = provider.search("q", limit=5)
    rows = out["data"]["web"]
    assert out["success"] is True
    assert len(rows) == 2, "duplicate URL must collapse"
    assert [r["url"] for r in rows] == ["https://docs.example.org/setup", "https://github.com/example/repo"]
    assert rows[0]["description"].endswith("covers setup.") and "documentation" in rows[0]["description"]
    assert rows[1]["description"].endswith("code and issues.") and "repository" in rows[1]["description"]
    assert [r["position"] for r in rows] == [1, 2]


def test_zero_hits_is_a_success_with_empty_results(provider, web_cfg, monkeypatch):
    body = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"results": []}', "annotations": []}]}]}
    _capture_httpx_post(monkeypatch, _httpx_response(200, body))
    out = provider.search("q")
    assert out["success"] is True and out["data"]["web"] == []


# ---------------------------------------------------------------- errors


def test_http_error_carries_status_and_upstream_code(provider, web_cfg, monkeypatch):
    _capture_httpx_post(monkeypatch, _httpx_response(402, {"code": "personal-team-blocked", "error": "spending-limit"}))
    out = provider.search("q")
    assert out["success"] is False
    assert "402" in out["error"] and "personal-team-blocked" in out["error"]


def test_error_envelope_on_200_is_a_failure(provider, web_cfg, monkeypatch):
    _capture_httpx_post(monkeypatch, _httpx_response(200, {"error": {"message": "overloaded", "type": "server_error"}}))
    out = provider.search("q")
    assert out["success"] is False and "overloaded" in out["error"]


def test_non_json_and_non_object_bodies_are_failures_not_exceptions(provider, web_cfg, monkeypatch):
    _capture_httpx_post(monkeypatch, _httpx_response(200, "<html>gateway</html>", json_body=False))
    out = provider.search("q")
    assert out["success"] is False and "JSON" in out["error"]
    for body in (["a"], "done", 7):
        _capture_httpx_post(monkeypatch, _httpx_response(200, body))
        out = provider.search("q")
        assert out["success"] is False and "non-object" in out["error"]


def test_request_error_names_the_relay(provider, web_cfg, monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    out = provider.search("q")
    assert out["success"] is False and "Hutch relay" in out["error"]


def test_search_reads_credentials_from_the_profile_secret_scope(provider, web_cfg, monkeypatch):
    import agent.secret_scope as ss
    seen = _capture_httpx_post(monkeypatch, _httpx_response(200, _fixture_a()))
    monkeypatch.delenv("HUTCH_API_KEY")
    monkeypatch.delenv("HUTCH_BASE_URL")
    assert provider.is_available() is False
    token = ss.set_secret_scope({"HUTCH_API_KEY": "scoped", "HUTCH_BASE_URL": "https://relay.test.example/v1/"})
    try:
        assert provider.is_available() is True
        assert provider.search("q")["success"] is True
        assert seen["headers"]["Authorization"] == "Bearer scoped"
        assert seen["url"] == "https://relay.test.example/v1/responses"
    finally:
        ss.reset_secret_scope(token)


def test_fixture_does_not_leak_relay_identifiers():
    raw = FIXTURE_A.read_text()
    data = json.loads(raw)
    for k in ("id", "prompt_cache_key", "safety_identifier", "user", "system_fingerprint"):
        assert data.get(k) in (None, "", "redacted"), k
    assert "encrypted_content" not in raw
    assert copy.deepcopy(data) == data  # sanity: fixture is plain JSON
