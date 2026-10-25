# Providers

A provider is a model endpoint and its key: where a harness's CLI sends its model calls. The key
is sealed the moment it arrives and opened only inside the gateway, per request; a harness in a
sandbox sees a per-turn session token and the gateway's address, never the key.

## Add one

From the command line, or the API:

```bash
hg providers put deepseek --url https://api.deepseek.com/anthropic --api anthropic-messages --key sk-…
```

```bash
curl -X PUT $HG/v1/providers/deepseek -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"url": "https://api.deepseek.com/anthropic", "api": "anthropic-messages", "key": "sk-…"}'
```

| Field | Meaning |
|---|---|
| name | `[a-z0-9][a-z0-9._-]*`; the middle segment of a model path |
| url | the endpoint's base, `https` only; the gateway forwards only that API shape's own paths under it |
| api | `anthropic-messages`, `openai-chat`, `openai-responses`, or `none` for an open endpoint without a key |
| key | the provider's secret; shown never again |

Owners add, replace and delete providers. The `api` names the wire shape the CLI expects, which
decides which endpoint to point it at. Common pairs:

| CLI | Shape | Endpoint |
|---|---|---|
| Claude Code, Pi, Oh My Pi, Kilo Code, Hermes Agent, DeepSeek Harness, Command Code | `anthropic-messages` | `https://api.anthropic.com` or DeepSeek's `https://api.deepseek.com/anthropic` |
| Cline and other OpenAI-compatible CLIs | `openai-chat` | `https://api.openai.com/v1` or DeepSeek's `https://api.deepseek.com/v1` |
| Codex | `openai-responses` | `https://api.openai.com/v1` |

Each [catalog package](catalog/index.md) says which shape it expects and names a default provider
in its `harness.json`; set up a provider with that name and the package works unchanged, or name
another in the request's model path.

## In the request

`model` chooses the provider: `harness` uses the package's default provider and model,
`harness/provider` that provider with the package's default model, and
`harness/provider/model` all three (the model last, so it may hold slashes of its own). A
continuation keeps the provider of the turn it continues unless the request names one.

## What the gateway does with a call

The CLI calls `$HG_PROVIDER_URL` (its `provider_env.url` name carries the same address) with the
session token as its key. The door in the sandbox's network forwards the call to the gateway; the
gateway checks the token against the turn, swaps it for the provider's key, allows only the API
shape's own paths (`/v1/messages` for Anthropic, `/v1/chat/completions`, `/v1/responses`,
`/v1/models` for OpenAI shapes), forwards over TLS, and keeps the exchange as the turn's capture
(bodies bounded at 4 MB),
readable at `GET /v1/responses/{id}/captures`. The key never appears in a capture.
