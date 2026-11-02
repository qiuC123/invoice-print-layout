---
name: verify-workbench
description: 验证发票工作台的真实本机网页：隔离启动 Python 发票助手，用 Playwright 操作合成票据的项目、收材料、自动整理配对、核对与矢量 PDF 报销包；功能变更后或验收时使用。
---

# 发票工作台验证

从仓库根目录运行。先读 [功能地图](features/README.md)，按功能文件判断需要的前提和未检查的入口。只操作本技能创建的临时实例。

## 启动

Windows、项目 `.venv`、Node、Playwright 和对应版本的 Chromium 是前提；完整流程还需要原生 Excel COM 与 `@oai/artifact-tool`。不自动安装依赖；浏览器开发依赖可先用仓库根目录 `npm ci`、`npx playwright install chromium` 准备。工作树共享 `.venv` 时，本脚本仍把 `PYTHONPATH` 固定为当前工作树 `src`。

本机已确认的 Playwright 位置如下；换机器时按 README 的 Node 标准模块解析方式设置 `NODE_PATH`，先用 `require.resolve` 检查。

```powershell
$env:NODE_PATH = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
node -p "require.resolve('playwright')"
node -e "const p=require('playwright'); console.log(p.chromium.executablePath()); console.log(require('fs').existsSync(p.chromium.executablePath()))"
node .claude/skills/verify-workbench/scripts/verify.cjs run verify-20261029-01
```

每次改一个新的运行名称；同名目录存在时拒绝覆盖。`run` 完成启动→体检→全部操作→证据→清理，失败也清理。退出码非零就检查证据，不能把已生成截图当成通过。

需要分步排错时：

```powershell
node .claude/skills/verify-workbench/scripts/verify.cjs start verify-20261029-02
node .claude/skills/verify-workbench/scripts/verify.cjs doctor verify-20261029-02
node .claude/skills/verify-workbench/scripts/verify.cjs drive verify-20261029-02
node .claude/skills/verify-workbench/scripts/verify.cjs stop verify-20261029-02
```

`drive` 从空项目开始，不能在同一实例重复跑；失败会停止并清理，成功后还要执行 `stop`。启动用 README、`start-invoice-assistant.ps1` 最终调用的真实命令：

```text
.venv/Scripts/python.exe -m invoice_print_layout.workbench_web --workspace <本次证据目录>/data --port <系统分配的临时端口> --mode invoice
```

脚本不调用启动器的默认端口探测和旧实例关闭逻辑。启动地址、监听 PID、启动器 PID、工作区 ID、源码指纹保存在 `session.json`。Windows venv 可能由子进程监听，必须核对父子关系，不能只相信 `spawn.pid`。

从空目录生成 10 组虚构发票 PDF＋付款 PNG，金额 31–40 元，每组商家及订单号对应；素材生成沿用 `tests/test_agent_transport.py` 的小写金额定位和 `tests/test_agent_goods_optimization.py` 的商品表格方式，图片经真实本地 OCR。生成器先断言票面与商品完整，再通过上传走实际识别。合成 XLSX 仅满足现有模板单元格契约，不代表公司模板的视觉版式。只预置配置和素材，不预写项目、识别结果、配对、费用或核对状态。AI 与模型辅助均关闭，上传后自动整理开启；没有邮箱配置。AI 配置只含关闭的 offline 供应商，不引用本机 Key 文件；子进程清空 Jev 凭据环境变量。本流程不调用外部分类服务的界面入口，检查任务模型调用为零；保留正常 Windows 用户环境以支持本机 Excel。

## 体检

`doctor` 是只读检查：要求进程存在、端口实际归属本次 PID、进程创建时间未变、`/api/runtime` 的 app/mode/workspace/build/capabilities/ready/start time 正确；确认 `/api/state` 可读、AI 与邮箱关闭、自动整理为本地规则。页面没有账号登录；写入使用当前实例的本机 token。

体检通过后还要等真实界面可用：项目创建、合成台、自动整理助手、报销预览的事件绑定齐全，样式加载完毕。网页资源连接失败时先留截图、ARIA 和状态，再尝试两次普通刷新；不注入产品函数、不修改响应、不隐藏失败。仍失败就停止，报告产品页面未就绪。

## 操作

操作由 [scripts/verify.cjs](scripts/verify.cjs) 完成，全部写入来自真实 UI 的点击、文件选择器和拖放；HTTP 只用于只读交叉检查、下载文件和关闭自己的服务。没有 `route.fulfill`、测试接口或直接写数据库。

1. 创建甲乙项目，填写开始/结束日期，侧栏切换；右键甲项目改结束日期，刷新确认保存。
2. 在材料合成台点 `＋ 收材料`，通过真实 `filechooser` 上传前 7 组；等自动整理创建 7 笔草稿。
3. 点 `导入文件夹` 接受含子目录的合成目录；在合成台拖文件、主页面拖目录，凑齐 10 笔。再走合成台拖目录、主页面拖文件的去重路径。拖放通过 CDP `Input.dispatchDragEvent` 传真实本地路径，不能用伪造的 `webkitGetAsEntry` 替代目录入口。
4. 检查每笔有发票＋付款截图、金额守恒、未核对；检查真实整理任务的模型调用数为零。
5. 通过鼠标滚轮将当前行放到列表底边，先证明下一行尚未完整可见，再点 `核对并下一笔`，检查提示条出现后的最终可见性；继续逐笔核对。在已核对中退回一笔，确认后重新核对。
6. 打开单笔打印预览并下载 PDF；全选、上传合成公司模板、整批报销预览、制作并下载 PDF＋MD＋Excel。

