"""hutch-x-search — behaviour contracts of the x_search override. No network.

Run from the plugin root with the Hermes tree importable::

    PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

PLUGIN_DIR = Path(__file__).resolve().parents[1]
FIXTURE = PLUGIN_DIR / "tests" / "relay_response_fixture.json"


@pytest.fixture
def plugin(monkeypatch):
    """Fresh import of the plugin module — a flat ``__init__.py`` plugin, loaded by path like
    Hermes does, so nothing leaks between tests through module state."""
    spec = importlib.util.spec_from_file_location("hutch_x_search_under_test", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HUTCH_API_KEY", "hutch-test-key")
    monkeypatch.setenv("HUTCH_BASE_URL", "https://relay.test.example/v1")
    return mod


@pytest.fixture
def core():
    import tools.x_search_tool as core
    return core


class _Ctx:
    def __init__(self, raise_on_register: BaseException | None = None):
        self.calls: list[dict] = []
        self._raise = raise_on_register

    def register_tool(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise


def _response(status: int, body, *, json_body: bool = True) -> SimpleNamespace:
    """Minimal ``requests.Response`` stand-in: ``.json()``, ``.text``, ``.status_code``,
    ``.raise_for_status()`` raising a real ``requests.HTTPError`` carrying the response."""
    text = json.dumps(body) if json_body else str(body)
    resp = SimpleNamespace(status_code=status, text=text)

    def _json():
        if not json_body:
            raise ValueError("not json")
        return body

    def _raise():
        if status >= 400:
            err = requests.HTTPError(f"{status} error")
            err.response = resp
            raise err

    resp.json = _json
    resp.raise_for_status = _raise
    return resp


def _capture_post(monkeypatch, response):
    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return seen


# ---------------------------------------------------------------- registration


def test_register_overrides_builtin_x_search_with_core_schema(plugin, core):
    ctx = _Ctx()
    plugin.register(ctx)
    assert len(ctx.calls) == 1
    call = ctx.calls[0]
    assert call["name"] == "x_search" and call["toolset"] == "x_search"
    assert call["override"] is True
    assert call["schema"]["name"] == "x_search"
    assert call["schema"]["parameters"] is core.X_SEARCH_SCHEMA["parameters"], \
        "parameters must be the core object, not a copy"
    assert call["check_fn"] is plugin._check_hutch_x_search
    assert set(call["requires_env"]) == {"HUTCH_API_KEY", "HUTCH_BASE_URL"}


def test_description_states_relay_credentials_and_keeps_core_text(plugin, core):
    """The description is the only thing the model knows about the tool; the core's last
    sentence ("configure XAI_API_KEY") is the wrong advice through the relay."""
    ctx = _Ctx()
    plugin.register(ctx)
    desc = ctx.calls[0]["schema"]["description"]
    core_desc = core.X_SEARCH_SCHEMA["description"]
    assert "HUTCH_API_KEY" in desc and "HUTCH_BASE_URL" in desc
    assert "XAI_API_KEY" not in desc and "SuperGrok" not in desc
    head = core_desc.split(plugin._CORE_AVAILABILITY_MARKER)[0].rstrip()
    assert desc.startswith(head), "every core word before the availability sentence must survive verbatim"


def test_hutch_schema_appends_when_core_marker_is_absent(plugin):
    params = {"type": "object", "properties": {}}
    out = plugin._hutch_schema({"name": "x_search", "description": "Something unrelated.", "parameters": params})
    assert out["description"] == "Something unrelated. " + plugin._HUTCH_AVAILABILITY_SENTENCE
    assert out["parameters"] is params
    assert out["name"] == "x_search"
    for empty in ("", None):
        assert plugin._hutch_schema({"description": empty})["description"] == plugin._HUTCH_AVAILABILITY_SENTENCE
    assert plugin._hutch_schema({})["description"] == plugin._HUTCH_AVAILABILITY_SENTENCE


def test_hutch_schema_keeps_core_text_after_the_availability_sentence(plugin):
    """Only the availability sentence is replaced; a sentence core adds after it survives, and a
    marker at position 0 leaves no leading space."""
    core_like = f"Head text. {plugin._CORE_AVAILABILITY_MARKER} are configured (X or Y). Tail sentence."
    out = plugin._hutch_schema({"description": core_like})["description"]
    assert out == f"Head text. {plugin._HUTCH_AVAILABILITY_SENTENCE} Tail sentence."
    assert plugin._hutch_schema({"description": f"{plugin._CORE_AVAILABILITY_MARKER} only."})["description"] \
        == plugin._HUTCH_AVAILABILITY_SENTENCE


def test_hutch_schema_never_mutates_the_core_schema(plugin, core):
    """Without the override grant the STOCK tool keeps serving; a mutated core dict would leak
    the relay wording into it."""
    before = core.X_SEARCH_SCHEMA["description"]
    before_keys = dict(core.X_SEARCH_SCHEMA)
    out = plugin._hutch_schema(core.X_SEARCH_SCHEMA)
    assert out is not core.X_SEARCH_SCHEMA
    assert core.X_SEARCH_SCHEMA["description"] == before
    assert dict(core.X_SEARCH_SCHEMA) == before_keys
    assert "XAI_API_KEY" in core.X_SEARCH_SCHEMA["description"]


def _override_denials():
    """Both denial shapes Hermes can raise: the registry's plain PermissionError and the
    PluginContext's PluginToolOverrideError (the real class when the core tree is importable)."""
    yield PermissionError("denied by registry")
    try:
        from hermes_cli.plugins import PluginToolOverrideError
        yield PluginToolOverrideError("denied by context")
    except ImportError:  # pragma: no cover
        yield type("PluginToolOverrideError", (PermissionError,), {})("denied by context")


