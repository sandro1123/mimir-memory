# Contributing to Mímir · 贡献指南

Thanks for considering contributing to Mímir! We welcome bug reports, feature
requests, documentation improvements, and code contributions.

感谢你考虑为 Mímir 做出贡献！我们欢迎 bug 报告、功能请求、文档改进与代码贡献。

Please take a moment to read this guide — it keeps the project maintainable and
makes collaboration smooth for everyone.

请花一点时间阅读本指南——它让项目保持可维护，也让每个人的协作更顺畅。

> English · 中文双语

---

## Code of Conduct · 行为准则

By participating, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
Be respectful and constructive in all interactions.

参与即表示你同意遵守[行为准则](CODE_OF_CONDUCT.md)。请在一切互动中保持尊重与建设性。

---

## How to Contribute · 如何贡献

### 1. Report Bugs · 报告 Bug

Open an issue using the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md).
A good bug report includes:
- Mímir version / schema version (`curl /health`)
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (from `~/logs/` or systemd journal)

使用 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.md) 提交 issue。一份好的 bug 报告包含：
- Mímir 版本 / schema 版本（`curl /health`）
- 复现步骤
- 预期 vs 实际行为
- 相关日志（来自 `~/logs/` 或 systemd journal）

### 2. Request Features · 请求功能

Open an issue using the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md).
Describe the problem you're solving, not just the feature you want.

使用[功能请求模板](.github/ISSUE_TEMPLATE/feature_request.md) 提交 issue。
描述你要解决的问题，而不只是你想要的功能。

### 3. Contribute Code (Pull Requests) · 贡献代码（PR）

1. **Fork** the repo and create a branch: `git checkout -b feature/your-feature`
2. Make your changes with clear, focused commits
3. **Run the tests** before submitting: `pytest tests/ -q`
4. Ensure new functionality has test coverage
5. Open a PR against `master` using the [PR template](.github/PULL_REQUEST_TEMPLATE.md)

1. **Fork** 仓库并创建分支：`git checkout -b feature/your-feature`
2. 做出清晰、聚焦的提交
3. 提交前**运行测试**：`pytest tests/ -q`
4. 确保新功能有测试覆盖
5. 使用 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 向 `master` 提交 PR

---

## Development Setup · 开发环境

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
pip install -e ".[embeddings]"   # includes local embedding model 包含本地嵌入模型
pip install pytest pytest-asyncio
pytest tests/ -q                 # run the full test suite 运行完整测试
```

---

## Project Conventions · 项目约定

- **Event sourcing 事件溯源**: never `UPDATE` or `DELETE` historical rows in
  `memory_events` / `fact_versions` — they are immutable by design. New states
  are written as new events.
  永远不要 `UPDATE` 或 `DELETE` `memory_events` / `fact_versions` 中的历史行——
  它们在设计上是不可变的。新状态以新事件写入。
- **Governance 治理**: all candidate mutations must be auditable (write to
  `audit_log` / `memory_events`). Do not bypass the review pipeline.
  所有候选变更必须可审计（写入 `audit_log` / `memory_events`）。不要绕过审核管线。
- **Python**: 3.11+, type hints on public APIs, follow existing module style.
  3.11+，公共 API 加类型注解，遵循现有模块风格。
- **Tests 测试**: each behavior change should add or update a test in `tests/`.
  每个行为变更都应在 `tests/` 中新增或更新测试。

---

## Commit Message Style · 提交信息风格

Use conventional commits 使用约定式提交：

```
feat: add X
fix: correct Y
docs: clarify Z
test: add coverage for W
refactor: ...
```

---

## Questions? · 有问题？

Open a [discussion](https://github.com/sandro1123/mimir-memory/discussions) or
an issue. We'll get back to you.

在 [discussion](https://github.com/sandro1123/mimir-memory/discussions) 或 issue
中提问。我们会回复你。

You can also reach the maintainer directly at
**sandro1123@hotmail.com**.

你也可以直接联系维护者：**sandro1123@hotmail.com**。
