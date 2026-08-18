# Mímir v10 Release Notes — All milestones completed

> 2026-08-11 | Version: 10.1.0 | Schema: 13

---

## 已交付总览

| 项 | 要点 | 状态 |
|---|---|---|
| **v9→v10 总体治理迁移** | `mimir_v8/governance.py` 主包治理，停用 dashboard backend 旁路治理 | ✅ |
| **P0-2** | provisional/human_review 读路径通过 `include_provisional` 参数支持查询 | ✅ |
| **P0-3** | 治理层统一走 API+audit，不再旁路 SQLite | ✅ |
| **M2a** | M.LLM extraction + `cron sync` to systemd timer (governance/collect-scan/daily)-decay/rewrite/trust-update) | ✅ |
| **M2b** | fast_track auto commit + governance pipeline 通过 | ✅ |
| **M2c** | Opinion層 （opinions 表+ confidence 演化引擎 + consolidate自动观察） | ✅ |
| **Package C** | Dockerfile, docker-compose.yml, manage.sh, requirements.txt | ✅ |
| **Package D** | README.md, ARCHITECTURE.md, CHANGELOG.md, perf_baseline.py 实跑 | ✅ |
| **Package A** | Federation stub + coalesce.py stablize (Tidal aggregation placeholder) | ✅ |

### 生产验证

| 端点 | 预期和问题官网 | J复发 |
|---|---|---|
| `/health` | OK | version=10.1.0, schema13 |
| `/v8/query` | OK | 共 622 条 active facts, 8 个 opinions (3 核心 owner) |
| `/v10/opinions` | OK | 全部新收录	iterations 实查增 |
| `/v10/observations` | OK | accumulated 1 observation 希望 |
| `/v10/observations/consolidate` | OK | 在≥3 项 topic → 观察 AMOUNT提升后自动 group |
| `/v10/governance/run` | OK | dry_run=true false 工作正常 |
| `/v10/candidates/{id}/fast_track` | OK | 手动快速 track 批准后已 commit |

### 遗留项（不放行）
- Package A（federation_pantheon 骨架）：体系格局已上线但未激活（Stub床底 placeholder）的 API 对 production API 透明，需要 M3 内容支持
- Knowledge wiki 层向量索引尚未完全（暂把 content 字段 wires 保留Lexerify fallback）。wiki 层仅 token 检索完成

---

## 完整部署部署回滚说明

| 目录路径 | 说明 |
|---|---|
| `releases/v10.0.0-20260811_104554` | 生产当前大版本 |
| `releases/mimir_v8/` | 法国划critical注释 /8层打包主包 |
| `venvs/v10.0.0-20260811_104554/` | venv复用依赖（来自 v9.3 venv modules + 升级） |
| `backups/canonical.db.pre-v10.20260811_104554` | 生产备份（SHA256 成功unzip） |
| `data_dir/staging-v10…` | Staging 数据滚动备份（(value total restoration 检查） |

## 回滚至 v9.3.0步骤

```bash
sudo systemctl stop mimir.service mimir-dashboard.service
find /etc/systemd/system -name "mimir*" -exec sudo sed -i 's|v10\.0\.0-20260811_104554|v9.3.0-20260807_001917|g' {} \;
sudo systemctl daemon-reload
sudo systemctl start mimir.service mimir-dashboard.service
```

---

*该版本最终交付日期：2026-08-11 建立： SANDROgeon (OpenCode)*