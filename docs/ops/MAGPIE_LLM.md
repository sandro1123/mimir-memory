# Governance & extraction LLM via Magpie (1.9.0)

> Status: **proposal — not applied**. Apply only after the owner approves.
> Why: production governance and extraction both read `secrets/evaluator.env`,
> whose 10Router key now returns **401** (extract: 43×401 in 6h on 2026-11-24;
> governance: every LLM assessment fails). Switch both to the dedicated Magpie
> key `Mimir-Memory`.

## 1. Secret (already in place, mode 600, never committed)

`~/.hermes/mimir/secrets/magpie_mimir.env`

```
MIMIR_ROUTER_API_KEY=<Mimir-Memory key>
MIMIR_EVALUATOR_API_KEY=<same key>
```

Governance reads `MIMIR_ROUTER_API_KEY`, extraction reads `MIMIR_EVALUATOR_API_KEY`;
systemd cannot alias variables, so the one secret file carries both names.

## 2. Magpie key policy (set in Magpie UI on the Magpie host)

| Setting | Value | Reason |
|---|---|---|
| Models | `group/auto-deepseek-v4-1-flash-free`, `group/glm-5.3-flash`, `deepaicode/*` | verified 2026-11-24: off-list models → 403 |
| MCP / skills / instructions | none | measured: 181 prompt tokens for a bare "OK" (no library injection) |
| Masking | on (second layer; Mímir DLP stays first) | |

Known gap: `deepaicode/*` also admits expensive models (e.g. `claude-opus-5-5`).
Narrowing to `deepaicode/deepseek-v4.1-flash` + `deepaicode/glm-5.3-flash` is
recommended once confirmed nothing else needs them.

## 3. Drop-in (identical for both units)

`/etc/systemd/system/mimir-v9.2-governance.service.d/20-magpie.conf`
`/etc/systemd/system/mimir-v12-extract.service.d/20-magpie.conf`

```ini
[Service]
# 1.9.0: LLM via Magpie key "Mimir-Memory" (replaces 401-ing 10Router key).
# Loaded after evaluator.env, so these values win. Remove this file to roll back.
EnvironmentFile=%h/.hermes/mimir/secrets/magpie_mimir.env
Environment=MIMIR_ROUTER_URL=http://<magpie-host>:3430/v1
Environment=MIMIR_EVALUATOR_API_URL=http://<magpie-host>:3430/v1
Environment=MIMIR_GOVERNANCE_MODEL=deepaicode/deepseek-v4.1-flash
Environment=MIMIR_GOVERNANCE_FALLBACK_MODELS=group/auto-deepseek-v4-1-flash-free,group/glm-5.3-flash
Environment=MIMIR_EVALUATOR_MODEL=deepaicode/deepseek-v4.1-flash
Environment=MIMIR_GOVERNANCE_TIMEOUT_S=45
Environment=MIMIR_EVALUATOR_TIMEOUT_S=45
Environment=MIMIR_EVALUATOR_MAX_TOKENS=1024
```

(`%h` is not expanded in system units — write the absolute home path when applying.)

## 4. Apply / verify / roll back

```bash
sudo systemctl daemon-reload
sudo systemctl start mimir-v9.2-governance.service   # one-shot run
journalctl -u mimir-v9.2-governance.service -n 5 --no-pager   # expect exit 0, llm_failures 0
sudo systemctl start mimir-v12-extract.service
journalctl -u mimir-v12-extract.service -n 20 --no-pager | grep -c 401   # expect 0
# rollback
sudo rm /etc/systemd/system/mimir-*.service.d/20-magpie.conf && sudo systemctl daemon-reload
```

## 5. Model choice evidence

`scripts/governance_model_eval.py`, real `EVALUATION_PROMPT`, decided candidates
(committed vs rejected), 2026-11-24:

| Model | JSON ok | Agreement | p50 latency |
|---|---|---|---|
| deepaicode/deepseek-v4.1-flash (Mimir-Memory key, n=16) | 16/16 | 0.94 | 14.1 s |
| auto/deepseek-v4.1-flash-free (Mimir-Memory key, n=16) | 16/16 | 0.94 | 13.5 s |
| group/glm-5.3-flash (Mimir-Memory key, n=16) | 16/16 | 0.88 | 13.9 s |
| our-free-model/* (shared key, n=10) | 0/10 | — | 429 rate-limited |
| 10Router cbcn/deepseek-v4.1-flash (current prod, n=10) | 0/10 | — | 401 |

Prompt ≈ 750 tokens (= the governance prompt itself; no MCP/skill injection).
Max latency 28–35 s → 45 s timeout leaves headroom.

Small sample; agreement is measured against past decisions, not ground truth.
Re-check with a daily sample after switching.
