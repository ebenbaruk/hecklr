# RFC 0023. `headers_env`: adapter header values from the environment

Date: 2026-10-24 · Status: accepted (2026-10-24) · Workstream: WShrun (issue #115) · Decided by:
architect (lead), pre-approved for the closing wave

## Problem

`docs/reference/configuration.md` promises that fields ending in `_env` name the environment
variable holding a secret, so secrets never go in `hecklr.yaml`. The `http` and `websocket` chat
adapters, the `webhook` adapter (chat and email) and the `webrtc` voice adapter authenticate only
through `headers: dict[str, str]`, whose values are literal. An agent behind
`Authorization: Bearer …` (the usual staging endpoint) can only be reached by committing its
token in `hecklr.yaml`, or by putting it in the URL, which then shows in agent error messages
stored in trials and reports (#115).

## Proposed change

Add one optional field to `HttpAdapterConfig`, `WebSocketAdapterConfig`, `WebhookAdapterConfig`
and `WebRTCAdapterConfig` in `hecklr_schemas/config.py`:

```python
headers_env: dict[str, EnvVarName] = Field(default_factory=dict)
"""Headers whose value is read from an environment variable: header name -> variable name."""
```

`hecklr_adapters.voice.WebSocketVoiceAdapterConfig` (RFC 0010a, still outside the schema) gains
the same field. Semantics, in the adapters:

- Each variable is read once, when the adapter is built (before any trial). An unset or empty
  variable is an `AdapterConfigError` naming the variable, never its value; `hecklr doctor`
  reports it the same way.
- A header may come from `headers` or `headers_env`, not both (compared case-insensitively):
  a clash is a configuration error rather than a silent precedence rule.
- Resolved values live only in the adapter's memory: they are not written back to the config,
  the run record, events, logs or reports.

`openai-compatible` keeps `api_key_env`, which already covers its `Authorization` header.

## Alternatives

- **`${VAR}` expansion inside `headers` values.** No schema change, but it changes the meaning
  of existing literal values containing `${`, and a typo silently sends the literal text (as the
  issue's repro shows). A separate field is explicit and validated by name pattern.
- **A per-adapter `auth_token_env` sending `Authorization: Bearer`.** Covers one scheme only;
  API keys in `X-Api-Key` and similar headers are as common.

## Migration

None. The field is optional with an empty default; every existing `hecklr.yaml` validates and
behaves as before. The JSON Schema export gains one property on four definitions.

## Decision (2026-10-24)

Accepted as proposed, as the one schema change of the closing wave. The `webrtc` voice adapter
is not built yet (`make_voice_target` refuses it), so its field is validated but unused until it
is.
