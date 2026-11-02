# Invoice Print Layout project rules

- Keep the tool offline by default. Do not upload tickets or add cloud AI, OCR, email, or printer-driver integration without a separately approved scope change.
- Never commit real invoices, trip sheets, generated print packages, workspace state, names, phone numbers, addresses, tax identifiers, or other user data.
- Preserve source PDFs. A layout may remove only confirmed trip-sheet decoration and whitespace; uncertain crop boundaries must fail closed into review.
- Keep PDF composition vector-based so text remains extractable and QR codes stay sharp.
- Add or update tests for every behavior change. Run the full pytest suite and mypy before delivery.
- Do not add release packaging until the user explicitly starts the publishing phase.
- Current task and recovery state live in TASK.md. Read it at start/resume and update it at milestones; keep one current owner.
- For construction daily reports, follow docs/construction-reports.md; for settlement photo drafts, follow docs/settlement-reports.md. Project/date facts and runtime paths live only in private workspace/construction-reports; never use a historical replay as today's report.

## Shared project memory

- At start or resume, read TASK.md and workspace/project-memory/入口.md. Follow the connection status recorded there; once enabled, call the read-only project-memory-invoice/read_project_rules tool and check project, revision, sources, and dated facts against current local state.
- Report connection failures or conflicting rules explicitly. Do not claim that a local mirror or old conversation is a live cloud read. The shared rules entry does not authorize automatic collection, sending, dispatch, or person identification.

## 验证与规则执行

验证本项目的功能：照 `.claude/skills/verify-workbench/SKILL.md` 做，功能地图在同目录 `features/`。CI 范围和本机补验见 `docs/ci.md`。

| 规则 | 由什么强制执行 |
| --- | --- |
| 私有 TASK.md 不超过 20,000 UTF-8 字节；归档已完成阶段和旧索引。 | `tests/test_agent_rules.py::test_private_task_size`；公开克隆缺文件时明确跳过。 |
| 保留用户 Office 实例；禁止按进程名终止 Office。 | `tests/test_agent_rules.py::test_tracked_scripts_preserve_user_office`，以及坏样本和历史脚本回归测试；所有权注释仍须代码审查。 |
| PDF 保留原件与矢量文字，裁剪不确定时进入人工复核。 | `tests/test_layout.py`、`tests/test_documents.py`；验证技能检查原件哈希和导出 PDF 文本。 |
| 行为变更需测试，交付前完整 pytest 和 mypy。 | `.github/workflows/ci.yml` 的 Python checks；是否覆盖了变更仍须审查。 |
| 真实界面使用合成数据验证，清理实例并保留证据。 | `verify-workbench` 的体检、断言和 `cleanup.json`；CI 的 Browser contracts 补充界面回归。 |
| 禁止提交真实业务数据、凭据和工作区状态。 | `.gitignore` 限制常见文件；推送前逐文件审查。没有自动检查能保证内容完全脱敏。 |
