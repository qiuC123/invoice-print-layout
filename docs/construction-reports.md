# 每日施工日报

车展项目的三项工作边界及五类照片模板见私有[车展项目模板](../workspace/construction-reports/车展项目模板/使用说明.md)。搭建日报仅覆盖搭建期间；展期、撤展及运输材料继续归档并用于结算，不因此新增搭建日报。每站的日期、工时、人员和清单仍须读取该项目实际资料，已确认规则不重复询问。

已有岗位文字标签及照片的交接入口见[人员交接说明](personnel-handoff.md)，与日报制作及照片采集调度分别调用。

这个入口把已登记项目的当天资料制作成可编辑 PPTX 和 HTML。由本次执行 Agent 核对照片、事实和施工阶段，程序核验日期、来源、工时、模板与文件完整性。WorkBuddy、Codex 或其他具备本机工具访问能力的 Agent 均使用相同正式 CLI 和清单契约。运行在本机，不启动工作台，不调用分类服务或发送报告，也不改变照片调度。

用户可以直接交代“制作某项目某日施工日报”。执行 Agent 按以下步骤完成；已确认且保存的规则不重复询问。首次或恢复时先读根 `TASK.md` 和 `workspace/project-memory/入口.md`，按其记录读取实际共享规则并核对来源及日期。阶段进度由当前主控持续维护，已有聊天摘要不能代替正式状态或当天事实。

## 调用

