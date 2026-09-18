# hutch-grok-tools

Hermes' **Grok-backed tools, transported through the Hutch relay** instead of a direct xAI
account — one plugin, one endpoint (`{HUTCH_BASE_URL}/responses`), one key (`HUTCH_API_KEY`):

| Tool | Hermes mechanism | Why this shape |
|---|---|---|
| `x_search` (X / Twitter search) | **override** of the built-in tool, gated by the `tools.override` consent | core credentials it only from `xai-oauth` / `XAI_API_KEY` and origin-pins `XAI_BASE_URL` to `*.x.ai`; there is no provider abstraction to plug into |
| `web_search` | a regular **web-search provider** named `hutch`, selected with `web.search_backend: hutch` | web tools have a provider architecture — this is exactly what the plugin API is for; no override involved |
| `web_extract` | **not provided** | Grok returns a synthesis, not page text; keep `web.extract_backend` on an extract-capable provider (`firecrawl`, `exa`, `parallel`, `keenable`, `tavily`) — Hermes resolves the two capabilities independently |

Same tool names, same parameters, same result shapes, same `config.yaml` keys the built-ins use.
For the model nothing changes except that the tools work.

Formerly `hutch-x-search` (1.0.x). See **Upgrading** below.

## Setup

```bash
hermes plugins install smwbev/hutch-grok-tools
hermes plugins enable hutch-grok-tools        # answer YES to the tools.override prompt (x_search)
```

Relay variables (the same two `hutch-provider` / `hutch-media` use):

```
HUTCH_API_KEY=…
HUTCH_BASE_URL=https://relay.example.com/v1
```

Then, in `config.yaml` (or via `hermes tools` → Web Search & Scraping → **Hutch Web Search
(Grok via relay)**):

```yaml
web:
  search_backend: hutch          # set it explicitly — autodetect stops at the built-in ladder (ddgs wins)
  extract_backend: firecrawl     # any EXTRACT-capable provider; Grok cannot extract (see below)
```

If you pick **Hutch Web Search** in the `hermes tools` picker instead, note that the picker
writes the *shared* key `web.backend: hutch` — which would route `web_extract` to Hutch too and
fail with "search-only backend". Always set `web.extract_backend` explicitly.

For `x_search`, **turn the toolset on** — it is off by default and only auto-enables when xAI
credentials are present, which you no longer need:

```
hermes tools   →   🐦 X (Twitter) Search   →   enable
```

Restart Hermes (gateway / desktop backend).

### Granting `tools.override` without the interactive prompt

The consent prompt only runs in a TTY (fail-closed elsewhere). Equivalent config:

```yaml
plugins:
  entries:
    hutch-grok-tools:
      allow_tool_override: true   # x_search only; web_search never needs it
```

Without the grant the plugin logs one WARNING with exactly this snippet, leaves the stock
xAI-credentialed `x_search` in place — and **still registers the `hutch` web-search provider**.
The two integrations are independent.

### `web.extract_backend`

Grok's `web_search` tool answers with a synthesis; it does not return page text, so this plugin
declares `supports_extract() = False`. Point `web.extract_backend` at a provider that **does**
extract: `firecrawl`, `exa`, `parallel`, `keenable` or `tavily` — all five ship a free keyless
tier in Hermes, so no extra key is required. `ddgs`, `brave-free` and `searxng` are search-only
and will answer `web_extract` with a "search-only backend" error. With `search_backend: hutch`
and `extract_backend` on another provider, `web_search` and `web_extract` each take their own
path (per-capability resolution is core behaviour).

## Upgrading from hutch-x-search

The plugin id is the manifest name, and the `tools.override` grant is stored under
`plugins.entries.<id>` — so the grant made for `hutch-x-search` does **not** carry over.

```bash
hermes plugins remove hutch-x-search          # or delete ~/.hermes/plugins/hutch-x-search
hermes plugins install smwbev/hutch-grok-tools
hermes plugins enable hutch-grok-tools        # accept tools.override again
```

Then delete the orphaned `plugins.entries.hutch-x-search:` block from `config.yaml`. Your
`x_search.*` settings are unchanged. The old repository is archived with a pointer here.

## What the model gets

### `x_search`

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
- `relay_model` — the concrete upstream build that answered (the relay maps `grok-4.5` →
  `grok-4.5-build`).
- `usage` — the upstream usage block; `server_side_tool_usage_details.x_search_calls` tells you
  how many X searches the answer cost. The relay does not add a price field.
- Top-level `citations` is always empty on this path — xAI returns `url_citation` annotations
  inline, exactly what the built-in already reads into `inline_citations`. `degraded` is
  computed from both, same rule as core.

Errors keep the core shape with `"provider": "hutch"`; upstream xAI codes pass through
(`personal-team-blocked: …`), and a timeout names the relay.

### `web_search`

The standard Hermes rows — nothing Hutch-specific reaches the model:

```json
{"success": true, "data": {"web": [
  {"title": "Hermes Agent Documentation | Hermes Agent", "url": "https://hermes-agent.nousresearch.com/docs/", "description": "…", "position": 1}
]}}
```

Grok is asked for a JSON `results` object (`include: ["no_inline_citations"]` keeps it clean);
when it narrates instead, rows are rebuilt from `url_citation` annotations with the preceding
text as the description. Zero hits is a success with an empty list. HTTP errors carry the status
and the upstream body (`HTTP 402: {"code":"personal-team-blocked", …}`); an error envelope on a
200 is reported as a failure, not as "no results".

