# WorkBuddy 本地助理接入

本模块是按需执行的合成文字联通探针，不是已上线的照片工作站。普通程序负责传递和校验，Agent 仍只有 WorkBuddy 与 Codex。没有后台监视、云任务切换或真实照片上传。

已登录桌面端的文件交接/RPA入口见[聊天照片交接](chat-photo-handoff.md)，与本文OpenAPI探针独立；桌面实验成功不代表本地助理API已授权。

## 开通条件

按 [第三方应用说明](https://open.workbuddy.cn/docs/third-party-app) 在 WorkBuddy 开放平台注册开发者应用，申请本地助理两个权限，并由用户完成 OAuth 授权：

- `user.localassistant.readable`：查询在线状态与消息。
- `user.localassistant.invokable`：发送任务给用户 PC 上的本地助理。

应用资料草稿：名称“照片日报本地工作流”；用途“向本人 WorkBuddy 本地助理发送岗位文字分类任务，并将结构化回复交回本地 Codex 工作流”。身份、联系方式按用户实际情况填写，不能用虚构资料。此草稿尚未提交。

应用注册、权限审核与 OAuth 回调地址须以后台实际要求为准。回调地址是否支持本机 loopback 尚未验证；不要登记猜测地址。应用密钥、授权码或令牌不写进聊天、文档、命令参数或 Git。客户端登录与开放平台应用授权是不同步骤；本模块不读取 WorkBuddy 内部凭据。

## 已实现的调用

接口依据 [官方 Open API](https://open.workbuddy.cn/docs/openapi)：固定访问 `https://www.workbuddy.cn/openapi/v2`，拒绝重定向，单次网络等待上限 20 秒。调用前须通过正式 OAuth 流程获得有效 token，并由本地调用环境注入 `WORKBUDDY_OPENAPI_ACCESS_TOKEN`。本轮尚未实现 OAuth 回调/兑换/刷新服务，过期时明确报错。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
# 无令牌时返回 needs_authorization，不访问网络。
.\.venv\Scripts\python.exe -B -m invoice_print_layout.workbuddy_probe online

# 每个实验使用独立目录；prepare仅创建固定的3条合成岗位文字。
.\.venv\Scripts\python.exe -B -m invoice_print_layout.workbuddy_probe prepare --folder PRIVATE_PROBE
# 在线检查通过后才发送一次。
.\.venv\Scripts\python.exe -B -m invoice_print_layout.workbuddy_probe dispatch --folder PRIVATE_PROBE
# 每次只读取一次回复；未就绪保持waiting，由调用方稍后再次poll。
.\.venv\Scripts\python.exe -B -m invoice_print_layout.workbuddy_probe poll --folder PRIVATE_PROBE
.\.venv\Scripts\python.exe -B -m invoice_print_layout.workbuddy_probe status --folder PRIVATE_PROBE
```

探针不接受任意照片或任务输入。它使用 SYN-001、SYN-003、SYN-004 三条虚构文字，岗位由输入直接提供，只检查通信与数据传递。与照片分类能力、身份识别能力或工时计算无关。完成真实联通后，才能决定如何接入现有初筛和岗位文件交接。

## 状态与恢复

`request.json`保存固定输入；`state.json`保存发送状态和摘要；仅通过全部校验后才产生`result.json`。

| 状态 | 含义与下一步 |
|---|---|
| prepared | 尚未派发；缺授权或WB离线时保持此状态。 |
| sending / dispatch_unknown | 可能已经送达；禁止直接重发。先人工核对WB消息。当前探针没有自动恢复此状态的接口。 |
| waiting | 已取得发送消息ID；重复dispatch不会重发，只需继续poll。 |
| validated | 收到匹配任务ID、随机标记、输入摘要、全部编号和预期岗位的JSON。重复调用核验已有结果，不重复生成。 |

只接收 assistant/text 回复，不自动响应任何工具授权或AskQuestion。保持原发送消息ID作为读取起点，避免跳过尚未完整的回复。无关对话、部分JSON、缺失/重复记录或错误岗位不算完成，也不保存无关消息正文。

`validated`仅表示这一份回复内容通过验证，**不证明WB原生任务生命周期已结束**。官方当前文档未明确指定现有桌面会话/项目/模型的路由方式，此点需真实接入验证；不能声称会复用某个特定对话或额度。

## 当前边界

离线HTTP替身测试覆盖重复派发、中断、缺授权、离线、错回执、文件篡改、重定向拒绝等；这些测试不等于官方接口实测。实时接入记录只写私有 `workspace/construction-reports`。应用未授权前，整条自动往返仍未验收。
