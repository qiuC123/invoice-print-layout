# 测试计划（先于测试代码，2026-10-18）

- `test_core.py`约10项：本机地址/工作区身份、项目边界、缓存/人工分类保护、误用版本、dry-run、POST不确定结果不重发、仅403刷新、历史报表路径/格式/内容hash与目录独占。
- `test_full_e2e.py`约10项：安装命令从其他cwd运行；doctor/project/material/expense/matching；原生隔离服务的合成收件、原建议→未核对草稿及幂等重试；人工类别保护；报告原格式与hash；真实既有3费用/9材料复用、下载原PDF/XLSX/MD，原账本和GLM/Jev调用次数不增加。
- 不采集新微信，不跑真实新OCR/GLM/Jev，不核对或提交真实费用。现有真实服务只GET；写入使用自己的合成fixture服务，原件不改。
- Codex与Hermes实际调用安装命令读取已有样本并复用识别/分类/报表，记录原生工具调用证明。沿用既有订阅，不更改原配置。
- 按项目AGENTS跑原完整pytest与mypy，CLI自有测试另跑；原生产模块/实盘/样本数据前后快照核验。清理仅限本轮临时文件；失败与未验证范围如实记录。

实际结果运行后补充。

## 实际结果

- 本机可编辑安装，从维护目录强制PATH安装命令：23/23，18.72秒（13 core + 10 E2E）。不制作新的发行EXE/公开wheel或发布。
- 真实既有样本3费用/9材料：识别、人工分类、核对和提交状态保持；直接下载已有PDF/XLSX/清单并验证与原文件字节一致，重复调用无新下载/排版/提交。详细私有期望值、命令与文件哈希保存在outputs/acceptance。
- Hermes沿用现有隔离订阅profile：gpt-6.1-sol/high，实际4次terminal（模型补做了离线结果校验），均退出0并复用；GLM/Jev、识别/分类/报表生成请求均0。没有新增凭据副本或修改原模型配置。
- 原有136文件/3283测试全覆盖通过：第1/2组原通过，第3组修正一处等长PDF fixture假设后1093项通过；同文件单独29项通过，最终mypy检查82源文件通过。初始失败与修正原文保留。此基线回归不冒称覆盖并行原主控新增手机联通测试。
- CLI23项最后运行晚于并行5文件API更新；原业务/Android/手机联通继续归原主控，CLI不回退其改动。
- 清理临时fixture/日志被自动审批审核拒绝，仅返回blocked by policy；未绕过，手动说明在outputs/acceptance/临时文件清理说明.md。
- 未执行真实新GLM/Jev/OCR、真实新收件/草稿创建、核对、提交或发送。显式处理/防重用隔离原生服务与合成材料验证，不能称为真实云账号端到端验收。历史报表不是当前修订报表。

完整CLI pytest输出（私有产物路径脱敏，原输出保留）：

```text
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.7.0 -- <PROJECT>\agent-harness\.venv\Scripts\python.exe
rootdir: <PROJECT>
configfile: pyproject.toml
collecting ... collected 23 items

<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_loopback_root_only_and_no_redirect_configuration PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_configuration_bom_and_url_change_needs_identity PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_wrong_runtime_identity_stops_before_state PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_saved_recognition_is_reused_even_when_processing_missing_enabled PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_missing_recognition_is_explicit_and_dry_run_never_posts PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_manual_other_reviewed_submitted_and_cached_categories_protected PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_foreign_project_ids_fail_before_requests PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_unknown_amount_and_returned_materials_do_not_inflate_totals PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_post_500_is_uncertain_and_never_retried PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_only_pre_dispatch_403_refreshes_once PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_post_invalid_json_shape_is_uncertain PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_report_paths_and_single_writer_lock PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_core.py::test_cached_report_damage_does_not_trigger_generation PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_installed_help_and_doctor_use_original_service [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_real_project_and_material_results_reused_without_ai [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_real_human_categories_remain_even_with_classify_missing [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_real_existing_report_download_is_byte_identical_and_idempotent [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE

Existing original md: <private artifact path recorded in acceptance log>

Existing original pdf: <private artifact path recorded in acceptance log>

Existing original xlsx: <private artifact path recorded in acceptance log>
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_installed_repl_and_invalid_project_are_json [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_native_cross_project_and_missing_recognition_dry_run [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_native_receive_duplicate_does_not_recognize_or_move [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_native_suggestion_to_draft_replay_and_changed_request_conflict [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_native_explicit_classification_uses_current_auto_route [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED
<PROJECT>\agent-harness\cli_anything\invoice_assistant\tests\test_full_e2e.py::test_native_fresh_receive_then_explicit_local_prepare [_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
[_resolve_cli] <USER>\.local\bin\cli-anything-invoice-assistant.EXE
PASSED

============================= 23 passed in 18.72s =============================

```
