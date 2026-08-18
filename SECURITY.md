# Security Policy

## Supported Versions

| Version | Schema | Supported |
|---------|--------|-----------|
| 12.0.x  | 18     | ✅        |
| < 12.0  | < 18   | ❌        |

## Reporting a Vulnerability

We take security seriously. **Do not open a public issue for security
vulnerabilities.**

To report a vulnerability, please email the maintainers directly at the contact
address listed on the repository, or open a private/security advisory via
GitHub's "Security" tab → "Report a vulnerability".

Please include:

- Affected version(s)
- A description of the vulnerability and its impact
- Steps to reproduce (if available)
- Any suggested remediation

You should receive an acknowledgment within 48 hours. We will keep you informed
of the investigation and remediation progress.

## Security Best Practices for Deployment

- Mímir API binds to `127.0.0.1` by default. **Do not expose it directly to the
  public internet.** Use a reverse proxy (nginx / Cloudflare Tunnel) with
  authentication if remote access is needed.
- Bearer tokens: store under `$MIMIR_HOME/secrets/` with `chmod 600`. Never
  commit tokens to the repository.
- If a token is compromised, rotate it immediately by removing it from
  `secrets/clients/*.token` and issuing a new one.
- Restricted facts: `egress_policy=local_only` prevents external processing.
  Validate this behavior with a security drill before relying on it in production.

## Disclosure Policy

We follow coordinated disclosure. Please allow us time to address the issue
before public disclosure.
