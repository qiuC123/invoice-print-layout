# CI pytest 分片失败调查（2026-11-16）

## 范围与关联 PR

只读检查 `qiuC123/invoice-print-layout` 的 CI 工作流，范围为 2026-10-17 至 2026-11-16。本范围实际有 40 个 run（最早 11-02）；包含 main push、pull_request 和手工兼容试跑。除失败/取消 run 的 `gh run view --log-failed`，还检查了所有 `run_attempt > 1` 的首次尝试：只看最后结果会漏掉真正的不稳定用例。

基线和查询时远端 main 均为 `25bc167a44e4eb04e92448c0ae0337f1f071bbf5`。[PR #12](https://github.com/qiuC123/invoice-print-layout/pull/12) 仅处理 CI 取消和汇总语义，本改动不碰其 workflow，不需叠放分支。该 PR 的 11-16 run 的 Python 分片全部通过，Browser contracts 因 `organize_nonblocking_browser.cjs` 的 fixture timeout 失败；这是另一个检查，不能算作 pytest 分片失败。

## 失败用例汇总

共 36 条失败记录、23 个不同 nodeid。次数按一次 run/attempt 中的 pytest FAILED nodeid 计，不把堆栈和汇总重复计数；同一 SHA 的 PR 与手工运行分别计数。时间为 UTC。表中参数组逐一列出，次数写为“每个参数的次数 × 参数数”。

| 用例（`tests/` 下） | 次数 | 日期、run/attempt、SHA | 报错及当前结论 |
| --- | ---: | --- | --- |
| `test_material_creation_http.py::test_create_from_one_material_http_required_choice_preview_and_undo[invoice,payment,receipt,detail,purchase,other]` | 2 × 6 | 11-07，R1/R2，`2770ec0` | 旧六项类别断言与新八项不符；确定性回归，已修复 `2d5cb87` |
| `test_review_fields_and_withdrawal.py::test_saved_queue_evidence_suggests_printing_without_changing_fee_or_rereading_original[goods,payment_note,purchase_content,transaction_description]` | 2 × 4 | 同上 | `KeyError: project_name`；已修复 `2d5cb87` |
| `test_review_fields_and_withdrawal.py::test_review_fields_http_state_auth_stale_and_classification_assets` | 2 | 同上 | `assert (8 == 6)`；已修复 `2d5cb87` |
| `test_agent_goods_optimization.py::test_numbered_product_names_are_not_price_metadata[价格标签打印纸]` | 2 | 同上 | 完整耗材名被归成“打印”；已修复 `2d5cb87` |
| `test_intake_special_fields.py::test_complete_native_trip_or_city_waybill_projects_actual_role_locally[运单文本-detail-顺丰-13.75]` | 2 | 同上 | 新“快递”类别与旧“顺丰”断言不符；已修复 `2d5cb87`。参数首段为该测试中的整段合成运单文本，此处缩写 |
| `test_assistant_startup.py::test_ensure_running_never_stops_or_opens_browser[old,malformed]` | 1 × 2 | 11-02，R3，`9bbbd9b` | PowerShell 超过 40 秒；16 分片/4 worker 时发生，`83b5402` 开始串行启动器，`3203145` 完成所有 PowerShell 模块分类 |
| 同一函数 `[occupied]` | 1 | 11-02，R4，`9bbbd9b` | 同上 |
| `test_invoice_assistant.py::test_launcher_mode_switch_and_identity_guards_without_touching_a_live_service[switch_to_invoice-invoice]` | 1 | 11-02，R5 attempt 1，`21284c9` | 32 分片中遗漏 PowerShell 标记，40 秒超时；已修复 `3203145` |
| 同一函数 `[cold-invoice]` | 1 | 11-02，R6，`0181aec` | 同上 |
| `test_phone_link.py::test_pairing_qr_and_session_isolation` | 1 | 11-02，R7，`d669d7f` | OpenCV 旧解码器对有效随机 QR 掩码返回空串；`9bbbd9b` 换检测器并保留失败掩码回归；仅出现在 16 分片时期 |
| `test_mail_agent.py::test_async_kick_never_blocks_http_and_close_does_not_half_commit_a_message` | 1 | 11-02，R8，`fb575ce` | 用耗时 `<0.5s` 判断异步，实测 `0.688s`；`e989a44` 已改为事件先后关系 |
| `test_report_review_http.py::test_http_formal_report_includes_the_same_table_as_final_preview` | 1 | 同上 | HTTP `TimeoutError`；`e989a44` 将 4 worker 降为 2，此后未见复发。历史日志不足以证明具体阻塞点，不将降并发称为已证明产品根因 |
| `test_agent_dispatch_recovery.py::test_answer_while_previous_spawn_worker_exits_continues_same_job[True]` | 1 | 11-08，R9 attempt 1，`773d8ab` | 15 秒后缓存状态仍为 `running`，message 为“整理费用草稿”；同 SHA attempt 2 通过；当前仍有该固定截止等待，本次处理 |
| `test_construction_photo_recovery.py::test_cli_routes_refresh_and_opt_in_recovery` | 1 | 11-10，R10 attempt 1，`4794914` | 导入 fixture 的 lambda 不接受 `package=`，Pydantic 惰性导入触发；已修复 `5a7e157` |

日志链接（均可用 `gh run view <id> --attempt <n> --log-failed --repo qiuC123/invoice-print-layout` 复查）：

- R1：[42359218448](https://github.com/qiuC123/invoice-print-layout/actions/runs/42359218448)，R2：[42359525739](https://github.com/qiuC123/invoice-print-layout/actions/runs/42359525739)。
- R3：[41432499177](https://github.com/qiuC123/invoice-print-layout/actions/runs/41432499177)，R4：[41432491856](https://github.com/qiuC123/invoice-print-layout/actions/runs/41432491856)。
- R5：[41441450361 attempt 1](https://github.com/qiuC123/invoice-print-layout/actions/runs/41441450361/attempts/1)，R6：[41444151908](https://github.com/qiuC123/invoice-print-layout/actions/runs/41444151908)。
- R7：[41429912168](https://github.com/qiuC123/invoice-print-layout/actions/runs/41429912168)，R8：[41448992897](https://github.com/qiuC123/invoice-print-layout/actions/runs/41448992897)。
- R9：[42459739984 attempt 1](https://github.com/qiuC123/invoice-print-layout/actions/runs/42459739984/attempts/1)，R10：[42759027666 attempt 1](https://github.com/qiuC123/invoice-print-layout/actions/runs/42759027666/attempts/1)。

报告中的 28/32 的三次失败由 R1、R2 的类别回归和 R5 的启动器超时组成；30/32 的两次由 R6 启动器超时和 R9 续跑等待组成。它们不是同一失败用例在各分片里反复出现。
## 本次修法与证据边界

R9 的失败发生在续跑子进程已经进入 `create_draft` 后，不是未派发：日志明确显示 `running / 整理费用草稿`。旧测试每 20ms 读取 SQLite，15 秒到期就断言最后一次读取值必须为 ready；它额外施加了产品没有约定的 15 秒整条业务流水线性能门槛。现有任务预算是 180 秒，包含真实 spawn、匹配、落库、商品处理及预览。历史日志没有子进程堆栈，无法断言当时是哪个系统调用变慢；本机原版 20 次（每次整个测试文件、2 worker）均通过，也未证明产品存在死锁。

本次只改该测试的同步方式：

- 用同一 spawn context 的 `Event` 发布“问题已写入”和“允许旧进程退出”，代替探测文件并 sleep；PID 和 queue revision 的文件回执仍保留。
- 在旧进程停住时记录监督线程，确认重复 kick 仍使用同一线程，再释放旧进程。
- `join` 等待该线程真正结束，随后重新读最终状态。等待上限直接取任务现有的 `limits.seconds`，不修改产品预算；任务超时、崩溃、丢失续跑或线程仍存活都会失败。
- 保留同一任务、不同子进程、累计预算不重置、确认回执、1700 分金额、两附件、原件哈希、不自动核对、单笔草稿和清理断言。

这里确实将测试等候整条流水线的上限从固定 15 秒改为已有任务预算，依据是等待对象和业务契约不同，不是让失败重跑。为验证该差异，用一次性诊断补丁在测试子进程的 `InvoiceTools._create` 调用真实实现前注入 16 秒延迟（其他业务步骤、金额和权限校验不变）：旧等待在约 17 秒后产生与 R9 相同的 `running / 整理费用草稿` 失败；新等待允许真实工作完成并通过全部原有断言。诊断延迟仅用于这对反证，未保留在提交中。该实验证明测试截止条件会误报，不证明 CI 当时真的停顿了恰好 16 秒。

不增加重试、skip、xfail，不改分片数、HTTP 超时、业务逻辑或 CI 调度。历史已修复项只记录出处，不重复修改。

## 实际验证与待验收项

- 未改等待的基线文件：`pytest tests/test_agent_dispatch_recovery.py -n 2 -ra` 连续 20 轮，220 项通过；自然负载未复现 R9，未据此宣称原版没有问题。
- 候选同一命令连续 20 轮，220 项通过。受控 16 秒慢路径：旧版 17.06 秒失败，新版 21.90 秒通过；诊断代码已移除。
- 完整两阶段 pytest：PowerShell 串行 75 项通过，其余 2 worker 为 5247 通过、72 失败、6 跳过。两份 JUnit 的并集为 5400 个不同用例，无缺失或重复。
- 72 个失败集中在合成邮件文件写入及其下游空结果；失败样本临时文件路径长 266 字符，父目录存在。保持代码和断言不变，把 pytest basetemp 改为新建的短系统临时目录后，**原 72 个 nodeid 全部通过（24.37 秒）**。复验 XML 的用例集合与首轮失败集合完全相等。没有给 pytest 配置重试，也没有改变产品路径处理；这是本机环境复验，不能写成“完整套件首轮全绿”。
- `python -m mypy src`：123 个源文件全部通过；`git diff --check` 通过。
- 未运行真实网页/浏览器契约：本次仅修改测试同步与调查文档，没有界面或产品行为变更。既有 6 项跳过属于私有模板集成测试。
- 提交前远端 main 仍为 `25bc167`。本角色不执行 GitHub 写操作，未推送、触发重跑、创建或修改 PR；本提交在最新 main 上的 **Python checks / Browser contracts** 仍须由控制器运行并确认。不能引用其他提交的绿灯当成本提交通过。

原始 run/attempt 日志、受控前后 XML、20 轮结果、完整两阶段 XML、短路径复验 XML 和集合核对记录保存在本任务工作树忽略目录 `.runtime/flake-investigation/`，不提交合成附件或运行状态。

本机清理命令在创建进程前被执行策略拒绝（`blocked by policy`），未更换工具绕过；三个测试临时目录及辅助脚本保留，具体路径和存在性写入上述忽略目录的 `cleanup.json`，需后续清理。
