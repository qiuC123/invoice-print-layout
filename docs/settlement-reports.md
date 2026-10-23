# 结算照片核对稿

类似车展项目的五类照片目录、主体细分与经验入口见私有[车展项目模板](../workspace/construction-reports/车展项目模板/使用说明.md)。归档主类与结算清单项目分别记录；报告配图标题以当前项目清单原名为准，有依据可复用同图，不沿用日报每页照片数量限制。

本入口供任一 Agent 调用，把已确认照片放入原结算模板，输出可编辑 PPTX、原生 PNG 和 HTML。当前范围是照片核对稿；实际验收数量、个人工时、人天、费用与签收仍需业务依据。本入口不计算这些结算结果。

## 调用

在项目根目录运行；Python、Node、Artifact Tool、PowerPoint 和 skill runtime 使用本机已登记环境，不临时安装替代程序。

```powershell
$env:PYTHONPATH = 'src'
& '.venv/Scripts/python.exe' -m invoice_print_layout settlement run --project '<project>' --manifest '<confirmed-manifest.json>'
& '.venv/Scripts/python.exe' -m invoice_print_layout settlement status --run '<run-path>'
& '.venv/Scripts/python.exe' -m invoice_print_layout settlement review --run '<run-path>' --reviewer '<actual-reviewer>' --evidence '<actual-page-review-evidence>'
```

默认 registry 为私有 `workspace/construction-reports/settlement-registry.json`；可显式传 `--registry` 和 `--output-root`。当前登记了南昌模板；杭州结算模板尚未登记，不混用。退出码 2 和 `state=needs_input` 表示缺少明确业务资料；哈希、项目或模板不符会停止报错。

## 输入

registry 保存 runtime、runs_root、projects；项目 config 保存 settlement 模板绝对路径、SHA256、尺寸、字体、修改锚点及表格列索引。仅原模板实收栏为空时生成。notes_policy 必须为 clear，来源凭据保存在私有输入快照与 build，旧成品不修改。

manifest schema_version=1，含 project_id、title、report_date（ISO日期）、revision（字符串）、mode（draft 或 replay）、整份 review、photos、slides 和 evidence_files。已确认字段同时需要 status=confirmed 和具体 evidence；不能把“文件收到”当作岗位、项目或实物对应已经确认。

每张 photo 保存唯一 photo_id、path、sha256、size、project_id、source_id、review 与材料 quality。captured_date 可为 null：未知时间保持未知，不用于工时推断。日期、已接受岗位标签和其依据可单列保存，不辨认身份，不做跨图同人关联。

slides 支持 cover、section、overview、items、groups，template 指向原模板的显示页序。items 逐项保存原生表格 row、原名 name 与照片 placement；groups 保存已有 label、status、evidence 及照片。placement 指向 photo_id 并保留已确认 caption/review。同一照片可用于多个有依据的项目；同一项内不重复计算。空的合并续行不另造名称。缺图保持空位或明确无对应照片。

values 可补充字符串文案，不覆盖 project/date/revision 等计算字段或结算字段。已确认历史旧格式必须显式转为此清单，并留存原路径与 SHA；不能隐式猜日期、岗位和项目。

## 生成与检查

程序冻结输入、运行环境与执行代码指纹，使用文件锁建立 attempt。执行顺序为 marker → Artifact Tool 编辑 → 严格原图映射准备 → PowerPoint 原生 compose及保存后内容核验 → 正式 finalizer → 原生 verify → HTML。导出库未保存图片标记时，程序先核对新增图片的顺序和逐处原字节，再清除旧占位并保存映射；只合并字节完全相同的媒体，不改变任何图像像素。准备和保存后核验回执进入不可变artifacts。仅真实且结构化的子进程 EBUSY 允许程序启动完整等价检查链；其他错误停止。回执如实区分 finalizer_returned 与 fallback_used。

原生表格保留，模板规格和数量标为参考，实际验收栏留空。照片原字节按显示页与对应次数检查，原表行名/数据不变，备注为空。物品布局按实际照片数量选列，可复用同图；没有每页六张上限。人员按已确认分组排版。

工具保留调用前已打开的 Office 文档；不调用 Quit、kill 或修改执行策略。运行路径若已被用户打开则拒绝接管，不关闭用户窗口排障。

成功生成只到 native_verified。逐页查看原生 PNG、核对可编辑文字/表格、配图与缺图后，实际审阅者才调用 review 登记 complete。HTML 依赖 pages 目录，移动时连同 output 一起保留。未实际浏览器加载时不能宣称该项验收成功。

相同输入、代码和 runtime 的 native_verified/complete 运行校验既有输入及产物哈希后复用；不重新渲染。失败留存旧 attempt，再次运行新建 attempt。原件或已记录产物改变时拒绝复用。

本入口不发送报告、不启动调度、不推断人员身份。当前机器验证和私有交接状态以 TASK.md 及其链接证据为准。
