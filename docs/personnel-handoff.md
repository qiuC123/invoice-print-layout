# WorkBuddy → Codex 岗位照片交接

只有两个Agent。Codex准备已经核对的照片、时间、项目归属；WorkBuddy只填写岗位标签；Codex接收后按时间整理、制作表格和照片预览。程序核验文件与完成状态，不负责规划或分类。目前手动调用入口，没有监视器、自动唤醒、云上传或发送。

前置的完整聊天资料包、WorkBuddy桌面用途初筛和文件回传见[聊天照片交接](chat-photo-handoff.md)。本文仍描述拍摄时间/归属已核对之后的岗位时间整理；两类包不能混用。

## 固定交付

每批使用独立目录，包含以下文件：

| 文件 | 谁负责 | 内容 |
| --- | --- | --- |
| `catalog.json` | Codex | 项目/批次、照片ID、拍摄日期时间、归属、相对路径、SHA-256 |
| `photos/` | Codex | 照片原字节副本，以照片ID命名，源照片保留 |
| `classifications.csv` | WorkBuddy | `photo_id,role,basis,status`四列，每张照片一行 |
| `交接说明.md` | Codex | 填写范围与完成步骤 |
| `COMPLETE.json` | 完成后的核验命令 | 项目/批次、照片数量、清单与分类CSV的哈希 |

岗位只使用：`项目经理`、`技术工程师`、`搭建工人`、`AV技术工程师`、`AV搭建工人`、`待确认`。已有岗位标注填`classified`；无法确认填`待确认`与`pending`，依据写明缺项。`classified`表示有分类结果，不表示用户已确认或人员身份已核实。

照片编号不是人员编号。不要求姓名、花名册、同人对应、跨图匹配或个人工时。WorkBuddy不修改日期、时间、项目归属、catalog、照片或Codex用户纠正记录。

`catalog.json`版本1字段如下；日期为`YYYY-MM-DD`，拍摄时间为`HH:MM`或`HH:MM:SS`：

```json
{
  "schema_version": 1,
  "project_id": "PROJECT",
  "project_title": "项目名称",
  "batch_id": "BATCH",
  "timezone": "Asia/Shanghai",
  "photos": [{
    "photo_id": "PHOTO-001",
    "date": "2000-01-01",
    "photo_time": "08:30",
    "project_status": "included",
    "path": "photos/PHOTO-001.jpg",
    "sha256": "替换为原件的64位SHA-256",
    "sent_at": "2000-01-01T08:35:00+08:00",
    "location": "已核对地点，可留空",
    "event_type": "已有文字记录，可留空",
    "note": "已核对说明，可留空"
  }]
}
```

`project_status`只有`included`（当前项目）、`pending`（归属待核）、`excluded`（异地排除）。待核与异地照片保留在预览及表中，归属计数分别列出。时间资料必须由Codex核对；程序验证格式与文件完整性，不从图片推断时间或项目。

`sent_at`可省略或留空，填写时使用带时区的ISO时间；表格按北京时间展示，JSON保存原文。它与水印拍摄时间分别记录，不用来补出缺失的拍摄时间。

## 顺序调用

项目根目录使用本项目Python，并显式设置`PYTHONPATH`。下列路径是占位，真实资料仅放私有workspace或用户项目目录。

```powershell
$env:PYTHONPATH='src'
# Codex准备：源清单名为catalog.json，照片路径相对于清单目录。
.\.venv\Scripts\python.exe -m invoice_print_layout daily role-pack --catalog SOURCE/catalog.json --destination HANDOFF
# WorkBuddy填写CSV后执行，或将填好的包交回，由Codex执行。
.\.venv\Scripts\python.exe -m invoice_print_layout daily seal-roles --package HANDOFF
# Codex统一接收。registry仅使用runtime中的Node与Artifact Tool路径。
.\.venv\Scripts\python.exe -m invoice_print_layout daily receive-roles --package HANDOFF --output-root PRIVATE_OUTPUT
```

完成标记写入后不再改此包。修改分类须建立新的批次/修订目录并重新完成；同一照片沿用原ID与原字节。完整包未到齐、ID重复/缺失、日期时间无效、越界路径、照片或CSV哈希变化都会停止接收并给出原因。

## 用户纠正

用户明确给出纠正后由Codex保存，不能根据照片自行补出身份或岗位：

```powershell
.\.venv\Scripts\python.exe -m invoice_print_layout daily correct-role --package HANDOFF --output-root PRIVATE_OUTPUT --photo-id PHOTO-001 --role AV搭建工人 --basis '用户明确确认' --note '用户提供的补充说明'
.\.venv\Scripts\python.exe -m invoice_print_layout daily receive-roles --package HANDOFF --output-root PRIVATE_OUTPUT
```

纠正历史保存在`PRIVATE_OUTPUT/PROJECT/user-corrections.json`，同时绑定照片ID与原件哈希。后续接收同项目同照片时优先应用最新用户纠正，原交接岗位仍单列。同ID换了照片字节会停止，不能把旧纠正套到新人/新图上。继续处理同项目时沿用同一个`PRIVATE_OUTPUT`；迁移须带上纠正历史。

## 输出与重复接收

按项目、批次、输入内容及实现版本保存运行记录。成功输出位于独立`attempt-*`目录：

- `time-records.xlsx`：岗位时间汇总、逐张照片记录；日期/水印时间为可排序的Excel数值，计数公式与详情一致。
- `time-records.csv`、`time-records.json`：时间顺序及来源依据；CSV自由文本防止被表格软件作为公式执行，JSON保留原文。
- `preview.html`、`photos/`：可离线移动的照片预览；按日期/上午下午晚间/岗位/归属筛选，可放大、上一张/下一张。
- `inputs/`：本次冻结的清单、岗位CSV、完成标记与用户纠正。
- `QA/`：表格导出重导入、公式、表格PNG、预览脚本语法与筛选函数检查。

`receipt.json`记录所有输出哈希与应用纠正。相同资料再接收时核验已有成品并返回`reused: true`，不重复生成；输出已损坏时停止，保留旧证据。生成中断可重新调用，会建立新attempt并保留失败记录。源照片、WorkBuddy输入、旧日报和旧预览不会被覆盖。

表格只归纳照片事件，不计算个人工时。浏览器页面实际显示与交互须单独查看；脚本语法/筛选检查不代替浏览器视觉验收。启用自动触发与使用另一批真实资料验收，属于后续阶段。
