# Codex执行策略只读探针

`scripts/codex_policy_probe.py`用于调查执行工具返回的`CreateProcess ... blocked by policy`，不是服务启动器或解锁工具。

采集范围：指定任务的当前SQLite权限字段；配置文件中与权限相关的少量字段；该任务JSONL内真正的工具拒绝回执；拒绝前后3秒的策略/审批/沙箱及对应调用日志元数据；用户规则与项目规则的离线判断。

探针只启动官方`codex.exe execpolicy check`，把历史命令作为`--`后的参数交给规则检查器，**从不执行历史命令**。SQLite使用只读连接和query_only；不修改配置、规则、凭据、服务或Codex数据库。仅在指定输出目录生成JSON和Markdown，包含时间、调用编号、日志行编号和命令哈希，不导出原始命令、日志、规则正文或密钥。

示例（路径需按本机实际安装位置填写；shell必须来自拒绝信息中的真实外层shell路径）：

```powershell
.\.venv\Scripts\python.exe scripts/codex_policy_probe.py --thread-id <任务ID> --codex-exe <官方codex.exe路径> --shell <拒绝信息中的pwsh.exe路径> --output workspace/policy-probe/<日期>
```

`no_match`只表示本次提供的规则文件没有匹配，不意味着完整执行链允许该操作。当前任务数据库不等于每次历史拒绝时的权限快照。日志按目标和时间有界筛选，未找到审查记录不能证明没有审查；没有理由就不能武断归因于自动审批、某条PowerShell指令、Windows权限或杀毒软件。

2026-09-24本机结果：4次拒绝、4次本地规则检查均no_match，包含一次无Stop-Process的隔离预览启动。当前任务sandbox disabled、approval never；探针仍未取得具体拒绝规则。私有报告在`workspace/policy-probe/20260924/`，后续反馈工具问题可使用其中时间和调用编号定位，发送前仍应人工查看报告。

官方资料：[规则检查](https://learn.chatgpt.com/docs/agent-configuration/rules)、[审批与安全](https://learn.chatgpt.com/docs/agent-approvals-security)。

## 增强与源码级离线验证

探针现在额外记录：调用前的历史 turn_context 权限类型、可恢复的字面工具参数摘要、规则文件 SHA256、HTTP URL 数量及本机/其他类型。URL 地址、查询参数和工作目录正文不导出；文本共现不伪称同一解析片段命中。完整 argv 无法无损恢复时明确标为 `exact_argv_recovered=false`，本地规则回放不能宣称与真实执行链等价。

2026-09-25 的进一步验证编译了 `rust-v0.154.0-alpha.6.2` 官方的 PowerShell tree-sitter 解析模块与 Windows 危险检查模块，未修改这两个文件。历史脚本文本仅作为标准输入字符串，未调用 PowerShell、启动器或任何被拒命令。结果和限制见 [源码对照结论](policy-source-review.md)。探针自身的 Python 测试不代替这个真实 Rust 源码实验。
