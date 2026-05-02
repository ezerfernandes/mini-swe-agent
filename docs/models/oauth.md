# OAuth subscription auth

!!! abstract "Overview"

    * Use Claude Pro/Max, ChatGPT Plus/Pro (Codex), and GitHub Copilot subscriptions instead of pay-per-token API keys.
    * Tokens are stored locally with mode `0600` and refreshed automatically when they expire.
    * Login / logout / refresh / list / status / token are exposed as `mini-extra oauth` subcommands.

!!! warning "Use according to each provider's terms of service"

    OAuth subscription tokens are issued for use with the provider's official clients. Check the relevant provider terms before pointing the agent at one.

## Supported providers

| Provider id        | Name                                | Auth flow                                    |
| ------------------ | ----------------------------------- | -------------------------------------------- |
| `anthropic`        | Claude Pro / Max                    | Authorization code + PKCE, localhost callback |
| `openai-codex`     | ChatGPT Plus / Pro (Codex)          | Authorization code + PKCE, localhost callback |
| `github-copilot`   | GitHub Copilot (incl. Enterprise)   | Device-code flow                             |

## Quick start

Log in once, drop a personal yaml, run `mini` against it.

```bash
mini-extra oauth login anthropic         # or openai-codex / github-copilot
```

`oauth_provider` is **required** — there is no CLI flag for it. Pass it via a yaml file (recommended) or `-c model.oauth_provider=...`.

### Pick the right model class

| provider         | model class       | example `model_name`                  |
| ---------------- | ----------------- | ------------------------------------- |
| `anthropic`      | `oauth`           | `anthropic/claude-sonnet-4-5-20250929` |
| `openai-codex`   | **`oauth_response`** (not `oauth`) | `openai/gpt-5`, `openai/gpt-5.1`, `openai/gpt-5.4` |
| `github-copilot` | `oauth`           | whatever your seat exposes (e.g. `claude-sonnet-4-5`) |

