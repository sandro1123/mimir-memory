# 联邦信封用发送方密钥加密：任一 peer 可解密他投并冒名发信

> **不要公开提交。** 按 `SECURITY.md`：GitHub Security tab → Report a vulnerability。

**标签**：security, federation
**严重度**：High
**影响版本**：1.0.0 起全部版本（含当前 1.7.0）

## 问题

联邦协议把"节点的对称 Fernet 密钥"当成"公钥"交付给 peer，而信封是用**发送方自己的**
密钥加密、又被接收方用**发送方注册的那把密钥**解密。等于每个与 A 握手过的节点都持有
A 的加密密钥。

`mimir_v8/federation/service.py`：

- `export_events`（410–469）：`ciphertext = encrypt_envelope(payload, self._key)`，注释 455–458 明确写着 "Envelope is encrypted with the SENDER's key"。
- `ingest_envelope`（471–499）：`decrypt_envelope(ciphertext, row["public_key"])`，`row` 是 `federation_peers` 里对 `from_node` 的登记。
- 事件署名（518–524）：`known_signers` 是 `registered | seen | {from_node}` 的**名字集合**，没有任何密码学绑定。

## 影响

1. **无收件人隔离**：A 发给 B 的信封，只要 C 与 A 握过手，C 就能解开。
2. **可冒名发信**：C 用 A 的密钥加密 `{from_node: A, to_peer: B, events: ...}`，B 校验 `from_node` 与解密都通过；事件 `node_id` 填 A 也通过署名检查。
3. **可伪造历史**：配合 LWW 折叠，攻击者可对任意 `crdt_key` 写入更高 `lamport`，永久压过真实值。

这与 README「支持多节点 Fernet 加密联邦同步」给人的安全预期不符。

## 修复方向

1. 每节点生成 Ed25519 签名对 + X25519 交换密钥；`federation_peers` 存**公钥**而不是共享密钥。
2. 信封按收件人加密（或带 recipient 标记的密钥封装），收件人以外的节点无法解密。
3. 事件体对 `(crdt_key, lamport, node_id, op, value)` 签名，ingest 验签失败一律拒收。
4. 旧信封保留一个只读兼容窗口，窗口期写入 `docs/FEDERATION.md` 并在 2.0 移除。

## 验收（反向注入必红）

- 同等 peer 解密他投信封 → 失败。
- 篡改任一字段 / 伪造签名 / 无签名 → ingest 拒收。
- 正常双节点同步、离线增量重放、LWW 平局决胜全部保持绿。

## 关联

- `docs/FEDERATION.md` 需要同步改写。
- 2.0 范围裁决 G3（`docs/superpowers/specs/2026-11-16-mimir-2.0-scope-and-acceptance.md`）。
