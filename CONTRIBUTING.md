# Contributing to Mímir

Thanks for considering contributing to Mímir! We welcome bug reports, feature
requests, documentation improvements, and code contributions.

Please take a moment to read this guide — it keeps the project maintainable and
makes collaboration smooth for everyone.

---

## Code of Conduct

By participating, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
Be respectful and constructive in all interactions.

---

## How to Contribute

### 1. Report Bugs

Open an issue using the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md).
A good bug report includes:
- Mímir version / schema version (`curl /health`)
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (from `~/logs/` or systemd journal)

### 2. Request Features

Open an issue using the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md).
Describe the problem you're solving, not just the feature you want.

### 3. Contribute Code (Pull Requests)

1. **Fork** the repo and create a branch: `git checkout -b feature/your-feature`
2. Make your changes with clear, focused commits
3. **Run the tests** before submitting: `pytest tests/ -q`
4. Ensure new functionality has test coverage
5. Open a PR against `master` using the [PR template](.github/PULL_REQUEST_TEMPLATE.md)

---

## Development Setup

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
pip install -e ".[embeddings]"   # includes local embedding model
pip install pytest pytest-asyncio
pytest tests/ -q                 # run the full test suite
```

---

## Project Conventions

- **Event sourcing**: never `UPDATE` or `DELETE` historical rows in
  `memory_events` / `fact_versions` — they are immutable by design. New states
  are written as new events.
- **Governance**: all candidate mutations must be auditable (write to
  `audit_log` / `memory_events`). Do not bypass the review pipeline.
- **Python**: 3.11+, type hints on public APIs, follow existing module style.
- **Tests**: each behavior change should add or update a test in `tests/`.

---

## Commit Message Style

Use conventional commits:

```
feat: add X
fix: correct Y
docs: clarify Z
test: add coverage for W
refactor: ...
```

---

## Questions?

Open a [discussion](https://github.com/sandro1123/mimir-memory/discussions) or
an issue. We'll get back to you.