@pytest.mark.parametrize("exc", list(_override_denials()), ids=lambda e: type(e).__name__)
def test_register_survives_denied_override_with_actionable_warning(plugin, exc, caplog):
    """A missing tools.override grant must never break plugin loading; the log tells the operator
    exactly which config line to add AND carries the core's own reason (a PermissionError can also
    mean 'module active in multiple profiles')."""
    ctx = _Ctx(raise_on_register=exc)
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)  # must not raise
    assert len(ctx.calls) == 1
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert msgs and "allow_tool_override: true" in msgs[-1] and plugin.PLUGIN_ID in msgs[-1]
    assert "denied by" in msgs[-1]


def test_register_skips_cleanly_when_core_tool_missing(plugin, monkeypatch, caplog):
    def broken():
        raise ImportError("no tools.x_search_tool")

    monkeypatch.setattr(plugin, "_core", broken)
    ctx = _Ctx()
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)
    assert ctx.calls == []
    assert any("not importable" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- check_fn


def test_check_fn_requires_both_relay_variables(plugin, monkeypatch):
    monkeypatch.delenv("HUTCH_API_KEY", raising=False)
    monkeypatch.delenv("HUTCH_BASE_URL", raising=False)
    assert plugin._check_hutch_x_search() is False
    monkeypatch.setenv("HUTCH_API_KEY", "k")
    assert plugin._check_hutch_x_search() is False
    monkeypatch.setenv("HUTCH_BASE_URL", "https://relay.test.example/v1")
    assert plugin._check_hutch_x_search() is True
    monkeypatch.setenv("HUTCH_API_KEY", "   ")
    assert plugin._check_hutch_x_search() is False, "whitespace-only key is not a key"


# ---------------------------------------------------------------- request shape


def test_request_goes_to_relay_with_hutch_bearer_and_core_payload(plugin, monkeypatch):
    seen = _capture_post(monkeypatch, _response(200, json.loads(FIXTURE.read_text())))
    out = json.loads(plugin._handle_hutch_x_search({
        "query": "  Hermes Agent  ", "allowed_x_handles": ["NousResearch"],
        "from_date": "2026-09-01", "to_date": "2026-09-19", "enable_image_understanding": True,
    }))
    assert out["success"] is True
    assert seen["url"] == "https://relay.test.example/v1/responses"
    assert seen["headers"]["Authorization"] == "Bearer hutch-test-key"
    assert seen["headers"]["User-Agent"].startswith("hutch-x-search/")
    assert "xai" not in seen["headers"]["User-Agent"].lower()
    body = seen["json"]
    assert body["store"] is False
    assert body["input"] == [{"role": "user", "content": "Hermes Agent"}]
    tool = body["tools"][0]
    assert tool["type"] == "x_search"
    assert tool["allowed_x_handles"] == ["NousResearch"]
    assert tool["from_date"] == "2026-09-01" and tool["to_date"] == "2026-09-19"
    assert tool["enable_image_understanding"] is True
    assert "excluded_x_handles" not in tool


def test_model_comes_from_core_config_key_or_core_default(plugin, core, monkeypatch):
    seen = _capture_post(monkeypatch, _response(200, json.loads(FIXTURE.read_text())))
    monkeypatch.setattr(core, "_load_x_search_config", lambda: {})
    plugin._handle_hutch_x_search({"query": "q"})
    assert seen["json"]["model"] == core.DEFAULT_X_SEARCH_MODEL

    monkeypatch.setattr(core, "_load_x_search_config", lambda: {"model": "grok-4.6"})
    plugin._handle_hutch_x_search({"query": "q"})
    assert seen["json"]["model"] == "grok-4.6"


def test_reasoning_effort_forwarded_when_configured(plugin, core, monkeypatch):
    seen = _capture_post(monkeypatch, _response(200, json.loads(FIXTURE.read_text())))
    monkeypatch.setattr(core, "_get_x_search_reasoning_effort", lambda: "high")
    plugin._handle_hutch_x_search({"query": "q"})
    assert seen["json"]["reasoning"] == {"effort": "high"}


# ---------------------------------------------------------------- response parsing


def test_real_relay_response_parses_like_the_builtin(plugin, core, monkeypatch):
    """Fixture = a real relay answer (2026-09-19, ids/encrypted reasoning scrubbed)."""
    fixture = json.loads(FIXTURE.read_text())
    _capture_post(monkeypatch, _response(200, fixture))
    out = json.loads(plugin._handle_hutch_x_search({"query": "Grok Imagine Video 1.5"}))

    assert out["success"] is True
    assert out["provider"] == "hutch" and out["credential_source"] == "hutch"
    assert out["tool"] == "x_search"
    assert out["answer"].strip()
    assert out["inline_citations"] and all(c.get("url") for c in out["inline_citations"])
    assert out["inline_citations"] == core._extract_inline_citations(fixture), "parser must be core's"
    assert out["answer"] == core._extract_response_text(fixture)
    assert out["citations"] == []  # relay/xAI return inline annotations only
    assert out["degraded"] is False
    assert out["relay_model"] == fixture["model"]
    assert out["usage"]["server_side_tool_usage_details"]["x_search_calls"] >= 1
    # never leak the reasoning blobs into the tool result
    assert "encrypted_content" not in json.dumps(out)


def test_filters_without_any_citation_mark_result_degraded(plugin, monkeypatch):
    body = {"model": "grok-4.5-build", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "From memory."}]}]}
    _capture_post(monkeypatch, _response(200, body))
    out = json.loads(plugin._handle_hutch_x_search({"query": "q", "from_date": "2026-09-01"}))
    assert out["success"] is True
    assert out["degraded"] is True and "from_date" in out["degraded_reason"]

    out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
    assert out["degraded"] is False and out["degraded_reason"] is None