## Configuration

Both tools read the **same keys the built-ins use**; the only plugin-specific namespace is
`web.hutch.*`, mirroring the bundled `web.xai.*`:

```yaml
web:
  search_backend: hutch
  extract_backend: firecrawl      # or exa / parallel / keenable / tavily
  hutch:                          # optional
    model: grok-4.5               # default; grok-4.6 also carries web_search on the relay
    timeout: 90                   # seconds
    allowed_domains: []           # ≤ 5; cannot be combined with excluded_domains (xAI rule)
    excluded_domains: []
x_search:                         # exactly the core keys
  model: grok-4.5
  reasoning_effort: low
  timeout_seconds: 180
  retries: 2
plugins:
  entries:
    hutch-grok-tools:
      allow_tool_override: true
```

The relay's `/models` catalog is not consulted (it is unstable); pick a model that exists. The
bundled xAI provider's default `grok-build-0.1` is **unknown to the relay** (`400
model_not_found`) — do not copy it into `web.hutch.model`.

## Behaviour notes

- **Availability** of both tools follows `HUTCH_API_KEY` + `HUTCH_BASE_URL` (both required),
  read through Hermes' profile secret scope — a multiplexed gateway sees each profile's own
  `.env`. With this plugin enabled, local `xai-oauth` / `XAI_API_KEY` are not used by either tool.
- **`hermes tools` copy.** For `x_search` the Tools screen still says “requires xAI OAuth or
  XAI_API_KEY” — that text lives in core (`hermes_cli/tools_config.py`); the description the
  *model* sees is already correct. For `web_search` the `hutch` provider appears in the picker
  normally, through the provider's own setup schema.
- **`User-Agent`** is `hutch-grok-tools/<version>` on both paths, not Hermes' xAI-OAuth client
  string, so relay logs are honest about who is calling.
- No network in `is_available()` / `check_fn` (they run on every `hermes tools` repaint and
  toolset build); nothing is read from env or config at import time.
- Requires Hermes ≥ 0.21.3 (`requires_hermes` in the manifest). On an older tree the x_search
  override logs which core helpers are missing and registers nothing; the web provider is
  unaffected.

## Relay contract (verified 2026-09-19 against CLIProxyAPI `05391d7`)

- `POST /v1/responses` with `tools: [{"type": "x_search" | "web_search", …filters}]` is
  forwarded to xAI unchanged (`normalizeXAITool` drops only `tool_search` / `apply_patch`).
- xAI answers `200 completed`; `output[]` = `reasoning` items (encrypted, ignored) + `message`
  items. For `x_search`, `url_citation` annotations carry the posts (no top-level `citations`).
  For `web_search` with the JSON prompt, the JSON object arrives in `output_text`; annotations
  are still present as a fallback.
- `usage.server_side_tool_usage_details.{x_search_calls,web_search_calls}` are reported; no
  cost field.
- Only `grok-4.5` / `grok-4.6` (and the `x-ai/` prefixed ids) carry the server tools;
  `grok-build-0.1` → `400 model_not_found`.

## Tests

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
```

43 tests, no network. **x_search (26):** registration contract (name/toolset/override/core
parameters identity), graceful degradation when the grant is missing (both denial shapes incl.
the real `PluginToolOverrideError`) or the core module is absent — the web provider registers
regardless — `check_fn` semantics, request shape, transport must be core's retry loop, parsing
of a real relay answer against the core parsers, description rewrite (relay wording, verbatim
core head, tail preserved, fallback, no mutation of the core schema), degraded rule, non-object
bodies, upstream error pass-through, timeout wording, validation before any HTTP, no
import-time env reads, secret-scope resolution, manifest/module version lock.
**web_search (17):** provider identity (`hutch`, search-only), setup schema listing both relay variables and no xAI
OAuth hook, a failing web-half never rolls back the x_search override, `is_available()` with network calls forbidden, request shape (URL, bearer, UA,
`grok-4.5` default, `no_inline_citations`, prompt carries query and limit), `web.hutch.*`
config incl. domain filters, both-filters rejected before HTTP, missing credentials without
HTTP, a real relay answer with a JSON block parsed into ordered rows and cut by `limit`, the
annotations fallback with duplicate URLs collapsed, zero hits as success, HTTP error with
upstream code, error envelope on 200, non-JSON / non-object bodies, request errors naming the
relay, secret-scope credentials, fixture hygiene.

## The proper fix lives upstream

For `x_search` this plugin is a bridge: the right solution is an `x_search.provider:
<provider_id>` setting in Hermes core so the tool takes `base_url`/key from the provider
registry (like `image_gen.provider`), keeping origin-pinning for the OAuth bearer only. When
that lands, the override half of this plugin can go; the web provider is already the intended
shape.

## Changelog

- **1.1.0** — renamed to `hutch-grok-tools`; new `hutch` web-search provider
  (`web.search_backend: hutch`), independent of the x_search override grant; x_search tool
  description states availability in terms of `HUTCH_*`.
- **1.0.1** — description rewrite (released under the old name).
- **1.0.0** — first release as `hutch-x-search`.

## License

MIT — see `LICENSE`.
