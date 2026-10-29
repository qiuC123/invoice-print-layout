# 发票工作台

在自己电脑上把一堆发票、付款截图、行程单整理成可直接打印报销的材料包。

出差和项目报销最费时间的往往不是填表，而是：票据散落在邮箱、微信和手机相册里；发票和付款记录对不上；同一笔钱重复报；打印时版面乱、二维码糊。发票工作台把这些步骤放在一个本机网页里完成：收材料 → 自动识别与配对 → 人工核对 → 一键生成报销包。数据默认只在本机处理，`workspace/` 下的账本和原件不会进入仓库。

> 运行环境：Windows + Python 3.12，浏览器打开 `http://127.0.0.1:8765/`。双击 `打开发票助手.cmd` 即可启动，优先打开无地址栏的 Edge 应用窗口；没有 Edge 时使用默认浏览器。启动脚本加 `-BrowserTab` 可强制普通浏览器标签页，`-NoBrowser` 仍只启动服务。

## 主要功能

**收集材料**
- 先选项目，再在材料合成台点“＋ 收材料”选文件、点“导入文件夹”，或直接把文件／文件夹拖到主页面或合成台（包含子文件夹）。只收 PDF、PNG、JPG、JPEG、WebP，每份最多 20MB；其他格式和过大文件跳过并汇总，超过 200 个文件整批不上传，请分批。已收过的原件不重复导入，原件先保存再识别。
- 导入文件夹时，浏览器会询问“要上传这些文件吗”，点上传即可；文件交给本机发票助手。开启“上传后自动整理”时，新增材料自动按每批最多 20 份交给助手，前一批结束后继续下一批；切换项目或关闭页面后，回到原项目继续待启动批次。
- 二维码／链接领票：二维码图片可随材料导入，领票链接在材料合成台的同名入口粘贴。
- 可选 163 邮箱自动收件（可随时暂停），以及按费用回查历史邮件中的发票附件和下载链接。
- 手机连接：在电脑上主动开启后，手机浏览器扫码即可上传照片和文件、核对同一本账（同一 Wi-Fi，或经 Tailscale 私人异地访问）。

**本地识别**
- 本机完成 PDF 文字提取、图片 OCR 和发票二维码解析，读出发票号码、金额、日期、商家和商品明细。
- 内置铁路 12306、网约车（滴滴、曹操、享道等）、酒店、外卖、快递和电商订单等常见版式规则；高铁票按出发日期计入。
- 按票号、交易号和原件摘要查重，避免同一笔钱报两次。

**自动配对与整理**
- 材料合成台把发票与付款截图、行程单或订单明细配成一笔费用，并校验金额守恒；支持多票对一笔付款、一票对多笔付款。
- 自动整理 Agent 优先使用本地规则；遇到复杂情况才交给 AI 兜底判断，结果只生成“待核对”草稿，不会替你核对或提交。
- 证据不足或存在多种合理组合时，保留给人工确认，不猜测。

**核对与管理**
- 按项目分账；费用分为待核对、已核对、已删除三个列表，支持按类别、人员、问题多条件筛选。
- 项目可设置并随时修改开始/结束日期，自动收件按项目名称和票据日期分配；时间重叠或归属不明时留待人工分配。
- 费用归属人可单笔或批量修改；批量核对、批量退回。已核对的费用会锁定，要修改需先退回待核对。
- 分类先用固定规则和你确认过的经验，可选接入 Jev 分类服务；报销名称根据商品自动生成（例如“鼠标采购”），人工修改优先。

**报销包**
- 单笔和整批都能先看最终打印预览。
- 生成矢量 PDF（A4、1×1 打印，文字可复制，二维码保持清晰），附带 Markdown 明细和公司模板 Excel。

**设置**
- 浅色/深色界面。
- AI 模型配置：可选 WorkBuddy 本机网关、智谱官方、DeepSeek 官方或任意 OpenAI 兼容接口；可在页面切换模型、填写 API Key、测试连接。密钥只保存在本机工作区，页面只显示末 4 位。AI 默认关闭，不开启也能完整使用。
- 自动整理：统一设置上传后自动整理和复杂问题使用模型的偏好，默认开启；没有可用模型时只用本地规则，结果仍需人工核对。
- 收件邮箱：配置163邮箱、客户端授权码、自动收件间隔与匹配不到项目时的归入项目，授权码保存在Windows凭据管理器；可测试连接和立即收件。

日常操作见 [操作指南](操作指南.md)，工作台细节见 [工作台说明](docs/workbench.md)，当前能力与未完成部分见[项目状态报告](docs/project-status-2026-09-29.md)。

## 同仓库的其他工具

- **打印助手 CLI**：从 163 邮箱或本地待处理目录接收票据，整理网约车凭证，按一列两行生成 A4 打印包。打印选择 **A4、1×1**，不要再次二合一。
- **后勤扩展**：项目待办、现场照片归档、卸货日报等，见下文。

本 README 后续部分主要用于安装、配置和维护。

## 打印助手 CLI 范围

- 已按真实样例验证可提取文字的滴滴、曹操、享道 PDF；其他平台按通用结构证据判断，证据不足才进入“需要检查”。
- 尚未命名的平台只要具有完整行程单结构、金额一致且能唯一配对，也会以“网约车”名称自动处理，并在费用清单中标记“新平台待命名”。
- 每个网约车票据组包含一份电子发票和一份可含多页的行程单。
- PDF 识别、图片 OCR 和拼版在本机完成；可选 Jev 费用分类会在点击判断时发送筛选后的商品文字和用途，不上传原始票据。
- 可选的 163 邮箱导入只下载相关 PDF，不删除、移动或标记邮件已读。
- 第一阶段从 Python 源码运行，不提供 EXE 或安装包。