def test_non_object_json_body_is_a_json_error_not_an_exception(plugin, monkeypatch):
    for body in (["not", "a", "dict"], "done", None, 42):
        _capture_post(monkeypatch, _response(200, body))
        out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
        assert out["success"] is False and out["provider"] == "hutch"
        assert "non-object" in out["error"]


# ---------------------------------------------------------------- errors


def test_relay_http_error_surfaces_xai_code_with_hutch_provider(plugin, monkeypatch):
    body = {"code": "personal-team-blocked", "error": "spending-limit reached for this team"}
    _capture_post(monkeypatch, _response(403, body))
    out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
    assert out["success"] is False
    assert out["provider"] == "hutch" and out["tool"] == "x_search"
    assert "personal-team-blocked" in out["error"] and "spending-limit" in out["error"]
    assert out["error_type"] == "HTTPError"


def test_timeout_names_the_relay(plugin, monkeypatch):
    def timeout_post(*a, **k):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(requests, "post", timeout_post)
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
    assert out["success"] is False and out["error_type"] == "ReadTimeout"
    assert "relay" in out["error"].lower() and "timed out" in out["error"]


@pytest.mark.parametrize("args", [
    {"query": "q", "allowed_x_handles": ["a"], "excluded_x_handles": ["b"]},
    {"query": "q", "from_date": "2026-09-19", "to_date": "2026-09-01"},
    {"query": "q", "from_date": "yesterday"},
    {"query": "   "},
])
def test_invalid_input_is_rejected_before_any_http(plugin, monkeypatch, args):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not reach the relay"))
    out = json.loads(plugin._handle_hutch_x_search(args))
    assert "error" in out and not out.get("success")


