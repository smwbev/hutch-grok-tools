"""``hutch`` web-search provider: Grok's ``web_search`` server tool, reached through the Hutch relay.

Registered with ``ctx.register_web_search_provider`` under its own name — the web tools have a
provider architecture, so no override is involved; the user selects it with
``web.search_backend: hutch`` (plugin providers are never auto-detected). Extract is NOT
supported: Grok returns a synthesis, not page text, so ``web.extract_backend`` stays on any
other provider (Hermes resolves the two capabilities independently).

The request/response logic mirrors the bundled ``plugins/web/xai`` provider (prompt asking for a
JSON ``results`` object, ``include: ["no_inline_citations"]``, JSON-block → annotations →
citations parsing) — copied, not imported: that file is a PLUGIN-COMPAT pointer scheduled for
removal, and its credential path (xAI OAuth refresh) has no meaning on the relay. Two
deliberate differences: the default model is ``grok-4.5`` (the bundled ``grok-build-0.1`` is
unknown to the relay → 400) and the user agent names this plugin.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from plugins.web._common import BaseWebSearchProvider, search_fail, search_ok, setup_schema, title_hit

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-4.5"
DEFAULT_TIMEOUT = 90.0
_MAX_DOMAIN_FILTERS = 5
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _coerce(cast, value: Any, default: Any) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _coerce_domain_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][:_MAX_DOMAIN_FILTERS]


def _load_web_config() -> Dict[str, Any]:
    """``config.yaml → web.hutch`` (``{}`` on miss). Read per call, never at import."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        for key in ("web", "hutch"):
            cfg = cfg.get(key) if isinstance(cfg, dict) else None
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("hutch web search: could not load web.hutch config: %s", exc)
        return {}


