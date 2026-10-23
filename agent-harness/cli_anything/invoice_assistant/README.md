# 发票助手独立 CLI

`invoice-assistant`，同义命令`cli-anything-invoice-assistant`。直接调用原发票助手本机API，不需要个人工作台、浏览器自动化或新增MCP服务器。原账本/原件/人工字段/GLM与Jev配置仍由原工具管理。

```powershell
invoice-assistant --json doctor
invoice-assistant --json projects list
invoice-assistant --json --project "原项目ID" project inspect
invoice-assistant --json --project "原项目ID" materials list
invoice-assistant --json --project "原项目ID" materials prepare --id "材料ID"
invoice-assistant --json --project "原项目ID" expenses classify --id "费用ID"
invoice-assistant --json --project "原项目ID" matches suggest
invoice-assistant --json --project "原项目ID" reports list
invoice-assistant --json --project "原项目ID" reports download --report-id "上一步ID" --output-dir "D:\新的报表副本"
invoice-assistant --json results show "D:\新的报表副本"
```

材料与费用支持`show ID`。list可用`--offset/--limit`；输出total/has_more，不把截断列表当全部记录。`project inspect`的known_total_cents仅合计已知金额，另列amount_pending；费用ID各计一次，不按原件累加。所有项目动作必须用稳定project_id，不能根据群名/商家推断归属。

prepare默认只读取已有识别；缺结果列needs_recognition，不自动补识别。用户确需处理时加`--recognize-missing`，仅缺结果的材料进入原本地优先、GLM兜底流程，不强制AI重读。classify默认复用缓存/人工分类，缺结果列needs_classification；显式`--classify-missing`才调用原规则/经验/缓存/Jev。已有人工类别（包括“其他”）、人工报销类型/名称、已核对或已提交费用始终保护。配置和现有模型分工不改。

`materials receive --file "原PDF或图片"`先按digest查重；已有同项目原件直接复用，另项目原件拒绝重绑，不自动识别。20MiB以上单文件请用原页面。新材料入库不等于核对或费用登记。

`drafts create --request "请求.json" --request-id "stable-request-001"`只调用原生create-batch，明确创建**未核对草稿**。request-id重复且请求相同由原数据库回读；改内容不能重用同一ID。JSON示例（使用matches suggest实际返回的候选ID/token，不猜）：

```json
{
  "groups": [{
    "suggestion_id": "原建议ID",
    "snapshot_token": "原快照token",
    "fields": {"project_type": "other", "project_name": "已明确的项目名称", "amount": "已明确的金额"}
  }]
}
```

候选、材料版本、明确金额/分类/名称、同项目及原件校验均由原服务完成。缺项、冲突、多种可能组合不能由CLI猜测。创建不执行核对或正式提交，仍由用户在原页面操作。本版不提供自动核对、submit、金额编辑或任意API转发。

reports只下载原先已生成的PDF/Excel/清单，不触发Office/排版、重新登记或提交。一个包可能涵盖多笔费用，返回expense_ids；保留生成时历史快照，不能称为当前修订报表。输出新目录，记录hash/格式及来源；重复同目录校验读回，损坏或请求不同直接报错，不覆盖也不重生成。旧包没有原hash时只记录本次观察hash。

`--json`、`--project`、`--url`、`--workspace-id`和`--dry-run`均放子命令前。dry-run不POST、不写输出；草稿dry-run校验候选token，完整业务校验仍由原服务事务执行。退出0是命令成功，不表示费用已确认；检查status、pending_review、missing等。退出1服务/状态，2参数，4身份/范围/损坏/原服务业务拒绝，5输出锁，6写操作结果不确定。6时先查询，不盲目重发；只有明确403前置拒绝才刷新token重试一次。

operations区分recognition_requests/classification_requests与glm_calls/jev_calls；只读复用时均0。显式调用原识别/分类后，原服务没有提供的模型次数为null，不能把HTTP请求数当模型计费次数。模型订阅是调用Agent的配置，不在本CLI里保存。

无参数或`repl`进入交互模式，quit退出，项目选择只在进程内沿用；账本状态由原服务持久化。原服务没有通用undo/redo，CLI不伪造撤销。

安装：原发票服务已启动后，在agent-harness运行`Install-CLI.ps1 -Python "Python路径"`。仅本机可编辑安装，不制作新的发行EXE或对外发布。配置`%LOCALAPPDATA%/InvoiceAssistantCLI/config.json`含url/workspace_id，不含凭据；安装时核对原服务身份，不改GLM/Jev或订阅。命令放用户`.local/bin`，作用域明确的Skill安装到Codex和Hermes。

默认8765及绑定的workspace_id；切换隔离服务需要同时指定`--url http://127.0.0.1:端口 --workspace-id "原runtime的工作区ID"`。也支持`INVOICE_ASSISTANT_URL`与`INVOICE_ASSISTANT_WORKSPACE_ID`。只接受本机HTTP，禁止重定向，绕过系统代理；不自动启动/重启服务，未连接时请使用原发票助手启动入口。
