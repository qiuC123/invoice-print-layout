# CI 与本机验证

`.github/workflows/ci.yml` 在所有 PR 和推送到 main 时运行，也支持手动触发；不部署、不合并、不使用密钥或自托管运行机。main 推送按提交 SHA 分组，每个合并提交都跑完，不再因后续提交自动取消；PR 和手动触发仍按 ref 分组，新运行取消同组旧运行。32 个 pytest 分组加一个浏览器 Windows job，按账户并发额度调度，测试 job 限时 25 分钟，缓存 pip 和 npm 下载。目标是每组 pytest 在 8 分钟内完成，为测试增长和运行机波动留余量；25 分钟是故障上限，不是目标时长。

## 云端检查

- **Python tests (1/32) 至 (32/32)**：Python 3.12，建立应用启动器要求的 `.venv` 并安装 `.[dev]`。第一组先运行 `python -m pytest -m powershell -n 0 -ra --junitxml=.runtime/ci/powershell.xml`，统一串行所有直接调用 PowerShell/脚本替身的模块，保留原断言和 40/60 秒子进程限时。随后各组 `python -m pytest -p tests.ci_shard -m "not powershell" -n 2 -ra --junitxml=...` 先按标记反选，再分片其余用例；每台运行机使用2个worker，减少4个worker下已观察到的短时限/HTTP争用，不延长请求超时。参数化大文件也会分散，包含 `tests/test_agent_rules.py`。32 份分组 XML 加 powershell.xml 的并集为完整套件，交付时核对无缺失、无重复。其他组用例显示为 deselected，不算跳过。分组索引和总数取 `strategy`；未加载分片插件或无分组环境变量时，本机收集不变。第一组另跑 `python -m mypy src`。
- **Python checks**：固定名称的汇总检查，`!cancelled()` 在工作流未取消时等待全部 Python 分组，仍严格要求 `needs.python.result == 'success'`；任何分组或 mypy 未通过都会失败。取消工作流时不再通过 `always()` 强制启动汇总并抛错。取消后的实际 job conclusion 必须按下文在 Actions 实测，不能把 `skipped` 当成取消验收通过。分组数变化不需要改这个必需检查名。
- **Browser contracts**：Node 24，`npm ci`、`npx playwright install chromium`，然后 `node scripts/run-browser-tests.mjs --output=.runtime/ci/browser`。运行公开 Git 文件中的全部 `tests/*.cjs`；Python 服务使用临时合成工作区和临时端口，其他界面契约使用模拟响应。它们不能代替真实 OCR、Excel 或 Android 真机验收。
- pytest XML、浏览器逐项日志和结果 JSON 保留为 Actions artifacts 七天；失败时也上传。没有真实发票、私有模板或台账。

## 2026-11-16 体检：汇总失败的三类原因

体检记录中的 11 次 Python checks 失败不都属于 flaky：

- 4 次是运行被取消后，旧 `always()` 汇总仍运行，将 `needs.python.result == 'cancelled'` 报为失败。本次调整汇总条件及 main 并发分组；不放宽成功条件。
- 4 次是 PR 上真实回归，汇总正确拦截：类别打印的期望未同步（2 次）、Browser contracts 失败（1 次）、手机连接二维码断言（1 次，已由 `9bbbd9b` 修复）。这些已在合并前修复，不在本次重复修改。
- 其余 3 次涉及 PowerShell 子进程超时、mail kick 的 0.5 秒时序断言和 report review HTTP 超时，均在 #4 合并前。`3203145` 将 PowerShell 测试串行化，`e989a44` 降低 worker 数并将 mail 测试改为事件顺序断言。体检第 3 项处理的是同类历史偶发失败；本次不重复修改这些 Python 测试、分片、超时或重试。