def test_missing_relay_credentials_is_a_tool_error_without_http(plugin, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not reach the relay"))
    monkeypatch.delenv("HUTCH_BASE_URL")
    out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
    assert "HUTCH_BASE_URL" in out["error"]


def test_plugin_reads_no_env_or_config_at_import(monkeypatch):
    """Discovery imports plugins before .env is loaded; a frozen import-time read would break
    the relay URL for the whole process (lesson from hutch-provider)."""
    monkeypatch.delenv("HUTCH_API_KEY", raising=False)
    monkeypatch.delenv("HUTCH_BASE_URL", raising=False)
    sys.modules.pop("hutch_x_search_import_probe", None)
    spec = importlib.util.spec_from_file_location("hutch_x_search_import_probe", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._check_hutch_x_search() is False
    monkeypatch.setenv("HUTCH_API_KEY", "k")
    monkeypatch.setenv("HUTCH_BASE_URL", "https://relay.test.example/v1")
    assert mod._check_hutch_x_search() is True, "env must be read at call time, not import time"


# ---------------------------------------------------------------- core reuse / multiplex / packaging


def test_transport_is_the_core_retry_loop(plugin, core, monkeypatch):
    """The plugin must not reimplement POST/retries: the request has to travel through
    core._post_with_retries (so core's timeout/retry config and 5xx policy apply)."""
    fixture = json.loads(FIXTURE.read_text())
    seen: dict = {}

    def fake_post_with_retries(url, headers, payload):
        seen.update(url=url, headers=headers, payload=payload)
        return _response(200, fixture)

    monkeypatch.setattr(core, "_post_with_retries", fake_post_with_retries)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("bypassed core transport"))
    out = json.loads(plugin._handle_hutch_x_search({"query": "q"}))
    assert out["success"] is True
    assert seen["url"] == "https://relay.test.example/v1/responses"
    assert seen["headers"]["Authorization"] == "Bearer hutch-test-key"
    assert seen["payload"]["tools"][0]["type"] == "x_search"


def test_credentials_come_from_the_profile_secret_scope(plugin, monkeypatch):
    """Under a multiplexed gateway a profile's .env is an overlay that never reaches os.environ;
    the plugin must read through agent.secret_scope like the core tool does."""
    import agent.secret_scope as ss

    monkeypatch.delenv("HUTCH_API_KEY", raising=False)
    monkeypatch.delenv("HUTCH_BASE_URL", raising=False)
    assert plugin._check_hutch_x_search() is False
    token = ss.set_secret_scope({"HUTCH_API_KEY": "scoped-key", "HUTCH_BASE_URL": "https://relay.test.example/v1/"})
    try:
        assert plugin._check_hutch_x_search() is True
        assert plugin._api_key() == "scoped-key"
        assert plugin._base_url() == "https://relay.test.example/v1"  # trailing slash trimmed
    finally:
        ss.reset_secret_scope(token)
    assert plugin._check_hutch_x_search() is False


def test_manifest_version_matches_module_version(plugin):
    """PLUGIN_VERSION feeds the User-Agent; plugin.yaml feeds Hermes. A release bump must move both."""
    import yaml

    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert str(manifest["version"]) == plugin.PLUGIN_VERSION
    assert manifest["kind"] == "standalone"
    assert "tools.override" in manifest["capabilities"]
    assert set(manifest["requires_env"]) == {"HUTCH_API_KEY", "HUTCH_BASE_URL"}
    assert "provides_tools" not in manifest, "declaring a built-in name would only add a second validate failure"
