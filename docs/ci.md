# CI 与本机验证

`.github/workflows/ci.yml` 在所有 PR 和推送到 main 时运行；不部署、不合并、不使用密钥或自托管运行机。两个 Windows job 并行，分别限时 15 分钟，缓存 pip 和 npm 下载。

## 云端检查

- **Python checks**：Python 3.12，安装 `.[dev]`；`python -m pytest -n 4 -ra --junitxml=.runtime/ci/pytest.xml`，包含 `tests/test_agent_rules.py`；`python -m mypy src`。
- **Browser contracts**：Node 24，`npm ci`、`npx playwright install chromium`，然后 `node scripts/run-browser-tests.mjs --output=.runtime/ci/browser`。运行公开 Git 文件中的全部 `tests/*.cjs`；Python 服务使用临时合成工作区和临时端口，其他界面契约使用模拟响应。它们不能代替真实 OCR、Excel 或 Android 真机验收。
- pytest XML、浏览器逐项日志和结果 JSON 保留为 Actions artifacts 七天；失败时也上传。没有真实发票、私有模板或台账。

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

其余公开测试使用合成数据、mock 或自己启动的临时本机服务，不因依赖 Windows 子进程、OCR 或浏览器而整类排除。CI 不读取邮箱、模型凭据、真实台账或 8765 服务。

## 本机复现

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
.venv/Scripts/python.exe -m pytest -n 4 -ra --junitxml=.runtime/verify/pytest.xml
.venv/Scripts/python.exe -m mypy src
npm ci
npx playwright install chromium
node scripts/run-browser-tests.mjs --output=.runtime/verify/browser
node .claude/skills/verify-workbench/scripts/verify.cjs run <新运行名称>
```

技能的体检、操作、证据和清理以 [verify-workbench](../.claude/skills/verify-workbench/SKILL.md) 为准。技能测试的是临时实例，不附加正式服务。Git-only 基线与候选版本应在同一依赖、同一公开测试范围内比较；私有测试数量不能混入比较。

建议用户后续将 **Python checks**、**Browser contracts** 设为 main 的必需状态检查，并要求人工批准合并；本次不修改仓库设置。