现有选择器、可观察结果与各入口局限分别见五份功能文件。新行为超出地图时先按用户界面补地图，不改产品或旧测试来迁就脚本。

## 证据

保存在 `.runtime/verify/<运行名称>/`。仓库已忽略 `.runtime/`，先确认 `git check-ignore .runtime/verify/probe` 有输出；已忽略就不修改 `.git/info/exclude`。换仓库时若未忽略，按授权范围处理后再运行，不能提交生成文件。

- `session.json`、`doctor.json`、`server.log`、`driver.log`、`*-command.json`：启动来源、隔离地址、健康证据、命令及退出码。
- `F*.png`、`F*.aria.txt`、`F*.state.json`：操作前、处理中和保存后的可见状态；状态副本去除 token。
- `trace.zip`、`ui-posts.json`、`browser-errors.json`：可重放浏览器操作、真实写请求和响应、页面与网络失败。上传 base64 从请求记录中省略。
- `F3-jobs.json`：实际整理任务、材料关联、模型调用数。
- `F4-before-next-visibility.json`、`F4-next-visibility.json`：核对前后下一行与列表/视口边界、滚动位置和提示条状态；必须先不可完整见、再完整可见。
- `F5-*.pdf`、同名 `.inspection.json`、首屏渲染 PNG：实际导出的矢量文字、虚构票号和页数。单笔预期 1 页，10 笔加汇总页预期 11 页；这是明确断言，不能用响应中的页数自证。
- `F5-package-response.json`：原生 Excel 制包的实际响应，失败立即记录具体错误，不等待下载链接空超时。
- `originals.json`：附件字节 SHA-256 与上传前合成原件比较。
- `results.json`：逐入口结果；`cleanup.json`：进程退出、临时端口关闭、数据清理及仍存在的证据。

只接受“通过 / 失败 / 确认进不去”。失败注明地图、脚本、产品或环境原因。没有执行的路径写“未检查”，不能算通过。系统文件选择窗口、浏览器目录上传确认窗未经过桌面自动化点击，不能宣称已验证。

## 回归自检

验证技能也要证明自己能抓住错误：在另行授权的独立工作树中撤掉一个已知修复，确认同一份验证脚本会因对应行为失败，再在保留修复的工作树确认通过。记录两边 HEAD、产品 diff 和运行时 `build_id`；只有版本不同还不够，必须看到目标断言失败。不要在正常验收中自动修改产品或为了通过而回滚其他人的改动。

第一个例子是 F13：故障版本把 `verifyExpense` 的 `notify(...)` 移到 `if(advanced){ revealExpenseRow(...) }` 之后。下一行原本若完整可见，旧场景会漏报；现在要求它先在底边以外，提示条出现后仍完整可见。

故障工作树由验收方准备，用完删除。在主工作区运行：

```powershell
git worktree add --detach ../invoice-print-layout-mine main
New-Item -ItemType Junction -Path ../invoice-print-layout-mine/.venv -Target (Resolve-Path .venv)
# 在 ../invoice-print-layout-mine/src 里撤掉要测的修复（F13 见上一段），产品改动只在这个工作树里做
Copy-Item -LiteralPath .claude/skills/verify-workbench -Destination ../invoice-print-layout-mine/.claude/skills -Recurse -Force
node ../invoice-print-layout-mine/.claude/skills/verify-workbench/scripts/verify.cjs run mine-<名称>
node .claude/skills/verify-workbench/scripts/verify.cjs run fixed-<名称>
# 把故障版的证据移到主工作区的 .runtime/verify/，然后先删 .venv 联接（只删链接本身），再删工作树。
# 不先删联接，git worktree remove 可能沿联接删除共享环境里的依赖（10-30 已发生）。
(Get-Item ../invoice-print-layout-mine/.venv -Force).Delete()
git worktree remove --force ../invoice-print-layout-mine
```

10-29 的 F13 自检结果：故障版 `mine-f13-rework1-20261029`、`claude-mine2-20261029` 都只有 F4 失败；修复版 `fixed-f13-rework1-20261029`、`claude-fixed2-20261029` 都是 11 项全部通过。

每次改用未占用的运行名称。故障版应在 `F4-review-next-visible` 报 `F4-next: Next row outside visible list after verification and completion notice`，核对前几何必须先满足前提；上传失败、体检失败或前提未构成都不算抓住 F13。查看即时 `F4-after-next.png` 和前后几何，不能拿后续滚动后的截图替代。保留修复的版本必须通过相同检查。两边都要清理自己的实例并保留证据；准备故障和恢复产品代码均不属于验证脚本的职责。

## 清理

```powershell
node .claude/skills/verify-workbench/scripts/verify.cjs stop verify-20261029-02
Get-Content .runtime/verify/verify-20261029-02/cleanup.json
Get-ChildItem .runtime/verify/verify-20261029-02
```

核对当前实例身份后调用它自己的 `/api/shutdown`，等待记录的 PID 退出、临时端口关闭，再删除当前运行目录中的 `data/`、`materials/`；证据保留。身份不符时拒绝关闭和删除，不按进程名杀进程。无论成功失败，都确认 `cleanup.json` 和截图/日志还在。

不要把 `USERPROFILE` 改到空目录：本机 Excel COM 导出在该环境下实测失败，恢复正常 Windows 用户环境后可以导出；这属于验证环境差异，不能以 mock 替代原生 Excel。

禁止访问、探测、附加或重启 8765；禁止读取正式 `workspace/` 及真实材料、凭据；不修改主工作区、产品和已有测试。不提交、推送、stash 或回滚。可用 `/maintain-verification-skill` 定期检查地图与脚本是否过时。