本轮只读复核：[run 42461640628](https://github.com/qiuC123/invoice-print-layout/actions/runs/42461640628) 整体为 `cancelled`，Python checks 为 `failure`，失败日志明确执行了 `if ('cancelled' -ne 'success')`。基线 main 提交 `25bc167` 的 [run 42764829214](https://github.com/qiuC123/invoice-print-layout/actions/runs/42764829214) 两个必需检查均成功；这是改动前的结果，不代表本补丁已通过 Actions。

初次提交的本地验证：YAML 解析及 mypy（123 个源文件）通过；两阶段 pytest 覆盖 5,400 项且无重复，PowerShell 为 75 passed，其余为 5,238 passed、81 failed、6 skipped。失败集中在邮件原件和人员交接的长路径写入（已确认一个失败路径为 283 字符）；改用扩展长度临时路径复验这 81 项时，80 passed，另 1 项邮件来源校验仍失败。因此本地完整 pytest 未通过，不将路径实验记为全绿。

随后提交 `d6eb2fc` 的 [run 43802972739](https://github.com/qiuC123/invoice-print-layout/actions/runs/43802972739) 中，Python checks 实际为 `success`；Browser contracts 为 83 passed、1 failed。唯一失败是 `organize_nonblocking_browser.cjs` 等待 Python fixture 的 30 秒启动超时，随后因启动阶段位于清理区外，子进程一直存活到外层 180 秒超时。返工将 fixture 等待、浏览器启动及操作统一纳入清理区；浏览器关闭失败也会关闭自己的 fixture，并增加启动阶段日志。新增 `organize_fixture_lifecycle.cjs` 覆盖启动超时、浏览器启动失败和关闭失败的清理。本机已复现旧版泄漏并验证修复；云端最初启动超时的具体阶段尚未复现，需结合新日志继续核对。不增加超时、不重试、不放宽原断言。

本次返工的完整浏览器套件为 85 passed、0 failed（276.64 秒），其中原失败合约为 8.01 秒；mypy 再次通过 123 个源文件。未重复运行未改动的完整 pytest，沿用上述本地失败记录及 `d6eb2fc` 的云端 Python 全绿结果；最新返工提交的 Actions 尚待控制器推送后验证。

待验收方在本分支推送后执行真实 Actions 验证（本地检查不能替代）：

1. 确认 PR 正常 CI 的 **Python checks** 和 **Browser contracts** 均为 `success`，记录 run 编号。
2. 用 `gh workflow run ci.yml --ref <分支>` 触发一次，等 Python 分片开始运行后执行 `gh run cancel <run编号>`，再用 `gh run view <run编号> --json jobs` 核对 **Python checks** 的 conclusion。预期为 `cancelled`；`failure`、`success`、`skipped` 均不能算本方案验收通过。
3. 若实测为 `skipped`，须调整取消处理使其明确为 `cancelled` 或 `failure`，保留 main 按 SHA 分组及严格成功条件，重新实测并将 run 编号、结果和修法写入 PR。未完成此项前，不宣称取消语义已验证。

## 2026-11-23 体检：Python checks 失败来源

以 UTC `2026-11-17T09:03:01Z` 为门禁完成时间上限，用 `gh run list --repo qiuC123/invoice-print-layout --workflow CI --limit 60 --created '<=2026-11-17T09:03:01Z'` 取 run 和 attempt，再用 `gh api --paginate --slurp repos/qiuC123/invoice-print-layout/actions/runs/<run>/attempts/<attempt>/jobs` 逐次读取固定名称 `Python checks` 的 conclusion。按门禁完成时间倒序，最近 21 个 attempt 有 **6 次失败、14 次通过、1 次取消**；每个 workflow run 只取最后一次 attempt，最近 21 个 run 有 **4 次失败、16 次通过、1 次取消**。两种口径均未复现体检的 13/21。扩展至全部 32 分片记录（#10–#49，各 attempt 单独计）有 **13 次失败 / 44 次**。带 ★ 的 6 次属于最近 21 个 attempt。

| 来源 | 最新 21 次 / 32 分片历史 | 运行、事件、分支和提交 | 失败的测试或取消情况 |
| --- | ---: | --- | --- |
| 16/32，已修的测试回归 | 1 / 1 | ★ [#49](https://github.com/qiuC123/invoice-print-layout/actions/runs/44050556932/job/133926998904) PR `agent/event-optin` `9b56ee2` | `test_runtime_identity_is_frozen_after_server_creation` 写入正被后台线程读取的配置时触发 WinError 32。`58fb40b` 将配置状态移到独立测试，随后 [#50](https://github.com/qiuC123/invoice-print-layout/actions/runs/44052506455) 通过。 |
| 30/32，子进程超时，根因未确认 | 1 / 1 | ★ [#48](https://github.com/qiuC123/invoice-print-layout/actions/runs/44048871635/job/133921334660) PR `agent/event-optin` `d7cc703` | `test_browser_fixture_isolates_native_interfaces` 子进程超过 30 秒；后续 `9b56ee2` 删除该用例，缺少同 SHA 通过记录。 |
| 18/32，已修的夹具回归 | 1 / 1 | ★ [#37·1](https://github.com/qiuC123/invoice-print-layout/actions/runs/42759027666/job/129640080727) PR `agent/payment-screenshot` `4794914` | `test_cli_routes_refresh_and_opt_in_recovery` 的导入替身不接受 `package=`；修复提交 `5a7e157` 的 [#38](https://github.com/qiuC123/invoice-print-layout/actions/runs/42760462779) 通过。 |
| 30/32，已确认偶发 | 1 / 1 | ★ [#32·1](https://github.com/qiuC123/invoice-print-layout/actions/runs/42459739984/attempts/1) PR `agent/organize-safety-net` `773d8ab` | `test_answer_while_previous_spawn_worker_exits_continues_same_job[True]` 等待超时，同 SHA [第二次通过](https://github.com/qiuC123/invoice-print-layout/actions/runs/42459739984/attempts/2)；`026870b` 已调整同步方式。 |
| 5、6、7、8、11、17、24、25、26、27、28、29/32，已修的类别回归 | 0 / 2，每个所列分片各 2 次 | [#22](https://github.com/qiuC123/invoice-print-layout/actions/runs/42359218448) 手动、[#23](https://github.com/qiuC123/invoice-print-layout/actions/runs/42359525739) PR，均为 `agent/category-print-express` `2770ec0` | 5–8 组为 `test_saved_queue_evidence_suggests_printing_without_changing_fee_or_rereading_original`，11 组为 `test_review_fields_http_state_auth_stale_and_classification_assets`，17 组为 `test_complete_native_trip_or_city_waybill_projects_actual_role_locally`，24–29 组为 `test_create_from_one_material_http_required_choice_preview_and_undo`，28 组另有 `test_numbered_product_names_are_not_price_metadata`。`2d5cb87` 的 [#24](https://github.com/qiuC123/invoice-print-layout/actions/runs/42361208580) 和 [#25](https://github.com/qiuC123/invoice-print-layout/actions/runs/42361211521) 通过。 |
| 23/32，历史时序失败 | 0 / 1 | [#17](https://github.com/qiuC123/invoice-print-layout/actions/runs/41448992897/job/125289184877) PR `codex/ci-launcher-serial` `fb575ce` | `test_async_kick_never_blocks_http_and_close_does_not_half_commit_a_message` 的耗时断言和 `test_http_formal_report_includes_the_same_table_as_final_preview` 的 HTTP 超时同次失败。`e989a44` 改用事件顺序断言并降低 worker 数，随后 [#18](https://github.com/qiuC123/invoice-print-layout/actions/runs/41451745057) 通过，HTTP 具体阻塞点未确认。 |
| 28/32、30/32，历史启动器超时 | 0 / 2，各 1 次 | [#13·1](https://github.com/qiuC123/invoice-print-layout/actions/runs/41441450361/attempts/1) PR `codex/auto-reimburse` `21284c9`；[#14](https://github.com/qiuC123/invoice-print-layout/actions/runs/41444151908/job/125272991568) push `main` `0181aec` | `test_launcher_mode_switch_and_identity_guards_without_touching_a_live_service` 两个参数分别超时；[#13·2](https://github.com/qiuC123/invoice-print-layout/actions/runs/41441450361/attempts/2) 同 SHA 通过。`3203145` 已将这组测试标记为 PowerShell 串行。 |
| 取消导致旧门禁报失败 | 2 / 4 | ★ [#37·2](https://github.com/qiuC123/invoice-print-layout/actions/runs/42759027666/attempts/2) PR `agent/payment-screenshot` `4794914`；★ [#35](https://github.com/qiuC123/invoice-print-layout/actions/runs/42461640628) 和 [#26](https://github.com/qiuC123/invoice-print-layout/actions/runs/42387054526) push `main` `88b2db6`、`59d0930`；[#16](https://github.com/qiuC123/invoice-print-layout/actions/runs/41447777628) 手动 `codex/ci-launcher-serial` `3203145` | 门禁日志的输入均为 `cancelled`。对应 1、32、32、9 个分片取消；后三次的 Browser contracts 同时取消。 |

失败测试名取自 `gh run view --log-failed --job <job-id>`。表中测试失败的 job 都在 `Full pytest (includes agent rules)` 步骤报错，且同次没有取消的并行 job。`Strict type check` 和 `Serial PowerShell contracts before resource-heavy tests` 失败均为 0 次。30/32 在最近 21 次中有两条不同原因的记录，其中已确认偶发的续跑等待已有修复；扩展窗口内重复最多的偶发用例是启动器测试，也已有串行化修复。此次只改门禁诊断。

门禁失败时，现在会在检查注解和运行摘要中逐行列出分片、结论、失败步骤和 job 链接。若查询失败，日志保留运行链接与原始门禁结果。本地用 [#49](https://github.com/qiuC123/invoice-print-layout/actions/runs/44050556932) 的实际 Jobs API 响应运行 workflow 中的 PowerShell，得到 `Python tests (16/32) [failure] Full pytest (includes agent rules)` 及该 job 链接。构造 1/32 的 mypy 失败时，输出 `Python tests (1/32) [failure] Strict type check`。两次均写入摘要并以 1 退出，API 失败时也以 1 退出；正常成功路径以 0 退出且不访问 API。

控制器推送后，可在临时验证提交中让 `Full pytest (includes agent rules)` 仅在 `INVOICE_PYTEST_SHARD_INDEX` 等于 `15` 时抛出诊断错误，检查 **Python checks** 日志、注解和 Summary 均列出 `Python tests (16/32)` 和 job 链接。记录该 run，再撤掉故障提交，确认最终 PR 的 **Python checks** 和 **Browser contracts** 均通过。

## PowerShell 测试的唯一分类来源

直接调用宿主或脚本替身的模块设置 `pytestmark = pytest.mark.powershell`，不维护第二份 CI 文件清单。当前为 `test_assistant_startup.py`（含 VBS 调用 PS 替身）、`test_workbench_launcher.py`、`test_invoice_assistant.py`、`test_construction_report_execution.py`、`test_construction_report_native.py`。整个模块串行，避免同文件里的辅助调用被漏掉。

`tests/powershell_contracts.py` 随 conftest 自动加载，在 `-m` 筛选前检查实际宿主解析和字面量子进程调用，漏标就收集失败；已标记测试进入 xdist worker 也会失败，提示使用 `-m powershell -n 0`。检查不把静态审计字符串或 Git 命令当成宿主执行，回归覆盖此前漏标的 invoice_assistant 模块。动态构造宿主时须扩展识别，不能只改文档。

隔离针对已观察到的宿主/子 Python 在重测试并行负载下超过 40 秒的现象。原 CI 日志没有分段计时，不能精确归因到某个系统调用；没有延长超时或加入测试重试。私有 Excel/COM 集成的原有跳过范围不变。

## 合并前的兼容试跑

可在已有 CI 分支手动运行同一工作流，输入 `validation-ref`（完整 40 位提交 SHA）。运行机将该提交检出，再将本 CI 分支相对 main 的补丁应用到临时索引；不创建合并提交、不推送、不修改目标分支。普通 PR/main 运行不输入此值，照常验证本次提交。

```powershell
gh workflow run ci.yml --ref <CI分支> -f validation-ref=<待验证提交SHA>
```

每组和浏览器 artifact 的 `source.json` 记录工作流提交、待验证 SHA 和实际测试树哈希，防止把 CI 分支的普通绿灯误称为功能分支已验证。补丁冲突或无效 SHA 直接失败。

依赖缓存配置依据 [setup-python](https://github.com/actions/setup-python#caching-packages-dependencies)；浏览器安装依据 [Playwright CI 文档](https://playwright.dev/docs/ci)。不缓存浏览器二进制，避免缓存恢复与重新下载收益不确定；固定 Playwright 版本由 `package-lock.json` 管理。

## 明确跳过与覆盖边界

| 测试 / 路径 | 原因及本机补验 |
| --- | --- |
| `test_agent_rules.py::test_private_task_size` | TASK.md 被 Git 忽略；公开克隆缺文件时跳过，合成边界测试照跑。本机有文件时执行 20,000 字节限制。 |
| `test_construction_report_native.py::test_isolated_native_retry_still_rejects_changed_history` | hosted CI 无 PowerPoint COM，`CI=true` 明确跳过；本机 Windows 有 Office 时运行。 |
| `test_missing_material_confirmation.py::test_real_excel_retains_missing_proof_text_and_readable_row_height` | 私有公司模板及原生 Excel 环境不在 CI；未配置 `INVOICE_TEST_REPORT_TEMPLATE` 时明确跳过。 |
| `test_person_allocation_export.py::test_company_main_sheet_identical_with_person_detail_column` | 同上，检查真实模板填写。 |
| `test_reimbursement.py::test_company_excel_integration` | 同上，检查真实报销表。 |
| `test_report_print_order.py::test_excel_detail_rows_and_summary_formulas_follow_template_order` | 同上，检查 Excel 明细和公式。 |
| `test_report_print_preview.py::test_native_company_form_is_one_vector_a4_with_chinese_and_matching_total` | 私有模板及 Excel COM 不在 CI，缺配置明确跳过。 |
| `test_report_types.py::test_company_template_all_fixed_and_dynamic_excel_rows` | 私有模板及本机 `@oai/artifact-tool` 不在 CI，缺配置明确跳过。 |
| `.gitignore` 中的私有测试和 `phone_web_browser.cjs`、`agent_details_browser.cjs` | 不属于公开 Git 文件；不向 CI 复制真实数据。未收集不计为 pytest 的 skip。 |
| 验证技能的真实整批预览/制包 | 需要原生 Excel COM 和本机 `@oai/artifact-tool`，不在云端运行；本机用合成模板按技能实测并留证。 |

其余公开测试使用合成数据、mock 或自己启动的临时本机服务，不因依赖 Windows 子进程、OCR 或浏览器而整类排除。`tests/conftest.py` 默认将网卡枚举替换为空合成网络，防止每次构造 HTTP 服务都启动真实 PowerShell 网卡探针；地址/缓存专项测试仍覆盖此默认值，注入各自的接口和延迟。产品网卡发现逻辑不变，真实网卡和手机连接仍需本机专项验收，不能据合成测试声称通过。CI 不读取邮箱、模型凭据、真实台账或 8765 服务。

## 本机复现

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
.venv/Scripts/python.exe -m pytest -m powershell -n 0 -ra --junitxml=.runtime/verify/powershell.xml
.venv/Scripts/python.exe -m pytest -m "not powershell" -n 4 -ra --junitxml=.runtime/verify/pytest.xml
.venv/Scripts/python.exe -m mypy src
npm ci
npx playwright install chromium
node scripts/run-browser-tests.mjs --output=.runtime/verify/browser
node .claude/skills/verify-workbench/scripts/verify.cjs run <新运行名称>
```

技能的体检、操作、证据和清理以 [verify-workbench](../.claude/skills/verify-workbench/SKILL.md) 为准。技能测试的是临时实例，不附加正式服务。Git-only 基线与候选版本应在同一依赖、同一公开测试范围内比较；私有测试数量不能混入比较。

本机完整 pytest 可用上述两阶段（XML 并集为全量），或直接 `python -m pytest` 串行全量；不要对含 PowerShell 标记的完整套件直接使用 `-n 4`。

main 的必需检查应为固定的 **Python checks** 和 **Browser contracts**，并要求人工批准合并。旧的六个 `Python checks (n/6)` 需由维护者替换；本次不修改仓库设置。
