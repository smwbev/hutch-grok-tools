# hutch-x-search

The built-in Hermes **`x_search`** tool (X / Twitter search via xAI's server-side `x_search`),
transported through the **Hutch relay** instead of a direct xAI account.

Same tool name, same parameters, same result shape, same `config.yaml → x_search.*` settings. The
only things that change are the endpoint (`{HUTCH_BASE_URL}/responses`) and the credential
(`HUTCH_API_KEY`). For the model — and for you — nothing changes except that it works.

## Why a plugin, and why an override

`tools/x_search_tool.py` in Hermes core credentials the tool **only** from `xai-oauth` or
`XAI_API_KEY`, and `XAI_BASE_URL` is origin-pinned to `*.x.ai` (a bearer-leak guard). There
is no `x_search.provider` setting yet, so a relay cannot be selected by configuration. If your
xAI access lives on a relay — or your direct account answers `403 personal-team-blocked:
spending-limit` because `x_search` is API-metered — the stock tool is dead weight.

Hermes' plugin API allows replacing a built-in tool with `register_tool(..., override=True)`,
gated by operator consent (`tools.override`). This plugin uses exactly that and imports the
schema, payload builder, retry loop and response parser **from the core module**, so it can
never drift from the built-in. It is deliberately a separate package from
[`hutch-provider`](https://github.com/smwbev/hutch-provider): plugins that override built-ins
are not admitted to the Hermes plugin catalog, and users of the provider who never touch X
should not be asked to grant `tools.override`.

## Setup

```bash
hermes plugins install smwbev/hutch-x-search
hermes plugins enable hutch-x-search        # answer YES to the tools.override prompt
```

Then make sure the relay variables exist (they are the same two `hutch-provider` and
`hutch-media` use):

```
HUTCH_API_KEY=…
HUTCH_BASE_URL=https://relay.example.com/v1
```

and **turn the toolset on** — `x_search` is off by default in Hermes and only auto-enables when
xAI credentials are present, which you no longer need:

```
hermes tools   →   🐦 X (Twitter) Search   →   enable
```

Restart Hermes (gateway / desktop backend). From now on every `x_search` call goes through the
relay.

### Granting `tools.override` without the interactive prompt

The consent prompt only runs in a TTY (fail-closed elsewhere). Equivalent config:

```yaml
plugins:
  entries:
    hutch-x-search:
      allow_tool_override: true
```

Without the grant the plugin logs one WARNING with exactly this snippet and registers nothing;
the stock xAI-credentialed `x_search` stays in place and Hermes keeps working.

## What the model gets

Identical to the built-in, plus two diagnostic fields:

```json
{
  "success": true,
  "provider": "hutch",
  "credential_source": "hutch",
  "tool": "x_search",
  "model": "grok-4.5",
  "relay_model": "grok-4.5-build",
  "query": "Grok Imagine Video 1.5",
  "answer": "…markdown with [[n]](https://x.com/…/status/…) links…",
  "citations": [],
  "inline_citations": [{"url": "https://x.com/…/status/…", "title": "…", "start_index": 0, "end_index": 12}],
  "degraded": false,
  "degraded_reason": null,
  "usage": {"server_side_tool_usage_details": {"x_search_calls": 10, "…": "…"}}
}
```

- **The tool description the model sees** is the core text with the last sentence replaced:
  availability is stated in terms of `HUTCH_API_KEY` / `HUTCH_BASE_URL`, not xAI credentials
  — so a failing call never ends in "configure XAI_API_KEY" advice. Parameters are the core
  object, untouched.
- `relay_model` — the concrete upstream build that answered (useful when the relay maps
  `grok-4.5` → `grok-4.5-build`).
- `usage` — the upstream usage block; `server_side_tool_usage_details.x_search_calls` tells you
  how many X searches the answer cost. The relay does not add a price field.
- Top-level `citations` is always empty on this path — xAI returns `url_citation` annotations
  inline, exactly what the built-in already reads into `inline_citations`. `degraded` is
  computed from both, same rule as core.

Errors keep the core shape with `"provider": "hutch"`; upstream xAI codes pass through
(`personal-team-blocked: …`), and a timeout names the relay.

## Configuration

Read from the **same keys as the built-in** — nothing plugin-specific, so the day Hermes grows
an `x_search.provider` setting you delete this plugin and your config is still valid:

```yaml
x_search:
  model: grok-4.5            # default = core default; grok-4.6 also available on the relay
  reasoning_effort: low      # optional
  timeout_seconds: 180       # floor 30
  retries: 2
```

The relay's `/models` catalog is not consulted (it is unstable); pick a model that exists.

## Behaviour notes

- **Tool availability** follows `HUTCH_API_KEY` + `HUTCH_BASE_URL` (both required) — not
  xAI credentials. With this plugin granted and enabled, local `xai-oauth` / `XAI_API_KEY` are
  no longer used for `x_search` at all.
- **UI text lag — `hermes tools` only.** The Tools screen still labels the toolset “requires
  xAI OAuth or XAI_API_KEY” and lists two xAI backends — that copy lives in core
  (`hermes_cli/tools_config.py`), the plugin cannot change it. Ignore it; the tool works with
  the relay variables alone. The tool description the *model* sees is already correct (above).
- **`User-Agent`** is `hutch-x-search/<version>`, not Hermes' xAI-OAuth client string, so relay
  logs are honest about who is calling.
- Nothing is read from env or config at import time (plugin discovery runs before `.env` is
  loaded; one gateway may serve several profiles).
- Requires Hermes ≥ 0.21.3 (the core helpers this plugin borrows). On an older tree it registers
  nothing and logs which names are missing.

## Relay contract (verified 2026-09-19 against CLIProxyAPI `05391d7`)

- `POST /v1/responses` with `tools: [{"type": "x_search", …filters}]` is forwarded to xAI
  unchanged (`normalizeXAITool` drops only `tool_search` / `apply_patch`).
- xAI answers `200 completed` with `output[]` = `reasoning` items (encrypted, ignored) +
  `message` items carrying `url_citation` annotations. No top-level `citations`.
- Filters (`allowed_x_handles`, `excluded_x_handles`, `from_date`, `to_date`, image/video
  understanding) pass through inside the tool object.
- Only `grok-4.5` / `grok-4.6` (and the `x-ai/` prefixed ids) carry the `x_search` server tool.

## Tests

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
```

26 tests, no network: registration contract (name/toolset/override/core parameters identity),
graceful degradation when the grant is missing or the core module is absent, `check_fn`
semantics, request shape (URL, bearer, payload, filters, `store: false`, model/config
precedence, reasoning effort), parsing of a **real relay response** captured 2026-09-19
(ids and encrypted reasoning scrubbed), description rewrite (relay wording, core parameters by identity, fallback when core text changes, no mutation of the core schema), degraded detection, non-object bodies, upstream error
pass-through, timeout wording, input validation before any HTTP, no import-time env reads.

## The proper fix lives upstream

This plugin is a bridge. The right solution is an `x_search.provider: <provider_id>` setting
in Hermes core so the tool takes `base_url`/key from the provider registry (like
`image_gen.provider`), keeping origin-pinning for the OAuth bearer only. When that lands,
uninstall this plugin; nothing else needs to change.

## Changelog

- **1.0.1** — the tool description the model sees now states availability in terms of
  `HUTCH_API_KEY` / `HUTCH_BASE_URL` instead of xAI credentials (core parameters untouched,
  core schema never mutated). No behaviour change.
- **1.0.0** — first release.

## License

MIT — see `LICENSE`.
