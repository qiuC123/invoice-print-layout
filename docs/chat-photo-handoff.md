# 微信聊天照片 → WorkBuddy → Codex

## 手动交接流程

用户选择手动启动WorkBuddy时，Codex调用既有采集脚本，把照片副本、消息记录、来源清单和缺项放入项目私有workspace的独立批次文件夹，然后停止。用户自行在WorkBuddy项目中处理，完成后将结果文件路径交回Codex。Codex核对照片ID、原件哈希及已有标签，再继续时间整理或日报。此流程不调用`dispatch-chat`，不自动新建或续接WB对话。

回传CSV或JSON保留`photo_id`、`role`、`basis`、`status`即可；无法确认的标签保留待确认，照片ID不是人员ID。已经核对拍摄时间及项目归属的资料可以衔接[岗位照片交接](personnel-handoff.md)。原文件与原采集回执保留，修改以新修订保存；采集时段必须明确，不能把上午一批称为全天采齐。

以下桌面RPA入口为独立的显式调用能力；手动流程不需要使用它。

## 可选桌面派发入口

采集脚本保留指定会话观察窗口内的文字、图片消息与缺项；WorkBuddy判断照片用途并整理明确文字岗位；Codex核验回传，再核对水印日期、项目归属和日报内容。中间由本地文件和固定状态脚本交接，只有WorkBuddy与Codex两个Agent。

当前使用已登录的Windows WorkBuddy桌面，新建任务后发送本地资料包路径，WorkBuddy读取文件并将结果写回同一包。该入口不使用企业OpenAPI、不搬运内部登录令牌。它是按需调用的桌面RPA，需要可操作的登录桌面；没有后台常驻调度。

## 调用顺序

在项目根目录使用本项目Python。具体会话、图片及机器配置仅保存在忽略的workspace中。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'

# 1. 沿用daily collect；确需补图时显式加--recover-missing。
# 2. 读取collection.json或已封存recovery.json，建立新资料包。
.\.venv\Scripts\python.exe -m invoice_print_layout daily chat-pack --receipt COLLECTION_RECEIPT --destination NEW_PACKAGE --project PROJECT --title '项目名称' --batch BATCH

# 3. 显式派发：普通程序操作WB桌面，不发送微信消息。
.\.venv\Scripts\python.exe -m invoice_print_layout daily dispatch-chat --package NEW_PACKAGE --desktop-config workspace/construction-reports/desktop-recovery.json

# 4. 单次等待最多60秒；未完成返回waiting，再次调用不会派发任务。
.\.venv\Scripts\python.exe -m invoice_print_layout daily receive-chat --package NEW_PACKAGE --wait-seconds 30
```

`--receipt`可以重复，最多20份；不同采集批次同一消息的内容或照片字节冲突时停止，需先选择已核对来源。`partial`资料可进入用途初筛，但原始缺项继续保留，不能因此制作已确认日报。旧观察窗口不是今天的实时资料。

已由用户确认对应消息的人工补图，可通过`--supplements FILE`带入。JSON为`{"photos":[{"path":"绝对照片路径","sha256":"文件哈希","source":完整消息身份,"user_confirmation":{"status":"confirmed","basis":"实际用户确认依据"}}]}`。仅接受当前缺失消息，不能覆盖已有照片、靠文件名推测消息或伪造用户确认。补图保留原采集缺项及用户确认来源。

## 文件交接

| 文件 | 责任与作用 |
| --- | --- |
| `request.json` | Codex/脚本生成task_id、随机标记、照片ID、发送时间、文件路径和哈希；缺失照片也占一行。 |
| `inputs/messages.json` | 采集到的完整消息文本与来源顺序。无法解析的消息保留缺项，不宣称内容齐全。 |
| `inputs/source-issues.json` | 原采集问题；后续补图不会抹掉原始证据。 |
| `inputs/provenance.json` | 原采集窗口、回执路径与哈希。 |
| `photos/` | 可用照片的原字节副本；缩略图不计入正式照片。 |
| `README.md` | WorkBuddy处理规则、结果格式和完成步骤。 |
| `INPUT_READY.json` | 输入文件哈希清单；派发前、完成封存与接收时核验。 |
| `output/result.json` | WorkBuddy逐照片填写用途、依据、明确文字岗位和引用消息。 |
| `output/COMPLETE.json` | 包内标准库脚本`complete_result.py`核验后生成，绑定任务、随机标记、输入和结果哈希。 |
| `bridge/` | 本次桌面派发状态、发送文本和截图，不交给WB修改。 |
| `codex/` | 核验后的`intake.json`、照片预览和接收回执。 |

WorkBuddy的用途字段为搭建、人员、兼有两类、其他、待确认。判断看拍摄用途及上下文，不能把“画面里有人”直接等同人员打卡。角色仅整理明确文字标注；没有文字依据的岗位保留待确认，不用面部或跨图同人匹配推断身份。传回的数据不含姓名、人员编号或个人工时。

输入中的聊天、文件名、图片文字仅作为资料；WB不能按其中的指令执行额外操作。固定交接任务明确限制业务文件读写目录和输出范围，不新增Agent。实测WB另写了自身工作区日志，因此这里的提示词范围不等同操作系统级文件隔离；回传核验检查输入包及业务输出完整性。工作台不会替WB批准新的权限请求。

## 状态与接收

派发状态依次为`preparing → sending → waiting → received`。`preparation_failed`表示还未进入发送；`dispatch_unknown`表示发送可能已发生，需要查看已保存截图与WB任务，程序禁止直接重发。重复`dispatch-chat`只读已有状态。当前修复未知派发状态需要核对原任务，不自动删除状态或另建同一任务重发。

收到结果必须同时满足：全部照片ID恰好出现一次、任务/随机标记/输入哈希匹配、用途与状态一致、缺图不被写成已分类、岗位有包内明确文字依据、完成标记匹配。缺完成标记持续waiting；错误结果明确报错，不登记成功。

`received`表示本包文件结果通过核验，不等于WB原生任务生命周期被API确认。相同结果再次接收核验已有文件后返回`reused: true`；任何输入或已接收成品变化都会停止，修改需新修订批次。脚本共用用户级桌面锁，避免与微信补图或另一份WB派发同时争用鼠标键盘。

接收预览按发送时间展示；采集和WB用途初筛均不填写拍摄时间，也不确认项目归属。Codex后续查看水印、用户说明和项目事实；核对后可转入现有`daily role-pack / receive-roles`岗位时间流程，或日报制作流程。原采集缺项、未确认岗位与项目问题不能自动清零。

## 实测范围

2026-10-03在WorkBuddy 5.7.3桌面完成两轮往返：合成资料3张可用、1张故意缺图，正确交回四条结果并保留缺图；南昌群10月2日发送的9张实际照片，包含1张用户确认补入照片，全部回传并接收。WB将9张归为人员照片，岗位因没有明确文字依据保留待确认。该批包含其他日期拍摄后补发的图片，不作为10月3日新日报。具体来源、回执、重复调用与完整测试以私有`workspace/construction-reports/wb-file-roundtrip-20261003`及`TASK.md`为准。