class HutchWebSearchProvider(BaseWebSearchProvider):
    NAME = "hutch"
    DISPLAY_NAME = "Hutch Web Search (Grok via relay)"
    EXTRACT = False
    KEYLESS = False

    def __init__(self, api_key_fn, base_url_fn, user_agent: str):
        # Credential readers are injected by the plugin root so both tools resolve HUTCH_*
        # the same way (profile secret scope, call-time). No network in is_available():
        # it runs on every ``hermes tools`` repaint.
        self._api_key = api_key_fn
        self._base_url = base_url_fn
        self._user_agent = user_agent

    def is_available(self) -> bool:
        return bool(self._api_key() and self._base_url())

    # ------------------------------------------------------------------ search

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return search_fail("Interrupted")
        except Exception:  # noqa: BLE001 — interrupt module is best-effort
            pass
        api_key, base_url = self._api_key(), self._base_url()
        if not api_key or not base_url:
            return search_fail("Hutch relay is not configured: set HUTCH_API_KEY and HUTCH_BASE_URL")
        # Same clamp range as web_search_tool so explicit limits aren't downgraded.
        limit = max(1, min(_coerce(int, limit, 5), 100))
        cfg = _load_web_config()
        model = (cfg["model"].strip() if isinstance(cfg.get("model"), str) else "") or DEFAULT_MODEL
        tool = self._web_search_tool(cfg)
        if tool is None:
            # xAI rejects the combination — say so before spending a request.
            return search_fail("web.hutch.allowed_domains and web.hutch.excluded_domains cannot both be set (xAI restriction)")
        payload: Dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": self._build_prompt(query, limit)}],
            "tools": [tool],
            # keeps the JSON block clean; URLs come from annotations when the model narrates instead
            "include": ["no_inline_citations"],
        }
        timeout = _coerce(float, cfg.get("timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)
        logger.info("hutch web search: %r (limit=%d, model=%s)", query, limit, model)
        data, error = self._post_responses(base_url, payload, api_key, timeout)
        if error:
            return error
        # xAI sometimes answers 200 with an error envelope (overloaded, refusal); surfacing it
        # beats "success with zero rows".
        api_error = data.get("error") if isinstance(data, dict) else None
        if isinstance(api_error, dict):
            err_msg = api_error.get("message") or api_error.get("code") or "unknown error"
            logger.warning("hutch web search: relay returned error envelope: %s", err_msg)
            return search_fail(f"Hutch relay returned an error: {err_msg}")
        if not isinstance(data, dict):
            return search_fail(f"Hutch relay returned a non-object response body ({type(data).__name__})")
        # Empty list on 0 hits is a success (matches the other providers).
        return search_ok(self._extract_results(data, limit=limit))

    @staticmethod
    def _web_search_tool(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        filters = {k: _coerce_domain_list(cfg.get(k)) for k in ("allowed_domains", "excluded_domains")}
        filters = {k: v for k, v in filters.items() if v}
        if len(filters) == 2:
            return None
        return {"type": "web_search", "filters": filters} if filters else {"type": "web_search"}

    def _post_responses(self, base_url: str, payload: Dict[str, Any], api_key: str, timeout: float):
        """``(parsed_json, None)`` or ``(None, failure_envelope)``. Single attempt: the relay key
        is static, so the bundled provider's 401→OAuth-refresh retry has nothing to refresh."""
        try:
            import httpx
        except ImportError:
            return None, search_fail("httpx is not installed (required for Hutch web search)")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        try:
            resp = httpx.post(f"{base_url}/responses", headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            try:
                body = exc.response.text[:300] if exc.response is not None else ""
            except Exception:  # noqa: BLE001
                body = ""
            logger.warning("hutch web search HTTP %d: %s", status, body)
            return None, search_fail(f"Hutch relay web search returned HTTP {status}: {body}".rstrip())
        except httpx.RequestError as exc:
            logger.warning("hutch web search request error: %s", exc)
            return None, search_fail(f"Could not reach the Hutch relay: {exc}")
        try:
            return resp.json(), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("hutch web search bad JSON: %s", exc)
            return None, search_fail("Could not parse the Hutch relay Responses API reply as JSON")

    # ------------------------------------------------------------------ prompt / parsing

    @staticmethod
    def _build_prompt(query: str, limit: int) -> str:
        return (
            "Use the web_search tool to find current information for the query below, then respond with ONLY a single "
            "JSON object — no prose, no markdown fences, no inline citation links — matching this exact schema:\n\n"
            '{"results": [{"title": "string", "url": "string", "description": "1-2 sentence summary"}]}\n\n'
            f'Return at most {limit} results, ordered by relevance, with absolute https:// URLs. If no usable results exist, return '
            '{"results": []}.\n\n'
            f"Query: {query}"
        )

    @classmethod
    def _extract_results(cls, response_data: Dict[str, Any], *, limit: int) -> List[Dict[str, Any]]:
        """Rows in order of preference: (1) the JSON object in ``output_text`` blocks, (2)
        ``url_citation`` annotations paired with surrounding text, (3) a top-level
        ``citations`` list (absent on the Responses API today; harmless)."""
        text_blocks, annotations = cls._collect_output_text(response_data)
        parsed = next((p for p in (cls._try_parse_json_results(b, limit=limit) for b in text_blocks) if p), None)
        if parsed:
            return parsed
        if annotations:
            from_ann = cls._results_from_annotations(annotations, "\n".join(text_blocks), limit=limit)
            if from_ann:
                return from_ann
        citations = response_data.get("citations") or []
        if not isinstance(citations, list):
            return []
        return [title_hit("", str(u), "", i + 1) for i, u in enumerate(citations[:limit]) if isinstance(u, str) and u.strip()]

    @staticmethod
    def _collect_output_text(response_data: Dict[str, Any]):
        output = response_data.get("output")
        chunks = [
            chunk
            for item in (output if isinstance(output, list) else [])
            if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), list)
            for chunk in item["content"]
            if isinstance(chunk, dict) and chunk.get("type") == "output_text"
        ]
        text_blocks = [c["text"] for c in chunks if isinstance(c.get("text"), str) and c["text"].strip()]
        annotations = [a for c in chunks if isinstance(c.get("annotations"), list) for a in c["annotations"] if isinstance(a, dict)]
        return text_blocks, annotations

    @staticmethod
    def _try_parse_json_results(text: str, *, limit: int) -> Optional[List[Dict[str, Any]]]:
        """Whole string first, then the regex-matched block (reasoning models prefix narration)."""
        match = _JSON_BLOCK_RE.search(text)
        for candidate in [text] + ([match.group(0)] if match and match.group(0) != text else []):
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            results = parsed.get("results") if isinstance(parsed, dict) else None
            if not isinstance(results, list):
                continue
            normalized: List[Dict[str, Any]] = []
            for row in results[:limit]:
                url = str(row.get("url", "")).strip() if isinstance(row, dict) else ""
                if url:
                    normalized.append(title_hit(str(row.get("title", "")).strip(), url,
                                                str(row.get("description", "")).strip(), len(normalized) + 1))
            if normalized:
                return normalized
        return None

    @staticmethod
    def _results_from_annotations(annotations: List[Dict[str, Any]], joined_text: str, *, limit: int) -> List[Dict[str, Any]]:
        """URL plus ~200 chars of preceding text as the description (the annotation title is a number)."""
        seen: set[str] = set()
        results: List[Dict[str, Any]] = []
        for ann in annotations:
            url = str(ann.get("url", "")).strip() if ann.get("type") == "url_citation" else ""
            if not url or url in seen:
                continue
            seen.add(url)
            description = ""
            start, end = ann.get("start_index"), ann.get("end_index")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(joined_text):
                description = joined_text[max(0, start - 200):start].strip()[-200:].strip()
            results.append(title_hit("", url, description, len(results) + 1))
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------ hermes tools picker

    def get_setup_schema(self) -> Dict[str, Any]:
        # No post_setup hook: the bundled provider's ``xai_grok`` hook starts an xAI OAuth login,
        # which is exactly the path this plugin exists to avoid. Both variables are listed so the
        # picker's readiness matches is_available() (key alone is not enough).
        schema = setup_schema(
            self.DISPLAY_NAME, "subscription",
            "Agentic web search via Grok's web_search tool on the Hutch relay — uses HUTCH_API_KEY / HUTCH_BASE_URL. "
            "Search only: keep web.extract_backend on an extract-capable provider.",
            key_env="HUTCH_API_KEY", prompt="Hutch relay API key",
        )
        schema["env_vars"].append({"key": "HUTCH_BASE_URL", "prompt": "Hutch relay base URL (…/v1)", "url": ""})
        return schema