在项目根目录执行，始终显式设置 `PYTHONPATH`，避免可编辑安装指向其他 checkout：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m invoice_print_layout daily run --project PROJECT --date YYYY-MM-DD
```

默认私有配置为 `workspace/construction-reports/registry.json`。`PROJECT` 可用项目键或唯一别名。已有当天完整清单时直接制作；没有清单时返回 `needs_input` 与缺项，退出码 2，不猜写日报。

需要采集时：

```powershell
.\.venv\Scripts\python.exe -m invoice_print_layout daily collect --project PROJECT --date YYYY-MM-DD
```

只读取配置中的精确会话及中国时区当天窗口，保存消息文本、图片、原缓存来源及质量说明。离线、缺缓存、分片缺失等错误保留在回执中。相同配置和日期的完整采集可直接复用；新增消息或需要重新采集时使用新的采集目录/更新经核对的来源配置，不覆盖旧证据。采集成功不代表项目归属和施工内容已确认。

### 缺图自动恢复（显式启用）

```powershell
.\.venv\Scripts\python.exe -m invoice_print_layout daily collect --project PROJECT --date YYYY-MM-DD --refresh --recover-missing --desktop-config workspace/construction-reports/desktop-recovery.json
```

`--refresh`读取此刻的新消息并新建批次；不加时可复用已完成的观察窗口，不能据此判断此后没有新消息。`--recover-missing`允许本轮操作微信窗口，默认采集仍不操作界面。配置路径及联系人/项目事实保存在忽略的workspace，不能提交。桌面配置包含本机nuphus可执行文件的绝对路径`nuphus_executable`，可选整数`wait_seconds`（1–30，默认3）、`max_photos`（1–50，默认10）、`max_scrolls`（0–20，默认6）。

启用后按顺序执行：

1. 读取指定账号、会话、日期的消息和缓存；已齐全则直接返回，不打开微信界面。
2. 缺图时核对本地联系人库中唯一的显示名称及微信进程；用本地OCR定位微信搜索框、独立搜索结果弹窗，再核对进入后的会话标题。重名或不匹配停止，不选择网络搜索建议。
3. 等待缓存加载，新建一次读取批次。已齐全则结束；否则使用缺图的完整缩略图作像素模板，在有限翻页内定位，打开另存。
4. 保存前核对默认文件名中的发送时间和第一个数字编号；末尾序号不等于消息本地编号，不能据此判错。图片解码、来源和文件哈希核验后，合并到独立补图回执，再次读取缓存。

`attempt-*/collection.json`始终保留原始采集结果；`recovery-*/recovery.json`保存综合状态、`recovery_steps`、全部原采集回执路径、另存照片及缺项。成功补图只消除对应图片缺项，不消除文本或发送者不明等其他问题。缩略图单列为`thumbnails`，不计作已取得正式照片。GUI另存不会伪装成源DAT解码结果。

找不到缩略图、相同图片出现多次、窗口被移动/焦点丢失、搜索结果不唯一或保存消息不匹配时，保留`partial`及原因；不会猜测配图、发送消息或派发WB。当前支持已验证的Windows微信界面和有限历史范围，超出范围需人工定位或补发。普通脚本、OCR和像素匹配不引入第三个Agent。

通过同一用户级文件锁避免多个采集任务同时操作微信。相同观察消息集的完整补图结果经文件哈希核对后复用；`--refresh`会重新观察。中断后保留事件和文件，下次先重新采集缓存，不盲目重复保存或覆盖旧成品。

## 执行步骤

1. 读取项目配置及当天清单。已有完整资料直接重放；否则调用限定采集，并查看 `messages.json`、`result.json` 和图片。
2. 找到“搭建结束”前的连续图片，核对水印日期、地点及项目。多个批次不得简单选最后一个；记录明确选择依据。无结束文字的已指定私聊，允许确认连续批次，必须有用户指定或逐图核验依据。消息发送日期不能代替拍摄日期。
3. 核实工时和此前累计截止日。只复用 `confirmed_work` 中当前日期记录；没有当日起止时间、此前累计或跨夜情况时明确询问。天数由 `first_day` 按自然日加一计算，卸货日可以是第一天。
4. 按现场事实选择对应阶段，填写当天进度、安装内容及照片说明。阶段对应的明日计划可按已确认 `plan_rule` 使用。原模板最终搭建阶段只有三页且无明日计划时，按下方显式配置保留其结构。配置没有该阶段时先检查参考模板和确认来源，不能擅自套用另一天已完成状态或百分比。
5. 将依据写入项目 `days/YYYY-MM-DD.json`，执行 `daily run`。可用 `--manifest PATH` 指定修订清单，`--output-root PATH` 指定新的验证根目录。原始模板、照片和历史基底从冻结副本读取，旧交付目录不会写入。
6. 打开生成的 HTML，查看**每一页**及原生页面。确认新日块事实、照片、换行、前三页/历史和备注。原生导出与历史比对通过只到 `native_verified`；实际逐页查看后才登记完成：

```powershell
.\.venv\Scripts\python.exe -m invoice_print_layout daily review --run RUN_DIRECTORY --reviewer REVIEWER --evidence '逐页检查的实际结果与证据位置'
.\.venv\Scripts\python.exe -m invoice_print_layout daily status --run RUN_DIRECTORY
```

7. 向用户提供 PPTX/HTML 与实际状态，区分历史重放和当天新报告。如果需要向项目正式日报目录复制，先检查已存在交付及哈希；不同内容采用新修订文件名。这个命令不发送、不覆盖已确认交付。不要为了验收调用飞书预览或发送 API。

操作 PPT 前遵循当前 presentations 技能及其 runtime/finalizer 规则。正式 `daily run` 在每个新 attempt 的首次编辑前自动运行一次 operation marker，并保存 `build/operation-marker.json`；调用方不再额外运行 marker，也不手动重放已开始的 attempt。复用成功产物时不会重复标记或编辑。

本文没有固定 runtime 安装路径。配置维护者通过本机依赖发现工具取得路径、确认依赖存在后，写入私有 registry；执行 Agent 使用该配置。共享记忆不代替机器上的 Node、Python、Artifact Tool、技能包或 PowerPoint 安装。

## 正式生成与校验

Python 外层按顺序调度：operation marker → Node 编辑阶段模板 → PowerShell 原生合成 → 正式 finalizer → PowerShell 原生验证及逐页导出 → HTML。Node 编辑器负责编辑和预览，不再由它启动 PowerPoint 子进程。项目、日期、模板及素材来自清单与冻结输入，不依赖历史实验脚本或实验目录。

最终化先实际调用技能包的 `finalizePresentation`，使用从原模板编码文字中读取的完整字体族、实际画布尺寸和预期页数。只有该调用返回结构化的子进程 `EBUSY`，才由 Python 外层直接启动同一技能包的完整校验链：参考字体、包完整性、图表发现与标题、布局及字体、表格算术、存在图表时的定量图表验证，以及 Artifact Tool 重新导入。候选和参考模板哈希必须保持不变，全部通过后才能发布独立成品。其他错误明确停止，不因日志文字中出现 `EBUSY` 就启用备用路径。

`build/validation.json` 和状态中的 `execution` 记录实际路径：

| 字段 | 正式 finalizer 返回成功 | EBUSY 后完整备用校验通过 |
| --- | --- | --- |
| `finalizer_returned` | `true` | `false` |
| `fallback_used` | `false` | `true` |

备用校验通过不表示 `finalizePresentation` 调用成功，也不等于视觉验收。`finalization_receipt` 指向本次最终化回执，各阶段日志保留实际退出码；失败不会登记为 `native_verified`。不修改执行策略或安全设置。

原生步骤只关闭自己打开的演示文稿，不调用 Office `Quit` 或终止进程。若本次要操作的同一路径文稿在开始前已经打开，则停止，避免接管用户文稿。合成与验证分别保存 `window-protection-compose.json`、`window-protection-verify.json`，核对原先已打开的演示文稿仍然存在；窗口保护未通过时不登记完成。

## 私有配置与事实清单

registry 的 `projects` 映射 `{项目键: {aliases: [], config: "相对配置路径"}}`；`runs_root` 为私有运行目录。`runtime` 包含 `node_executable`、`artifact_module`、`python_executable`、`skill_dir`，均为当前机器绝对路径；可选 `powershell_executable` 指定本机 PowerShell，未设置时查找 `powershell.exe`。

项目条目可另设 `config_by_date: {"YYYY-MM-DD": "历史配置路径"}`。`config` 始终保存当前配置；指定日期时仅按合法 ISO 日期精确匹配 `config_by_date`，未命中则使用 `config`，不自动选择最近或最新历史配置。项目别名遵循相同选择规则，所选配置的 `project_id` 仍须与登记项目一致。历史日期曾使用不同会话、`source_id` 或阶段配置时保留独立映射，不能为通过核验改写旧来源身份。未传日期的调用方继续使用当前 `config`。

项目配置 `schema_version: 1`：

| 字段 | 用途 |
| --- | --- |
| `project_id`, `report_title`, `first_day` | 项目身份、报告标题和含卸货的首日 |
| `source` | `source_id`、`chat_type` (`group/private`)、精确 `chat_id`、`account`、`reader_config`、`batch_policy` (`closeout/explicit`)、`closeout_marker` |
| `confirmed_work` | 按日期保存 `status: confirmed`, `date`, `start`, `end`, `prior_hours`, `prior_as_of`, `evidence`；不能成为后续默认值 |
| `stages` | 阶段键映射模板路径/哈希、`block_start/block_end`、`edits/texts/image_slots`、已确认计划规则 `plan_rule`；默认四页，明确最终阶段可用三页 |
| `evidence_files` | 规则来源文件；会冻结并记录 SHA-256 |

阶段 `edits` 为 `{id, old, text}`，锚点来自实际 Artifact Tool inspect，旧文本必须恰好出现一次且位于所选页块。`texts` 为 `{id,text}`，用于完整图片说明等。模板必须覆盖新日块所有当天事实和照片槽；旧站点事实不能作为缺省值。替换文本可用 `{month}`、`{day}`、`{ordinal}`、`{start}`、`{end}`、`{hours}`、`{cumulative}`、`{next_month}`、`{next_day}`、`{project_title}`，以及每天核验的业务字段。工时/日期计算值禁止由业务字段覆盖。

通常日块必须包含进度、安装内容、现场配图、明日计划四页。只有已核对的最终搭建阶段，才可在阶段中设置 `is_final_stage: true`，并在 `plan_rule` 中同时保存 `status: "confirmed"`、实际 `evidence` 和 `omit_next_day_plan: true`，使用进度、安装内容、现场配图三页。两个开关必须是 JSON 布尔值，不能用字符串 `"true"` 代替；当天 `facts.stage_review` 仍须确认该阶段。缺少标记、确认或依据时，三页日块会停止制作。不要为了补齐第四页臆造次日计划。页数按选定三页或四页计算，前三页和历史保留规则不变。

每日清单 `schema_version: 1`：

| 字段 | 用途 |
| --- | --- |
| `project_id`, `report_date`, `mode`, `source_id` | `mode` 明确为 `replay` 或 `daily`，身份须匹配配置 |
| `messages_path`, `issues` | 当天消息文件；存在未解决缺项时停止制作 |
| `batch` | `status`, `evidence`, 有序 `message_ids`, `closeout_message_id`；私聊或多批次时加 `selection_evidence` |
| `photos` | 每张 `photo_id`, `message_id`, `path`, `sha256`, `quality`, `captured_date`, `project_id`, `review: {status,evidence}` |
| `selected_photos` | 按模板图片槽顺序排列的照片键，不允许重复照片 |
| `facts` | `status`, `evidence`, `stage`, `stage_review: {status,evidence}`, `values`（当天事实字符串） |
| `work` | 可选当天已确认时间；缺省只查配置中完全相同日期 |
| `base` | 上一日历史基底的 `status`, `evidence`, `project_id`, `as_of`, `path`, `sha256`, `slides`，可选 `drop_blank_last`, `cover_replacements` |
| `evidence_files` | 其他需要冻结的确认依据 |

`confirmed` 是执行 Agent 将实际用户确认或逐图核验写入私有清单的记录，不是程序自动识图。新业务信息缺失时必须向用户明确提出。普通缓存始终保留缓存质量描述；不称为拍摄原图。

## 状态和恢复

运行目录为 `runs_root/项目/日期/输入与代码指纹/`。指纹包含输入清单、配置的 runtime 字段、业务代码和共用执行/校验模块，辅助代码变化也会建立新的运行身份。`inputs/` 保存内容校验的原件副本，`state.json` 保存来源清单、批次、工时、attempt、输出哈希、实际 `execution` 及核验状态。`attempt-*/build/` 保存具体编辑 job、Artifact Tool 检查、最终化回执、marker、窗口保护、原生回执和日志；`attempt-*/output/` 保存 `report.pptx`、`preview.html`、`pages/`。

相同输入及指纹的 `native_verified/complete` 运行检查已有产物后返回 `reused: true`，不再渲染或复制照片。最终化、marker、窗口保护等验收回执也登记为不可变 artifacts，修改后不能复用或登记人工 review。失败或中断保留旧 attempt，再次执行原 `daily run` 命令创建新 attempt，复用同一份已核验输入快照。OS 文件锁防止同一运行并发写入，进程退出自动释放。原件或成品改变会明确停止，不能把损坏的既有结果冒充成功。

恢复时先读取 `daily status`，按其 `attempt_path` 查看本次失败日志或待审产物。没有清单时为 `needs_input`；开始生成后为 `rendering`，可捕获的执行失败登记为 `failed`。强制中断留下的 `rendering` 不表示完成，锁释放后再次调用会建立新 attempt。全部程序检查通过为 `native_verified`，仍需执行 Agent 实际查看每页并使用 `daily review` 登记 `complete`；不得直接编辑状态文件跳过这些步骤。

PPT 由 Artifact Tool 编辑阶段模板、PowerPoint 原生插页及清备注，再经 finalizer 和 PowerPoint 重新打开导出。保留页按原生 PNG 哈希逐页比较，明确封面文字修订除外；删除末页前检查确为空白。PPT 包还检查页数、所选照片原字节、空备注。预览依赖 `pages/`，共享 HTML 时需连同整个 `output/` 目录；本轮不添加发布打包。

如果保留页连续导出的图像与基底不同，验证器会保留该图像，并重新只读打开成品、单独导出这一页，以排除 PowerPoint 页面访问顺序造成的文字渲染差异。回执 `isolated_exports` 记录两次哈希；仍须与基底精确一致才通过，不修改 PPT、不放宽比对条件。

## 验证范围

真实历史重放和机器测试证据保存在私有 workspace，当前结果及证据入口见 `TASK.md`。合成行为用例覆盖缺项、错项目/日期、批次歧义、工时、模板、原件保护、重跑、中断、最终化成功/EBUSY/其他错误、校验失败与窗口接管拒绝；完整 pytest/mypy 仍须在每次业务修改交付前执行。实际执行环境还需完整日报、全部原生页面及用户原有 Office 窗口的机器验收。历史重放不证明今天微信在线或今晚照片调度成功。
