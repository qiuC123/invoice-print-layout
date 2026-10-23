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
| `/api/transition`、`/api/report`、`/api/review/confirm` | 本版不开放 | 核对与正式提交继续由用户在原页面操作 |

只接受本机HTTP根URL，默认原发票8765并绑定workspace_id；改为测试服务必须显式提供对应workspace_id。每个项目动作需稳定project_id，材料/费用不得跨项目。不自动启停、升级或重启服务。配置只含URL/工作区身份，不存模型Key或订阅。

命令提供JSON、明确错误码、dry-run与默认REPL。无独立账本状态，不伪造undo/redo；原服务持久化业务状态，草稿创建由原服务idempotency_key防重。非幂等写请求遇到超时/5xx/响应不明时返回不确定，不自动重发；只允许明确403前置拒绝后刷新token重试一次。

已有结果读取保证没有GLM/Jev调用。显式补识别/分类记录原服务请求次数；实际LLM次数若原服务不提供则返回未知，不能把一次HTTP等同一次模型请求。缺材料、缺价、冲突、已删除、人工分类和人工核对均不被CLI悄悄改变。历史报表保留生成时快照，不能宣称与当前账本修订相同。

原AGENTS禁止未授权发行打包：本轮只通过setup.py本机可编辑安装、生成PATH入口，不制作发行EXE、对外wheel发布或PyPI发布。
