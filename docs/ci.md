# CI 与本机验证

`.github/workflows/ci.yml` 在所有 PR 和推送到 main 时运行；不部署、不合并、不使用密钥或自托管运行机。32 个 pytest 分组加一个浏览器 Windows job，按账户并发额度调度，测试 job 限时 25 分钟，缓存 pip 和 npm 下载。目标是每组 pytest 在 8 分钟内完成，为测试增长和运行机波动留余量；25 分钟是故障上限，不是目标时长。

## 云端检查

- **Python tests (1/32) 至 (32/32)**：Python 3.12，建立应用启动器要求的 `.venv` 并安装 `.[dev]`。第一组先运行 `python -m pytest -m powershell -n 0 -ra --junitxml=.runtime/ci/powershell.xml`，统一串行所有直接调用 PowerShell/脚本替身的模块，保留原断言和 40/60 秒子进程限时。随后各组 `python -m pytest -p tests.ci_shard -m "not powershell" -n 2 -ra --junitxml=...` 先按标记反选，再分片其余用例；每台运行机使用2个worker，减少4个worker下已观察到的短时限/HTTP争用，不延长请求超时。参数化大文件也会分散，包含 `tests/test_agent_rules.py`。32 份分组 XML 加 powershell.xml 的并集为完整套件，交付时核对无缺失、无重复。其他组用例显示为 deselected，不算跳过。分组索引和总数取 `strategy`；未加载分片插件或无分组环境变量时，本机收集不变。第一组另跑 `python -m mypy src`。
- **Python checks**：固定名称的汇总检查，`always()` 等待全部 Python 分组；任何分组或 mypy 失败、取消或跳过都会失败。分组数变化不需要改这个必需检查名。
- **Browser contracts**：Node 24，`npm ci`、`npx playwright install chromium`，然后 `node scripts/run-browser-tests.mjs --output=.runtime/ci/browser`。运行公开 Git 文件中的全部 `tests/*.cjs`；Python 服务使用临时合成工作区和临时端口，其他界面契约使用模拟响应。它们不能代替真实 OCR、Excel 或 Android 真机验收。
- pytest XML、浏览器逐项日志和结果 JSON 保留为 Actions artifacts 七天；失败时也上传。没有真实发票、私有模板或台账。

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
