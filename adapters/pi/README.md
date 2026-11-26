# pi adapter (Mímir memory bridge)

`mimir.ts` is a pi extension that ports `adapters/claude-code/mimir_auto_capture.py` to pi. It does two things:

- **Inject memory.** On `session_start` and `session_compact` it fetches `/v8/hooks/session-start` and injects iron rules and user preferences into the system prompt. The injection is capped at 2400 chars.
- **Capture turns.** On `agent_settled` it posts the last user message (max 3000 chars) and the final assistant reply (max 5000 chars) to `/v8/hooks/session-end`. It uses `memory_mode: observe` and `retention_class: short`. Tool output is not captured. Duplicates are dropped by content hash and `idempotency_key`.

## Identity (required)

One agent, one identity.

- **`MIMIR_AGENT_ID` must be set.** There is no default. If it is unset, the extension logs `MIMIR_AGENT_ID not set; extension disabled` and does nothing.
- **Each agent uses its own token.** The token comes from `~/.mimir_<agent_id>.token`, mode 0600, issued for that agent. Never point `MIMIR_TOKEN_FILE` at another agent's token.
- **Captured turns are stored as `<agent_id>-bridge`.** That is the connector id `mimir-capture-check` watches.

## Environment

| Variable | Default | Notes |
|:--|:--|:--|
| `MIMIR_AGENT_ID` | — (required) | e.g. `brokkr` |
| `MIMIR_HOST` | `127.0.0.1` | Mímir API host; for a remote node use the n100 tailnet address |
| `MIMIR_PORT` | `8456` | |
| `MIMIR_TOKEN_FILE` | `~/.mimir_<agent_id>.token` | |
| `MIMIR_HOOK_LOG` | `~/.mimir_<agent_id>_hook.log` | |

## Install

1. Copy `mimir.ts` into the pi profile's `pi-agent/extensions/` directory, e.g. `~/.aiducat-<agent>/profiles/<agent>/pi-agent/extensions/`.
2. Set `MIMIR_AGENT_ID` (and `MIMIR_HOST` if Mímir is remote) in the bridge service environment.
3. Restart the agent's bridge. Then check the hook log for `bootstrap ... rules=N prefs=M`, and after one turn, for `session-end ok`.

> Brokkr (on Gungnir) was first deployed from a pre-repo copy whose defaults were `brokkr` and the n100 tailnet host. On any redeploy from this version, set `MIMIR_AGENT_ID=brokkr` and `MIMIR_HOST` explicitly.
