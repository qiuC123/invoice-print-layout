# Invoice Print Layout project rules

- Keep the tool offline by default. Do not upload tickets or add cloud AI, OCR, email, or printer-driver integration without a separately approved scope change.
- Never commit real invoices, trip sheets, generated print packages, workspace state, names, phone numbers, addresses, tax identifiers, or other user data.
- Preserve source PDFs. A layout may remove only confirmed trip-sheet decoration and whitespace; uncertain crop boundaries must fail closed into review.
- Keep PDF composition vector-based so text remains extractable and QR codes stay sharp.
- Add or update tests for every behavior change. Run the full pytest suite and mypy before delivery.
- Do not add release packaging until the user explicitly starts the publishing phase.
