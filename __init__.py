"""hutch-x-search — the built-in Hermes ``x_search`` tool, transported through the Hutch relay.

Hermes ships ``x_search`` (``tools/x_search_tool.py``) wired to xAI's Responses API and
credentialed exclusively from ``xai-oauth`` or ``XAI_API_KEY``; ``XAI_BASE_URL`` is
origin-pinned to ``*.x.ai`` so the bearer cannot leak to a foreign host. That makes the tool
unusable for people whose xAI access is a relay (CLIProxyAPI) rather than a direct account.

This plugin re-registers the SAME tool — same name, toolset, schema, result shape — with
``override=True`` and swaps only the transport: ``POST {HUTCH_BASE_URL}/responses`` with the
Hutch key. Everything else (filter validation, payload, retries, response parsing) is imported
from the core module so the two never drift. The relay forwards the ``{"type": "x_search"}``
server tool to xAI untouched (CLIProxyAPI ``normalizeXAITool`` strips only ``tool_search`` /
``apply_patch``) and the answer comes back with ``url_citation`` annotations.

Overriding a built-in is operator-gated: the plugin declares ``tools.override`` in its
manifest (consent prompt on install/enable) and, when the grant is missing, degrades to
"nothing registered + one WARNING with the exact config line" instead of failing the load.

Nothing here reads env or config at import time — ``check_fn`` and the handler resolve
``HUTCH_*`` on every call (plugin discovery runs before ``.env`` is loaded, and one gateway
process may serve several profiles).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

PLUGIN_ID = "hutch-x-search"
PLUGIN_VERSION = "1.0.0"
PROVIDER = "hutch"

_OVERRIDE_HELP = (
    "hutch-x-search: overriding the built-in x_search tool is not permitted for this plugin. "
    "Grant it in config.yaml —\n"
    "    plugins:\n"
    "      entries:\n"
    f"        {PLUGIN_ID}:\n"
    "          allow_tool_override: true\n"
    "— or re-run `hermes plugins enable hutch-x-search` and accept the tools.override prompt. "
    "Until then the stock xAI-credentialed x_search stays in place."
)


# --------------------------------------------------------------------------- env
#
# Credentials go through ``agent.secret_scope`` exactly like the core tool does: under a
# multiplexed gateway each profile's ``.env`` is an overlay that never reaches ``os.environ``,
# so a bare ``os.environ.get`` would see nothing (or another profile's value). Outside
# multiplex ``get_secret_str`` falls through to ``os.environ``; the ``ImportError`` fallback is
# only for running the test-suite against trees without the module.


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret_str
    except ImportError:  # pragma: no cover — every supported Hermes has it
        return (os.environ.get(name) or "").strip()
    return (get_secret_str(name, "") or "").strip()


def _api_key() -> str:
    return _secret("HUTCH_API_KEY")


def _base_url() -> str:
    return _secret("HUTCH_BASE_URL").rstrip("/")


def _headers() -> Dict[str, str]:
    # Deliberately NOT ``hermes_xai_user_agent()``: that string marks a request as the Hermes
    # xAI-OAuth client, which is meaningless (and misleading in logs) on the relay.
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": f"hutch-x-search/{PLUGIN_VERSION}",
    }


def _check_hutch_x_search() -> bool:
    """Tool is offered only when both relay variables are present. No network here — the
    registry calls check_fns on every toolset build."""
    return bool(_api_key() and _base_url())


# --------------------------------------------------------------------------- core seams
#
# The heavy lifting lives in ``tools/x_search_tool.py``. Its helpers are underscore-private but
# in the same tree the plugin runs in; importing them is how the schema, payload and parser stay
# byte-identical to the built-in. Resolved lazily so a core rename degrades to a clear error at
# call time instead of an import-time crash of plugin loading.


def _core():
    """The core module with every seam this plugin borrows verified present.

    Those helpers are underscore-private and appeared together in Hermes 0.21.3; an older tree
    must produce one clear error naming the requirement, not an AttributeError from mid-handler.
    """
    import tools.x_search_tool as core  # noqa: WPS433 — late import is the point
    missing = [n for n in _CORE_SEAMS if not hasattr(core, n)]
    if missing:
        raise RuntimeError(
            f"tools.x_search_tool lacks {', '.join(missing)} — hutch-x-search needs Hermes >= 0.21.3"
        )
    return core


_CORE_SEAMS = (
    "X_SEARCH_SCHEMA", "DEFAULT_X_SEARCH_MODEL", "DEFAULT_X_SEARCH_TIMEOUT_SECONDS",
    "_build_x_search_tool_def", "_get_x_search_reasoning_effort", "_get_x_search_int",
    "_load_x_search_config", "_post_with_retries", "_extract_response_text",
    "_extract_inline_citations", "_http_error_message",
)


def _tool_error(message: str) -> str:
    try:
        from tools.registry import tool_error
        return tool_error(message)
    except Exception:  # pragma: no cover — registry always exists where tools run
        return json.dumps({"error": message}, ensure_ascii=False)


def _error_json(error: str, exc: BaseException) -> str:
    """Core ``_error_json`` hard-codes ``provider: xai``; this is the same 4-line shape with ours."""
    return json.dumps(
        {"success": False, "provider": PROVIDER, "tool": "x_search", "error": error,
         "error_type": type(exc).__name__},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- handler


def hutch_x_search_tool(
    query: str,
    allowed_x_handles: Optional[List[str]] = None,
    excluded_x_handles: Optional[List[str]] = None,
    from_date: str = "",
    to_date: str = "",
    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
) -> str:
    """Mirror of core ``x_search_tool`` with the credential/base-url step replaced by HUTCH_*."""
    if not query or not query.strip():
        return _tool_error("query is required for x_search")
    api_key, base_url = _api_key(), _base_url()
    if not api_key or not base_url:
        return _tool_error("hutch-x-search: HUTCH_API_KEY / HUTCH_BASE_URL are not set")

    try:
        core = _core()
    except Exception as exc:
        return _error_json(f"core x_search module unavailable: {exc}", exc)

    try:
        tool_def, active_filters = core._build_x_search_tool_def(
            allowed_x_handles, excluded_x_handles, from_date, to_date,
            enable_image_understanding, enable_video_understanding,
        )
        reasoning_effort = core._get_x_search_reasoning_effort()
    except ValueError as exc:
        return _tool_error(str(exc))

    try:
        # Same config keys as the built-in (``x_search.model`` etc.) — no plugin-specific knobs, so
        # the user's config stays valid when this override is retired in favour of an upstream
        # ``x_search.provider`` setting. The relay catalog is NOT consulted (unstable).
        model = (str(core._load_x_search_config().get("model") or "").strip()
                 or core.DEFAULT_X_SEARCH_MODEL)
        payload: Dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": query.strip()}],
            "tools": [tool_def],
            "store": False,
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}

        data = core._post_with_retries(f"{base_url}/responses", _headers(), payload).json()
        if not isinstance(data, dict):
            raise ValueError(f"relay returned a non-object response body ({type(data).__name__})")

        citations = list(data.get("citations") or [])
        inline_citations = core._extract_inline_citations(data)
        # Same rule as core: a 200 with narrowing filters and no citations of either kind means
        # the answer came from training data, not from X.
        degraded = bool(active_filters) and not citations and not inline_citations
        result = {
            "success": True,
            "provider": PROVIDER,
            "credential_source": PROVIDER,
            "tool": "x_search",
            "model": model,
            # Upstream reports the concrete build it served (``grok-4.5-build`` for ``grok-4.5``).
            "relay_model": data.get("model"),
            "query": query.strip(),
            "answer": core._extract_response_text(data),
            "citations": citations,
            "inline_citations": inline_citations,
            "degraded": degraded,
            "degraded_reason": (
                f"no citations returned despite filters: {', '.join(active_filters)}" if degraded else None
            ),
            # ``server_side_tool_usage_details.x_search_calls`` lives here; the relay does not
            # add a cost field, so none is promised.
            "usage": data.get("usage"),
        }
        return json.dumps(result, ensure_ascii=False)
    except requests.HTTPError as exc:
        logger.error("hutch x_search failed: %s", exc, exc_info=True)
        return _error_json(core._http_error_message(exc), exc)
    except requests.ReadTimeout as exc:
        logger.error("hutch x_search timed out: %s", exc, exc_info=True)
        timeout = core._get_x_search_int("timeout_seconds", core.DEFAULT_X_SEARCH_TIMEOUT_SECONDS, 30)
        return _error_json(f"Hutch relay x_search timed out after {timeout} seconds", exc)
    except Exception as exc:
        logger.error("hutch x_search failed: %s", exc, exc_info=True)
        return _error_json(str(exc), exc)


def _handle_hutch_x_search(args: Dict[str, Any], **_kw: Any) -> str:
    return hutch_x_search_tool(
        args.get("query", ""), args.get("allowed_x_handles"), args.get("excluded_x_handles"),
        args.get("from_date", ""), args.get("to_date", ""),
        bool(args.get("enable_image_understanding", False)),
        bool(args.get("enable_video_understanding", False)),
    )


# --------------------------------------------------------------------------- registration


def register(ctx: Any) -> None:
    try:
        core = _core()
        schema = core.X_SEARCH_SCHEMA
    except Exception as exc:
        logger.warning("hutch-x-search: core x_search tool not importable (%s); nothing registered", exc)
        return

    try:
        ctx.register_tool(
            name="x_search",
            toolset="x_search",
            schema=schema,
            handler=_handle_hutch_x_search,
            check_fn=_check_hutch_x_search,
            requires_env=["HUTCH_API_KEY", "HUTCH_BASE_URL"],
            emoji="🐦",
            override=True,
        )
    except PermissionError as exc:
        # Both gates — PluginContext.register_tool (PluginToolOverrideError) and
        # tools.registry.register (plain PermissionError) — derive from PermissionError.
        # Missing grant is an operator decision, not a plugin failure: warn with the fix, keep
        # loading. The core's own message is appended because a PermissionError can also mean
        # "plugin module active in multiple profiles" — the snippet alone would mislead there.
        logger.warning("%s\n(core said: %s)", _OVERRIDE_HELP, exc)
        return
    logger.info("hutch-x-search: x_search now routes through the Hutch relay")
