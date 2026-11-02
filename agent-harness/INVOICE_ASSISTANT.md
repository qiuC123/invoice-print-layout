# 接口盘点与命令契约

原工具`invoice-print-layout`已提供独立本机发票服务。原CLI只有process/mail/bot/daily/settlement，发票业务通过HTTP API提供，CLI采用薄客户端；不导入个人工作台，不重写业务核心。

| 原接口 | CLI入口 | 处理方式 |
| --- | --- | --- |
| GET `/api/runtime`、`/api/state` | doctor、projects、project inspect、expenses list/show | 校验app/workspace身份，过滤令牌；保留金额、版本、人工分类/核对状态 |
| GET `/api/intake` | materials list/show/prepare | 先读已保存fields/field_sources/ai_analysis；默认不POST |
| POST `/api/intake/receive` | materials receive | 文件digest查重，保留项目，不自动识别 |
| POST `/api/intake/prepare` | materials prepare --recognize-missing | 仅对无保存结果的指定材料补识别；原本地优先、GLM兜底。无自动force_ai入口 |
| POST `/api/category/auto` | expenses classify --classify-missing | 原规则/经验/缓存/Jev；已有分类或人工/已核对记录不调用。注意旧中台桥接字符串category/run不是当前原服务路由，不能照抄 |
| GET `/api/material-matching`、`/suggestions` | matches list/suggest | 复用原本地规则及快照token，无新增AI编排 |
| POST `/api/material-matching/create-batch` | drafts create --request FILE --request-id KEY | 只消费原建议ID/token和显式fields；原事务/版本/原件校验/防重；结果仍为未核对草稿 |
| state.items.report_files + GET `/export/<filename>` | reports list/download | 下载已生成历史包，不重做PDF/Excel；记录包涵盖的费用ID与原快照性质 |
| POST `/api/reports/create` | reports create --project ID --request-id KEY [--expected-version VERSION] | F23-A（11-02 用户批准范围变更）：用户点击项目出包后，复用原生成/提交流程，只接该项目已核对且未出包的费用；请求编号持久幂等 |
| `/api/transition`、`/api/report`、`/api/review/confirm` | 不开放任意转发 | 自动核对由工作台规则判断；CLI不修改金额、核对结论或材料缺项确认 |

只接受本机HTTP根URL，默认原发票8765并绑定workspace_id；改为测试服务必须显式提供对应workspace_id。每个项目动作需稳定project_id，材料/费用不得跨项目。不自动启停、升级或重启服务。配置只含URL/工作区身份，不存模型Key或订阅。

命令提供JSON、明确错误码、dry-run与默认REPL。无独立账本状态，不伪造undo/redo；原服务持久化业务状态，草稿创建由原服务idempotency_key防重。非幂等写请求遇到超时/5xx/响应不明时返回不确定，不自动重发；只允许明确403前置拒绝后刷新token重试一次。

已有结果读取保证没有GLM/Jev调用。显式补识别/分类记录原服务请求次数；实际LLM次数若原服务不提供则返回未知，不能把一次HTTP等同一次模型请求。缺材料、缺价、冲突、已删除、人工分类和人工核对均不被CLI悄悄改变。历史报表保留生成时快照，不能宣称与当前账本修订相同。

原AGENTS禁止未授权发行打包：本轮只通过setup.py本机可编辑安装、生成PATH入口，不制作发行EXE、对外wheel发布或PyPI发布。

项目出包返回 `files`（PDF/XLSX/MD 文件名）、`expense_count`、`total_cents`、`expense_ids` 与版本。费用提交和成功回执在同一事务里写入；进程中断后可重试同一 request-id。`expected-version` 使用 `invoice.project_ready.ref.version`，制作期间版本再次变化也拒绝发布。不会发送给财务或外部系统。

业务失败以 `{ok:false,error:{code,message,retryable}}` 返回，HTTP 200 保留已处理的明确回执；身份/令牌拒绝仍沿用原 HTTP 规则。CLI 转为非0退出并保留 code/retryable。主要代码：`nothing_to_package`、`version_changed`、`request_id_conflict`、`in_progress`、`template_missing`、`template_invalid`、`runtime_unavailable`、`excel_error`、`storage_error`、`report_error`、`too_many_expenses`、`project_not_found`、`invalid_request`。同一 request-id 成功后始终读原结果；不能跨项目重用。
