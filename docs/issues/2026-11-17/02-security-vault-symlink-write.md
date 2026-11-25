# Vault 发布未拒绝符号链接，卡片写入可落到库外

> **状态（2026-11-25）**：已修复，见 #69（1.9.2）。


> **不要公开提交。** 按 `SECURITY.md` 走 GitHub Security Advisory。

**标签**：security, vault
**严重度**：High（需要能改动 Vault 文件系统或同步通道）
**影响版本**：1.7.0

## 问题

`mimir_v8/vault_publish.py` 的写入路径（69–89）：

```python
rel_path = f"{_CARD_ROOT}/{card_filename(entity_id, ...)}"
target = vault_root / rel_path
snapshot = _snapshot_before_write(vault_root, target)
...
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(rendered, encoding="utf-8", newline="\n")
```

没有任何 `resolve()` 或 symlink 判断。`publication._validate_path`（544–554）只校验
字符串形态（拒绝 `..`、限制允许根、限制 `.md`），**不校验文件系统实体**。

## 影响

若卡片目录或某个 `<entity_id>.md` 被替换为符号链接（共享 Vault 常经 Syncthing 等
多机同步，或由人类手工整理），机器发布就会写到库外目标，并且写前写后的 Git 快照都
落在错误路径上——"可一键回滚"的保护也随之失效。

## 修复方向

1. 写入前 `target.resolve()` 并断言仍在 `vault_root.resolve()` 之内。
2. 目标本身与其任意父级若为 symlink，一律拒绝。
3. 写临时文件 + `os.replace` 原子替换，避免跟随链接写入。
4. 同一套检查复用到 `publication.register_document` 的落盘路径。

## 验收（反向注入必红）

- 卡片路径为 symlink → 拒写、返回明确原因、库外文件字节不变。
- 卡片父目录为 symlink → 同样拒写。
- 正常运行路径不受影响，`tests/test_vault_publish.py` 全绿。

## 备注

`card_filename` 对 `mimir_id` 有 `[a-z0-9][a-z0-9_.-]{0,127}` 白名单，**不存在**
文件名穿越，本条只针对文件系统链接，不是路径字符串问题。
