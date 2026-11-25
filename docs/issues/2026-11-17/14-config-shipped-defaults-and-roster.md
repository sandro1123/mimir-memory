# 出厂默认值带着维护者的机器路径、已退役的主体和一个死版本号

> **状态（2026-11-25）**：已修复，见 #74（1.9.2，Vault 默认路径；其余出厂默认值仍待处理）。


**标签**：bug, config, onboarding
**严重度**：Medium
**影响版本**：当前全部版本

## 三个独立但同源的问题

### 1. 默认 Vault 路径写死维护者的机器

`mimir_v8/config.py:69`：

```python
vault_root=Path(vault_root) if vault_root else Path.home() / "obsidian-vault" / "Sandro's Vault",
```

同源硬编码还在 `mimir_v8/reporting.py:249`（`self.vault_path = Path.home() / "obsidian-vault" / "Sandro's Vault"`），
它决定了日报写到哪里，且这一处不经过任何配置入口。两处都要改。

任何人 `pip install` 后不设 `MIMIR_VAULT_ROOT`，拿到的都是一个**不存在且带维护者名字**的路径。
这是"版本遗迹"里最典型的一类：本机调试路径被留成了通用默认值。
`release_scan.py` 的七面扫描显然没有覆盖到这行。

### 2. `init.sh` 会把已退役的主体重新造出来

`scripts/init.sh:22`：

```bash
AGENTS=("heimdallr" "quantmaster" "jarvis" "mentor")
```

1.5.0 的判词是"退役 `mentor`、由 `heimdallr` 继承"，但新部署执行 init.sh 又会生成一个
独立的 `mentor` 主体与 token——正好把刚退役的割裂重新引入。

### 3. `init.sh` 写进配置的版本号是一具尸体

`scripts/init.sh:99` 生成 `mimir_config.yaml` 时写入 `version: 14.1.0`。
`config.load_federation_registry` 只读 `federation` 段，**这个字段没有任何消费者**；
用户会以为它在描述自己的版本，而实际版本是 `MIMIR_VERSION`（1.7.0）。

## 附带：环境变量别名只覆盖了一半

`config.py:30–33` 对 `data_dir` 同时接受 `MIMIR_DATA_DIR` 与 `MIMIR_V8_DATA_DIR`，
但 `config_file` / `secrets_dir` / `log_dir` / `vault_root` **只有新名字**，
而 `server.py`、Dockerfile、QUICKSTART、FEDERATION.md 仍在教 `MIMIR_V8_*`。
所以"按文档配了变量、服务却读不到"是可能发生的。

## 修复

1. 默认值改为相对路径（`./var/mimir/vault`）或在未配置时**明确报错**；生产模式已有 fail-closed，非生产也不该悄悄指向别人的家目录。
2. `init.sh` 的默认主体去掉 `mentor`，并在注释里写明"如果你的库里还有 mentor 事实，由别名梯继承"。
3. 删掉配置里的 `version` 字段，或写成 `MIMIR_VERSION` 的注入值。
4. 四个路径变量补上 `MIMIR_V8_*` 别名，并在启动日志打一次性 deprecation 警告。
5. 给 `scripts/release_scan_rules.txt` 加一条：源码里不得出现绝对家目录路径与个人姓名形式的默认值。

## 验收

- 干净容器里不设任何 `MIMIR_*` 变量启动 → 要么用 `./var/...` 正常起，要么报出明确缺项，不出现 `Sandro's Vault`。
- 新部署的 `api_tokens.json` 里没有 `mentor`。
- 只设 `MIMIR_V8_DATA_DIR` 与只设 `MIMIR_DATA_DIR` 两种方式行为一致（同一测试两跑）。