## 后勤扩展

- **项目待办与辅助消息**：`/logistics` 按项目管理来源群、临时待办和定时照片任务。普通聊天收件器尚未接通常驻采集，消息区用于照片和报销辅助资料。[项目与收件箱](docs/logistics-projects.md)
- **现场照片**：上传、递归读取文件夹、按指定群和日期读取微信本机缓存；本地OCR与可选Jev文字分类后按项目归档，区分原图、普通缓存和待核对记录。微信读取器需另行配置。[照片管理](docs/photo-management.md)
- **定时照片读取**：后端按项目定时读取指定微信群的本机缓存，结果在工作台查看和核对。需要电脑、微信和服务运行。[照片管理](docs/photo-management.md#每日定时读取)
- **固定模板卸货日报**：工作台手动匹配工厂/现场车头照、替换允许的日期和工时字段，使用本机PowerPoint检查并提供HTML预览与可编辑PPTX。通用施工日报、结算报告和人员考勤尚未形成自动闭环。[日报说明](docs/daily-reports.md)
- **查找邮箱发票**：按事项回查历史邮件，提取附件、受支持的淘宝与发票通下载链接；预览后手动关联原事项。[邮箱查找](docs/mail-invoice-search.md)
- **费用分类建议**：固定规则优先，模糊商品文字可交给 Jev；采用后仍须保存、核对，不自动提交报销。[分类说明](docs/expense-classification.md)
- **住宿**：保留本地住宿字段校验、草稿服务及房间容量预览逻辑，尚无独立页面或 CLI 入口；住宿报销可在工作台按酒店费用整理。[住宿说明](docs/lodging-usage.md)
- **报销包**：生成总PDF、MD明细及公司模板XLSX。Excel依赖 Node.js、`@oai/artifact-tool` 和自行提供的模板，详见[工作台说明](docs/workbench.md)；仅安装Python依赖不能直接生成Excel。

工作台默认仅监听本机回环地址；手机连接需在电脑上主动开启，只在所选私有网络地址监听（见[手机连接](docs/phone-lan.md)），不提供公网部署。邮件、Jev均需自行配置；不开启这些集成也可本地登记和整理。

## 开发环境

需要 Windows 和 Python 3.12。克隆仓库并进入目录：

```powershell
git clone https://github.com/qiuC123/invoice-print-layout.git
cd invoice-print-layout
```

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

安装后双击 `打开工作台.cmd`，或运行：

```powershell
.\.venv\Scripts\python.exe -m invoice_print_layout.workbench_web --workspace .\workspace
```

浏览器打开 `http://127.0.0.1:8765/`。不配置邮箱也能手动登记、上传凭证。识别结果需要核对；目前优先支持淘宝闪购订单截图，其他版式可能需手动填写。

## 公开仓库与本地数据

本仓库仅包含代码、说明和虚构测试数据，不包含真实发票、订单截图、报销台账、个人配置、授权码或历史工作区。克隆后可按需配置收件邮箱，数据在本机 `workspace/` 下生成。

验证代码：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy src/invoice_print_layout
```

## 使用

首次配置 163 邮箱（授权码隐藏输入并保存到 Windows 凭据管理器）：

```powershell
.\.venv\Scripts\invoice-print-layout.exe mail setup --workspace .\workspace
```

只读检索最近 30 天邮件，将发票和行程 PDF 下载到 `待处理`：

```powershell
.\.venv\Scripts\invoice-print-layout.exe mail import --workspace .\workspace --days 30
```

生成打印包：

```powershell
.\.venv\Scripts\invoice-print-layout.exe process --workspace .\workspace
```

首次处理会创建工作区，并提示输入用于输出文件名的姓名。也可以不用邮箱功能，直接把 PDF 放入 `workspace\待处理`。

输出名称示例：

```text
2026-01-15_滴滴_123.45元_张三.pdf
2026-09-03_曹操_52.14元_张三.pdf
```

处理规则和边界见 [实现方案](docs/implementation-plan.md)，项目术语见 [CONTEXT.md](CONTEXT.md)。

## 验证

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy src
```

安装开发依赖后也可运行 `python -m pytest -n auto`（需要 `pytest-xdist`）；串行跑法保持有效。
浏览器测试统一入口会按顺序执行全部 `tests/*.cjs`，失败后继续其他文件，最后返回非零退出码：

```powershell
node scripts/run-browser-tests.mjs --list
node scripts/run-browser-tests.mjs
```

Playwright 按 Node 标准模块规则解析：优先项目本地依赖，也可把已有 Playwright 所在的 `node_modules` 绝对路径放入 `NODE_PATH`。
可用 `node -p "require.resolve('playwright')"` 检查解析位置。入口不会自动安装依赖或启动正式服务。 包含 Python HTTP 夹具的用例优先使用项目 `.venv`，也可通过 `PHOTO_TEST_PYTHON` 显式指定。
默认日志保存在忽略的 `workspace/browser-tests/`；可通过 `--output=目录` 指定，单文件超时默认180秒，可用 `BROWSER_TEST_TIMEOUT_MS` 调整。

真实票据、生成结果和工作区均被 `.gitignore` 排除，不能提交到仓库。
