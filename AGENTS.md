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
