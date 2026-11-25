# 发版流程（1.9.2 起固定）

四步都完成才算发版完成，少一步都不算。1.9.1 漏了第 2 步，GitHub 上的 Latest 因此在一段时间里停在 1.9.0，和生产版本对不上。

1. **Tag**：PR 合并且 CI 全绿后，在 master 上打 `vX.Y.Z` 的注释 tag 并推送。tag 打好后不再移动；需要热修时另打 `vX.Y.Z-hotfix.N`。
2. **GitHub Release**：`gh release create vX.Y.Z --verify-tag --latest`。说明用 CHANGELOG 中本版那一节，再补「安装用哪个 tag」和「已知问题」。
3. **部署**：
   - 用 `git archive <tag>` 导出到 `releases/<ver>-<ts>`。
   - **同名建 venv**：`venvs/<ver>-<ts>`，与 release 目录一一对应。复制上一版 venv，把 `bin/activate*` 和各脚本 shebang 里的旧目录名改成新名，再 `bin/python3 -m pip install --no-deps --no-build-isolation -e releases/<ver>-<ts>`。
   - 切换前在 `/tmp` 下、**不设 `PYTHONPATH`** 验证：`env -u PYTHONPATH venvs/<ver>-<ts>/bin/python3 -c 'import mimir_v8'` 成功，且 `mimir_v8.schema.MIMIR_VERSION` 等于本次版本。
   - `releases/current` 与 `venvs/current` **一起**原子切换（`ln -sfn <name> current.tmp && mv -Tf current.tmp current`），然后 `daemon-reload`。只切一个会让两边版本错开：1.9.0 hotfix 到 1.9.2 期间 `venvs/current` 一直停在 1.9.0，靠单元里的 `PYTHONPATH` 才没出事，手动运行 worker 则直接 `ModuleNotFoundError`。
   - 回滚时两个 `current` 也一起指回上一版。
   - 回滚备份放在 `~/.hermes/mimir/backups/<ver>-deploy/`，**不要放 `/tmp`**。
   - 重启 `mimir.service`，确认 `/health` 返回的版本号就是本次发布的版本。
   - 手动跑一轮治理和抽取，确认都以 exit 0 结束。
4. **复查**：部署约一小时后单次复查，检查 health、治理的 `llm_failures`、抽取的 `llm_unavailable`/`failed`，以及服务日志里有无错误。
   - 复查必须用**持久的定时任务**（`CronCreate durable: true` 或 systemd timer）。只存在于会话里的定时任务会随会话结束而丢失，1.9.1 的复查就是这样丢的。

部署会改动的仓库外配置（drop-in、settings、token scope）写进 CHANGELOG 本版的「运维侧」小节。
