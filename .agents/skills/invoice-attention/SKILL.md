---
name: invoice-attention
description: 查证发票工作台 invoice.needs_attention 疑点，用 invoice-assistant 查看原件与候选，凭证据或用户明确决定写回并重新整理。用于发票疑点处理，不用于批量收票、核对或出包。
---

# 发票疑点查证与写回

从事件取得 `object`、`ref.workspace_id`、项目和版本。工作台是唯一业务写入方；不用脚本直接改 SQLite，也不改 GLM/Jev 配置。金额、关联与来源不能猜。事件是当时的快照，开始和写回前都要读最新上下文。

## 读取

用事件的工作区身份固定连接。默认本机服务是 `http://127.0.0.1:8765`；其他端口必须同时指定对应的 `--workspace-id`。先用 `doctor` 确认运行身份；身份不符就停下，不改配置来绕过。

```powershell
invoice-assistant --json --url <本机服务URL> --workspace-id <事件workspace_id> doctor
invoice-assistant --json --url <本机服务URL> --workspace-id <事件workspace_id> --project <项目ID> attention show "invoice-material:<ID>"
invoice-assistant --json --url <本机服务URL> --workspace-id <事件workspace_id> --project <项目ID> attention list --offset 0 --limit 100
```

对象也可能是 `invoice-item:<ID>`。上下文的 `version` 是写回必须使用的当前版本，`record.version` 是留疑点时的版本。查看 `record`（原因代码、问题、缺项、整理模型是否试过）、`fields`（包括 `agent_review_reason`、`agent_content`、`field_conflicts`）、`materials`、`related_materials`、`candidates` 和 `audit`。疑点已解决/排除且没有新信息时，返回 `nothing`。列表按 `has_more` 继续翻页。

## 找依据

- 先读本疑点的原件：上下文中的 `path` 是本机原件，`digest` 是入库 SHA256；也可通过服务的 `/intake-file/<材料ID>` 或费用的附件入口查看。保留原文件，不裁改原件或用识别文字代替原件。
- 再查同项目的 `materials list/show`、`expenses list/show`、`matches suggest`、`project inspect`，比较商家、交易编号、金额构成和项目来源。金额相同本身不能证明同一笔；不要把发票和付款重复算成两笔费用。
- 缺原票时用已有“按费用找票”：本机 `POST /api/mail/search`，JSON 为 `{"id":"费用ID","start":"YYYY-MM-DD","end":"YYYY-MM-DD"}`，复用现有邮箱历史查找，不新增邮箱接入。范围最多366天且不晚于今天。使用现有 CLI 的 `Client`（`configuration` + `post`）可保留身份/token保护；结果中的下载原票和邮件引用才是证据。不要把邮件标题、只有链接或尚未下载的候选当原票，也不要整箱重收。
- 当前没有费用ID时先检查同项目材料的邮件来源。新的材料仍通过 `materials receive` 入库；本技能不保证分次收票自动汇合。新增材料不在当前候选中时，保留疑点交用户，不能编候选或改库强绑。

## 写回

有确凿依据时准备 UTF-8 JSON 请求文件，再使用 CLI。支持的动作：

| action | values | 用途 |
| --- | --- | --- |
| `set_fields` | 明确的 `amount`（元字符串）、`expense_date`、`category`、`project_id`、`title`、`merchant` | 补字段或纠正实际冲突；日期用 YYYY-MM-DD；项目用稳定ID |
| `choose_candidate` | `{"candidate_id":"上下文中已有的候选ID"}` | 确认当前材料组合；候选原件、版本和项目会重验 |
| `confirm_content` | `{"goods":["具体商品"],"category":"类别","content_material_id":"对应原件ID"}` | 原件或用户明确的购买内容；不能掩盖原文明示的其他商品 |
| `not_invoice` | `{}` | 未关联材料不是可报销票据，保留原件并归档 |
| `not_reimbursable` | `{}` | 未关联材料明确不报，保留原件并归档 |

`category` 是费用类别（如材料采购、酒店、餐饮），`project_id` 是所属业务项目。多原件费用改总金额必须有原件分项合计支持；接口不分摊猜测。带所属人的对象改项目请交工作台页面处理，避免人员归属串项目。

```json
{"action":"set_fields","values":{"amount":"17.00"},"decided_by":"codex","evidence":{"method":"原件逐字段核实","reference":"材料ID，第1页价税合计","statement":"原件价税合计为17.00元，AI的18.00为识别冲突。"}}
```

```powershell
invoice-assistant --json --url <本机服务URL> --workspace-id <事件workspace_id> --project <项目ID> attention resolve "invoice-material:<ID>" --expected-version <attention-show的version> --request <请求.json>
```

`decided_by=codex` 表示自己查证；用户在飞书明确决定后用 `decided_by=user`，`evidence.reference` 指向该次答复，`statement` 忠实保存选定含义。执行者始终记作 Codex，与页面人工操作分开。不要把推断写成用户确认。

写回只排入后续整理；不直接核对。保留返回的 `job_id`，再检查原对象/费用与 `attention list`；新疑点继续按最新上下文处理。版本冲突时重新查证，不能只替换版本号盲重试。HTTP结果不明先读 `audit` 和现状，不重复提交。只有写回已持久保存且后续任务已排队，才返回 `done`；结果说明是否已自动核对或仍在整理。

## 不能确认与用户答复

证据不足就返回 `needs_decision`。问题、现状、影响和建议用用户能懂的话；给2–3个具体选项，例如“归项目A”“归项目B”“不报”，不要用候选编号或“确认/取消”代替业务选择。不要编金额或猜来源。

同一对象的用户决定回来后，先重读上下文与版本，再按该选择填写 `decided_by=user` 并写回。选择与当前对象不相符时说明变化，不执行旧答案。

每次结果摘要固定附一行 `办法：<实际使用的方法>`，例如 `办法：邮件历史找到原票`、`办法：原件逐字段核实`、`办法：同项目材料交叉核对`、`办法：用户确认项目归属`。没有找到证据如实写 `办法：查阅原件及邮件历史，仍缺对应依据`，为把重复方法变成代码保留依据。
