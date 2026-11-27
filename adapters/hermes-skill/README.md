# Hermes `mimir-system` skill

Repo copy of the Hermes skill that teaches Hermes profiles how to use Mímir,
plus its two client scripts. Production lives at
`~/.hermes/skills/system/mimir-system/` on n100 (1.9.1 backlog item 12).

| File | Purpose |
|:--|:--|
| `mimir-system/SKILL.md` | Access and ops guide loaded by Hermes |
| `mimir-system/references/claude-code-auto-memory-hook.md` | How to wire the auto-memory hook into Claude Code |
| `mimir-system/scripts/mimir-auto-hook.py` | Legacy Claude Code auto-memory hook for remote agents |
| `mimir-system/scripts/mimir-remember.sh` | One-shot write client: `mimir-remember <domain> <fact_type> "text"` |

Differences from production, kept deliberately:

- `MIMIR_HOST` defaults to `127.0.0.1` instead of the n100 tailnet address;
  a remote host must set `MIMIR_HOST`.
- The token-registry and `federation.agents` examples use `thor` instead of the
  retired `claudecode`.

The current Claude Code hook for Thor is `adapters/claude-code/`. The older
per-profile copies under `~/.hermes/profiles/*/skills/system/mimir-system/`
(v7.0.0) are stale and not tracked here.

Odin's local wrapper `~/.claude/hooks/mimir_auto_hook.py` on sanjose is not in
the repo yet: n100 has no SSH key for that host, so it has to be copied from
the Odin side.
