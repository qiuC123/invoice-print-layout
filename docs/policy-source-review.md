# Windows 执行拒绝：源码对照结论

## 2026-09-26：隔离验收服务启动已验证

用户要求解决启动阻塞后，将操作缩小为启动已经准备好的验收副本，使用受管理终端中的前台进程，不包含复制正式数据、隐藏后台启动或健康检查。未修改 Codex 权限和执行规则。该调用经现有工具检查正常执行。

执行目录为当前 worktree 的 `src`（确保加载当前源码），命令格式如下；替换为实际的虚拟环境和验收副本绝对路径：

```powershell
& '<venv>\Scripts\python.exe' -m invoice_print_layout.workbench_web --workspace '<acceptance-workspace>' --port 18770
```

随后独立只读检查确认：首页 HTTP 200，`/api/runtime` 的应用、workspace、源码指纹与预期一致且 `ready=true`，台账及带事项 ID 的报表快照 API 正常返回。此实例使用隔离副本，尚未将新版切换到正式台账。

Chrome 控制调用另报 `nodeRepl.fetch request failed`，所以这里只确认真实服务启动及 HTTP 验证，不表示完整浏览器操作验收通过。前台实例依赖当前受管理终端存活，不是持久后台部署。

2026-09-25。范围：对已知 4 次 `CreateProcess … blocked by policy` 作离线诊断；不更改执行策略、不实际运行被拒命令、不重启正式工作台。

## 结论

已从“只有相关特征”推进到**同版源码对历史脚本文本的复现**：4 个文本均无法被 PowerShell 解析器降低为独立的字面命令，退回整体检查后，Windows 危险判断均为 true；同版核心代码在未命中显式规则且审批为 `never` 时，将危险命令判为 Forbidden。

这对“启动入口与本地 HTTP 健康检查在复合脚本中共现”解释提供了强证据。仍不是当时完整 Codex 可执行文件及工具封装的逐字节回放，不能宣称取得了原拒绝回执中的精确规则 ID。

## 核实的源码

固定标签 `rust-v0.154.0-alpha.6.2`，来源为官方 `openai/codex`：

- [powershell_tree_sitter.rs](https://github.com/openai/codex/blob/rust-v0.154.0-alpha.6.2/codex-rs/shell-command/src/command_safety/powershell_tree_sitter.rs)：遇到不支持的动态语法返回无法解析，不执行脚本。
- [windows_dangerous_commands.rs](https://github.com/openai/codex/blob/rust-v0.154.0-alpha.6.2/codex-rs/shell-command/src/command_safety/windows_dangerous_commands.rs)：在词序列中同时找到 URL 和启动入口会命中；本地 HTTP URL 也符合 URL 判断。
- [exec_policy.rs](https://github.com/openai/codex/blob/rust-v0.154.0-alpha.6.2/codex-rs/core/src/exec_policy.rs)：约 877–905 行为解析失败保留整个调用的路径；约 774–814 行为危险判断和 `never → Forbidden` 的路径。行号以此标签为准。

当前正在运行的桌面客户端使用的 `codex.exe --version` 返回 `0.154.0-alpha.6.2`，与任务数据库记录相同。4 次调用前的历史 turn_context 也均能读到 `approval_policy=never`、`sandbox_policy.type=danger-full-access`，补足了以前只有当前配置的证据。但历史二进制指纹未保存，不能把当前版本检查当成完整历史二进制验证。

## 实验及结果

构建独立 Rust 诊断程序，直接编译上述未修改的解析器和 Windows 检查器，只从 stdin 接收 JSON 字符串并输出布尔值和计数。没有调用进程执行 API。推导审批结果参照核心源码分支，未编译完整 core；依赖使用上游约束，解析器及语法包分别固定为 0.25.10 和 0.26.4，完整解析后的依赖版本保存在实验 Cargo.lock。

| 输入 | 拆分 | 检查路径上的危险判断 |
| --- | --- | --- |
| 4 个历史脚本文本，各自检查 | 全部失败，退回整体 | 全部 true |
| 本地程序启动、不含 URL | 该样例保持整体 | false |
| 仅本地 HTTP 健康检查 | 成功 | false |
| 两条简单字面命令构成的序列 | 成功，2 段 | 两段均 false；若强制整体扫描则 true |
| 含变量/循环的启动与健康检查复合样例 | 失败，退回整体 | true |
| 启动入口直接接收 URL，正向对照 | 成功 | true |

因此不能把“任意同一段脚本含启动和 URL 必然拒绝”作为结论，**能否解析拆分是关键变量**。对照文本只用于不执行命令的源码实验，不构成改写命令绕过实际策略的操作建议。

## 保留的边界

- 原始脚本文本从对应工具调用中恢复，哈希与旧探针一致；回放 wrapper 使用重建的 `pwsh.exe -Command`，原始完整 argv 的无损恢复未成功。工具默认值、编码前缀或其他封装仍需单独核实。
- 检查器与解析器源码真实执行；`never` 对应审批结果是从已核对核心分支推导的，不是假称整个运行时已执行。
- 本地显式规则仍为 4 次 `no_match`。这个结果仅限提供的规则，不表示完整链路无策略。
- 未取得原始内部决策追踪或审查理由。若需要维护方确认，应附准确版本、时间/调用编号、脱敏最小样例、源码哈希和上述边界；不附业务票据、凭据或私人日志全文。

## 对项目的修复

1. 新增冻结于进程加载时的后端源码指纹与运行信息接口；启动器区分就绪、旧版本、不同实例、缺能力和启动超时，不再把 HTTP 成功当作新版已加载。
2. 探针补充历史权限上下文、URL 类型、参数恢复边界和规则指纹。
3. Jev/OCR 移出全局写锁，仅在前后版本读取/校验时持短锁。同步保存可以完成，变化后的旧建议作废。

本轮全量 pytest 413 项通过（含私有 Excel 模板集成），mypy 33 文件通过，分类页面浏览器回归通过。Windows PowerShell 启动器测试替换了网络/进程副作用，覆盖 12 个状态；不等于正式服务已重新启动。两条原有 Lark 弃用警告仍在。

源码修改没有修订 Codex 权限。当前会话仍为 `never`，不支持请求升级执行，不能在此重复或换入口执行已被拒绝的服务操作。功能加载需通过允许的维护操作另行完成；完成后以运行指纹和 `category_suggestion_ready` 验收。策略根因调查与业务版本加载分别记录。