Codex requires `oauth_response` because the ChatGPT Plus/Pro backend only
mounts its models under the **Responses API** (`/responses`), not chat
completions. The plain `oauth` class hits `/chat/completions` and 404s for
every Codex model. See [Provider notes](#per-provider-notes) below for the
full reasoning.

### Personal override yaml (recommended)

```yaml
# ~/my-claude.yaml  — Anthropic Claude Pro/Max
model:
  model_class: oauth
  model_name: anthropic/claude-sonnet-4-5-20250929
  oauth_provider: anthropic
```

```yaml
# ~/my-codex.yaml  — ChatGPT Plus/Pro
model:
  model_class: oauth_response
  model_name: openai/gpt-5.4
  oauth_provider: openai-codex
  cost_tracking: ignore_errors   # silences "model not in pricing table" noise
```

Run with both the bundled defaults and your override:

```bash
mini \
  -c "$(MSWEA_SILENT_STARTUP=1 python -c 'from minisweagent.config import builtin_config_dir; print(builtin_config_dir / "mini.yaml")')" \
  -c ~/my-codex.yaml
```

`-c` files merge left-to-right; later ones win. **`-c` replaces the default config**, it does not merge on top of it — so you must always pass the bundled `mini.yaml` first, otherwise the run fails with `ValidationError: ... system_template / instance_template Field required`. The `python -c '...'` snippet prints the absolute path of the bundled file so you don't have to hard-code it.

### One-shot CLI

If you don't want a yaml file, pass the keys inline (still need the bundled config):

```bash
mini -m openai/gpt-5.4 --model-class oauth_response \
  -c "$(MSWEA_SILENT_STARTUP=1 python -c 'from minisweagent.config import builtin_config_dir; print(builtin_config_dir / "mini.yaml")')" \
  -c model.oauth_provider=openai-codex
```

## `mini-extra oauth` subcommands

```bash
mini-extra oauth login PROVIDER     # run the login flow + persist credentials
mini-extra oauth logout PROVIDER    # delete stored credentials
mini-extra oauth refresh PROVIDER   # force-refresh the access token now
mini-extra oauth list               # show all stored providers + expiries
mini-extra oauth status PROVIDER    # show current credential status
mini-extra oauth token PROVIDER     # print a fresh access token to stdout (scripts)
```

`mini-extra oauth token --refresh PROVIDER` forces a refresh before printing.
`mini-extra oauth login --no-browser PROVIDER` skips opening the browser (useful over SSH; paste the redirect URL manually).

## Per-provider notes

=== "Anthropic (Claude Pro/Max)"

    * Login binds a localhost listener on port `53692` (override with `MSWEA_ANTHROPIC_CALLBACK_PORT`).
    * The `oauth` model class injects the required `Authorization: Bearer <token>`, `anthropic-beta: claude-code-20250219,oauth-2025-04-20`, `user-agent: claude-cli/...`, and `x-app: cli` headers.
    * It also prepends the `"You are Claude Code, Anthropic's official CLI for Claude."` system message (required by Anthropic's OAuth path). Disable with `inject_claude_code_system: false` if you know what you are doing.

=== "OpenAI Codex (ChatGPT Plus/Pro)"

    **Always use `--model-class oauth_response`.** The ChatGPT Plus/Pro
    Codex backend serves models exclusively via the OpenAI Responses API.

    * Login binds a localhost listener on port `1455` (override with `MSWEA_CODEX_CALLBACK_PORT`).
    * Requests are sent to `https://chatgpt.com/backend-api/codex` by default — override with `MSWEA_CODEX_BASE_URL` or the `codex_base_url` config key.
    * The `originator` header defaults to `mini-swe-agent`; override with the `codex_originator` config key.
    * The model class enforces several backend-specific defaults you cannot disable (these are required for the request to be accepted):
        * `stream: true` — backend rejects `false` with `"Stream must be set to true"`.
        * `store: false` — backend rejects `true` for ChatGPT-account flows.
        * System prompt is moved out of `messages` into the top-level `instructions` field.
        * `tool_choice: "auto"`, `parallel_tool_calls: true`, and a default `reasoning: {effort: medium, summary: auto}` block — without these the model often emits prose and never calls the tool.
        * The bash tool is sent with `strict: false` (matches pi-mono's `convertResponsesTools(..., {strict: null})`).
        * Output is reassembled from streaming `response.output_item.done` events; the backend's `response.completed` event carries an empty `output` array, so the per-item events are the source of truth.
    * **Caller kwargs override these defaults.** To raise reasoning effort: `-c 'model.model_kwargs={"reasoning":{"effort":"high","summary":"auto"}}'`. To force a specific tool: `-c 'model.model_kwargs={"tool_choice":{"type":"function","name":"bash"}}'`.
    * **Allowed model ids** are gated by your subscription tier. ChatGPT Plus/Pro can call `openai/gpt-5`, `openai/gpt-5.1`, `openai/gpt-5.4`, `openai/codex-mini-latest`. **Not** allowed: `gpt-5-codex`, `gpt-5.1-codex-max` (API/org tier only — backend returns `"X model is not supported when using Codex with a ChatGPT account"`). Pi-mono's `models.generated.ts` (`provider: openai-codex` block) is the canonical list.
    * **Cosmetic noise to expect:**
        * Cost calc warns once per turn unless you set `cost_tracking: ignore_errors` — Codex models aren't in LiteLLM's pricing table.
        * Pydantic union-discriminator warnings are suppressed by the model module on import. If you see them, your import order is wrong.
        * Assistant turns may render with role `Unknown:` in interactive mode — pre-existing behavior of `LitellmResponseModel` (top-level response dump has no `role` field), not specific to OAuth.
    * **Debug:** set `MSWEA_OAUTH_RESPONSE_DEBUG=1` to dump the aggregated response payload to stderr after each call. Useful for diagnosing empty `output[]` or unexpected item types.

=== "GitHub Copilot"

    * Uses the GitHub device-code flow — no localhost listener.
    * The login prompt asks for your GitHub Enterprise URL / domain; leave blank for `github.com`.
    * Requests go to `https://api.individual.githubcopilot.com` by default; the base URL is derived from the token's `proxy-ep=` claim or, for enterprise, from `https://copilot-api.<domain>`.

## Troubleshooting

| symptom | cause | fix |
| --- | --- | --- |
| `oauth_provider must be one of [...], got None` | Used `--model-class oauth*` without setting `oauth_provider`. | Add `-c model.oauth_provider=<provider>` or set it in your yaml. |
| `ValidationError: ... system_template / instance_template Field required` | Passed any `-c` and dropped the bundled `mini.yaml`. | Re-pass the bundled config first: `-c "$(MSWEA_SILENT_STARTUP=1 python -c '...')"` then your override. |
| `litellm.NotFoundError: Error code: 404 - {'detail': 'Not Found'}` (Codex) | Used `--model-class oauth` (chat completions) instead of `oauth_response` (Responses API). | Switch to `--model-class oauth_response`. |
| `OpenAIException - {"detail":"Stream must be set to true"}` | Codex backend rejects non-streamed requests. | The `oauth_response` class always streams — if you see this you're on `oauth`. Switch classes. |
| `OpenAIException - {"detail":"The 'X' model is not supported when using Codex with a ChatGPT account."}` | Asked the Codex backend for a model your subscription tier can't reach (e.g. `gpt-5-codex`). | Use a ChatGPT-account-allowed id: `gpt-5`, `gpt-5.1`, `gpt-5.4`, `codex-mini-latest`. |
| Tool calls never happen — `No tool calls found in the response` loop | Reasoning model is replying in prose instead of calling the tool. | Defaults already nudge toward tool use; if it persists, try `-c 'model.model_kwargs={"reasoning":{"effort":"high","summary":"auto"}}'` or force the tool: `-c 'model.model_kwargs={"tool_choice":{"type":"function","name":"bash"}}'`. |
| Empty `output[]` in the response after streaming | The Codex backend's `response.completed` event always has empty output; per-item events carry the data. | Should not happen with `oauth_response` (it aggregates `output_item.done`). If it does, set `MSWEA_OAUTH_RESPONSE_DEBUG=1` and share the dump. |
| `Error calculating cost for model openai/gpt-5.X` | Codex models aren't in LiteLLM's pricing registry. | Set `cost_tracking: ignore_errors` in your yaml or `export MSWEA_COST_TRACKING=ignore_errors`. |
| `python -c '...'` snippet leaking the startup banner into a command-substitution path | Importing `minisweagent.config` triggers the banner on stdout. | Always prefix with `MSWEA_SILENT_STARTUP=1`, e.g. `MSWEA_SILENT_STARTUP=1 python -c '...'`. |

## Environment variables

```bash
# Path to the OAuth credentials file (default: $MSWEA_GLOBAL_CONFIG_DIR/oauth.json, mode 0600)
MSWEA_OAUTH_FILE="/path/to/oauth.json"

# Seconds to wait for the localhost callback before falling back to a paste prompt
# Used by the anthropic and openai-codex flows (default: 300)
MSWEA_OAUTH_CALLBACK_TIMEOUT="300"

# Localhost port used by the Anthropic OAuth callback (default: 53692)
MSWEA_ANTHROPIC_CALLBACK_PORT="53692"

# Localhost port used by the OpenAI Codex OAuth callback (default: 1455)
MSWEA_CODEX_CALLBACK_PORT="1455"

# Base URL for OpenAI Codex requests (default: https://chatgpt.com/backend-api/codex)
MSWEA_CODEX_BASE_URL="https://chatgpt.com/backend-api/codex"
```

The localhost hostname is hard-coded to `localhost` for both the Anthropic and Codex flows because the providers whitelist that exact string in their redirect URI allowlists.

## Storage layout

Credentials are persisted as JSON keyed by provider id:

```json
{
  "anthropic":      {"refresh": "...", "access": "...", "expires": 1735689600000},
  "openai-codex":   {"refresh": "...", "access": "...", "expires": 1735689600000, "account_id": "..."},
  "github-copilot": {"refresh": "...", "access": "...", "expires": 1735689600000, "enterprise_url": "..."}
}
```

`expires` is unix-millis. The file is created with mode `0600` and parents with `0700`. Override the path with `MSWEA_OAUTH_FILE`.

## Programmatic API

```python
from minisweagent import oauth

# List provider implementations
providers = oauth.get_oauth_providers()

# Run a login flow yourself with custom callbacks
creds = oauth.login_provider("anthropic", callbacks)

# Force a refresh from your own code (e.g. before a long batch run)
oauth.refresh_provider("anthropic")

# Get a fresh access token, refreshing on demand
token = oauth.get_oauth_api_key("anthropic")

# Subscribe to refresh events (e.g. to mirror the token to another store)
oauth.subscribe_refresh(lambda provider_id, creds: ...)
```

Public surface lives in `minisweagent.oauth` — see [the API reference](../reference/models/oauth.md) for full details.

{% include-markdown "../_footer.md" %}
