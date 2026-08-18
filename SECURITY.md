# Security Policy · 安全政策

> English · 中文双语

## Supported Versions · 支持的版本

| Version 版本 | Schema | Supported 支持 |
|---------|--------|-----------|
| 12.0.x  | 18     | ✅        |
| < 12.0  | < 18   | ❌        |

## Reporting a Vulnerability · 报告漏洞

We take security seriously. **Do not open a public issue for security
vulnerabilities.**

我们认真对待安全。**请勿为安全漏洞公开提交 issue。**

To report a vulnerability, please email the maintainers directly at the contact
address listed on the repository, or open a private/security advisory via
GitHub's "Security" tab → "Report a vulnerability".

要报告漏洞，请直接发送邮件给仓库上列出的维护者联系方式，或通过 GitHub 的
"Security" 标签页 → "Report a vulnerability" 提交私密/安全通告。

Please include 请包含：

- Affected version(s) 受影响的版本
- A description of the vulnerability and its impact 漏洞描述及其影响
- Steps to reproduce (if available) 复现步骤（如有）
- Any suggested remediation 任何修复建议

You should receive an acknowledgment within 48 hours. We will keep you informed
of the investigation and remediation progress.

你应在 48 小时内收到确认。我们会持续向你通报调查与修复进展。

## Security Best Practices for Deployment · 部署安全最佳实践

- Mímir API binds to `127.0.0.1` by default. **Do not expose it directly to the
  public internet.** Use a reverse proxy (nginx / Cloudflare Tunnel) with
  authentication if remote access is needed.
  Mímir API 默认绑定 `127.0.0.1`。**请勿直接暴露到公网。** 如需远程访问，请使用
  带鉴权的反向代理（nginx / Cloudflare Tunnel）。
- Bearer tokens: store under `$MIMIR_HOME/secrets/` with `chmod 600`. Never
  commit tokens to the repository.
  Bearer token：存放于 `$MIMIR_HOME/secrets/` 并设置 `chmod 600`。切勿将 token
  提交到仓库。
- If a token is compromised, rotate it immediately by removing it from
  `secrets/clients/*.token` and issuing a new one.
  若 token 泄露，立即从 `secrets/clients/*.token` 移除并重新签发。
- Restricted facts: `egress_policy=local_only` prevents external processing.
  Validate this behavior with a security drill before relying on it in production.
  受限事实：`egress_policy=local_only` 阻止外部处理。在生产依赖之前，请先通过安全
  演练验证该行为。

## Disclosure Policy · 披露政策

We follow coordinated disclosure. Please allow us time to address the issue
before public disclosure.

我们遵循协调披露原则。请在我们处理问题后再公开披露。
