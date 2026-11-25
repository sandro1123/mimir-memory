# 卡片改名后被误判为 `human_content_changed`，发布永久停摆

**标签**：bug, vault
**严重度**：Low（会静默卡死，不损坏数据）
**影响版本**：1.7.0

## 问题

`vault_card.render_card` 把标题写在托管区之外：

```python
f"# {title}",
```

而 `vault_publish.publish_entity`（81–86）用"非托管部分"整体比对来决定是否覆盖：

```python
human_changed = (
    target.exists()
    and human_content_hash(target.read_text(encoding="utf-8")) != human_content_hash(text)
)
if human_changed:
    return {"status": "skipped", "reason": "human_content_changed", ...}
```

于是**实体改名**（机器侧合法的状态变化）会让两个哈希必然不同，被当成"人类改过卡片"，
从而永久跳过发布——而且原因字符串把责任归给了人类，运维很难看出是改名导致的。

同一逻辑还有一个更隐蔽的停摆条件：如果人类删掉了某个 `<!-- mimir:managed:end ... -->`
标记，`human_content_hash` 的正则匹配不到完整块，会把机器正文算进"人类内容"，
之后每次发布都返回 `human_content_changed`，同样没有任何告警。

## 修复方向（任选其一，写进规格）

1. 把 `# {title}` 也纳入托管段（`sections["title"]`），让非托管区只剩真正的人类文字。
2. 或把 `title` 写进 frontmatter（已有 `title:` 字段）而正文标题由托管段渲染。
3. 无论哪种：当 `human_content_hash` 发现"托管标记不成对"时，返回**专门的**跳过原因（如 `managed_marker_malformed`），并在 `mimir ops health` 里计数，避免静默。

## 验收

- 实体改名后发布仍成功，卡片标题更新，人类加在卡片底部的一段话保持不变。
- 人为删掉一个 end 标记 → 返回 `managed_marker_malformed`（而非 `human_content_changed`），且健康检查能报出。
- `tests/test_vault_publish.py` 增两条对应用例。
