Task 1: complete — session-end hook ported (5 tests RED->GREEN: ingest+crystallize/replay/explicit/DLP/HTTP), commit on feat/1.8.0
Task 2: complete — session-start hook (3 tests GREEN: payload/visibility/scope), commit on feat/1.8.0
Task 3: complete — hooks integration tests (3 tests GREEN), all 11 hook tests passing
Task 4: complete — heat columns (recall_count/useful_count/last_recalled_at/last_spaced_at) additive + idempotent migration, 4 tests GREEN
Task 5: complete — heat-aware decay/ranking (capped boost, neutral when unused), 4 tests GREEN
Task 5 (feedback side): complete — search_feedback updates heat counters/spacing, 4 tests GREEN; heat ranking+schema+feedback all passing
Task 6: complete — retention fail-closed proof tests added (impl already fail-closed); commit 921fca0
Task 7: complete — A-tier hook adapter scaffolding (claude-code/codex paths, payload builder, no explicit mode), tests + ruff clean, commit 8c34ae8
Task 8: complete — docs/BENCHMARK-COMPARE.md added with hook/STM/adapter evidence and pending items
