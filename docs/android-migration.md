# Android 独立版迁移

当前阶段：2026-10-17 Android 0.2.0-dev，剩余工作流补齐及模拟器验收。电脑版既有账本、运行服务和原件继续保留。当前状态入口为 `workspace/invoice-operations/TASK.md`，业务约束沿用根 AGENTS.md。

## 已选择的实现方式

- `android/` 为独立 Android 工程；Java 原生层负责 SQLite、Storage Access Framework 文件导入/导出、本地 OCR、PDF 处理；随包 HTML/CSS/JS 只负责手机界面。
- 输入体验修复（2026-10-18）：专用模拟器启动时关闭自动手写浮栏、保留普通软键盘；表单按可用窗口高度滚动，确认按钮固定在可见区。错误提示放在弹窗内部，可被看到和读取；无关联材料的既有费用仍可编辑，新增空白费用仍需材料。
- 手机设置使用底部独立“设置”页，按账本与报销、收件与识别、数据与外观分组；子功能关闭/取消/系统返回先回设置页，再返回原工作页，并恢复原费用阶段与勾选。对应回归：`android/tests/settings.browser.cjs`（Playwright + 合成 Native 桥，无真实数据/账号）。
- App 直接读取自己的设备私有账本和原件目录，不调用 Windows 后台、不使用模拟器到宿主机的转发地址；界面没有远端网址或服务器依赖。
- 随包安装 ML Kit 中文识别和二维码模型，不依赖下载模型。0.2 增加联网权限供可选服务使用；GLM/Jev/163 默认关闭，凭据须由用户在手机填写，用 Android Keystore 加密。电脑密钥不会进入 APK 或备份。
- 本地文字和规则优先；只有用户启用 GLM 且关键字段不足时才请求文字字段兜底，也可主动重试。图片读取另有独立开关，默认关闭；开启后才可上传这次选中材料的有限图片（最多4页、每页5MB以内）作为视觉兜底。结果必须有转录原文证据，冲突进入人工确认；人工字段/用途不被覆盖。
- Jev 单独提供商品/用途的分类建议；先查明确规则和人工经验，再按需调用。去除带身份/编号标签的文字，低置信度保留“其他”；用户点“采用此分类”并保存后才改变费用。
- PDFBox Android 复制源 PDF 页面对象，保留可提取文字。短 PDF 可在同一报销子项内按原字号合排；只有实际减少页数才采用。不同类别/子项不混页，不自动裁掉不确定的空白或装饰；整页材料保持完整，图片等比放入 A4。明确选择“仅付款凭证”时使用电脑版同样的四张一页布局，PDF 仍是矢量，原始文件保持不变。
- Noto Sans SC 用于打印明细的可提取中文；来源 Google Fonts，SIL OFL 许可随源保留。字体由构建脚本下载到忽略目录。

## 第一阶段流程与边界

项目创建/选择 → 图片或 PDF 导入 → 本地识别预填 → 手工确认材料用途与组合 → 编辑金额/类别/人员/日期 → 核对通过 → 本机生成 PDF → 用户另存或打开系统分享面板。

原件按内容 SHA256 在项目内防重；同份材料不能关联两笔费用；付款截图和发票不会相加。金额采用整数分，不接受四舍五入、科学计数或未知金额直接核对。材料缺项只允许在必填字段完整时，经独立确认后核对，并保留缺项事实。项目“已报销/未报销”是独立人工标签。

0.2 的工作流为：材料收件 → 字段/用途编辑 → 唯一明确自动组合或手工多材料组合 → 待核对 → 核对通过 → 选择费用生成 Excel 与最终 PDF → 查看预览 → 确认提交。提交记录独立于项目人工报销标签，也不表示财务已经收到文件。

## 0.2 功能入口与约束

| 功能 | 手机实现与验证方式 |
| --- | --- |
| 项目管理 | 新建、选择、改名、置顶、回收与恢复；“未报销/已报销”独立保存 |
| 人员管理 | 项目内稳定 ID、改名、停用/恢复、合并、人员费用明细；多人分摊须精确等于费用金额 |
| 收件 | 系统文件多选、Android 分享、系统相机；复制原件并按项目 SHA256 防重 |
| 材料整理 | 用途、金额、商家、平台、店铺、渠道、消费日期和编号可编辑；归档、删除、恢复、本地重新识别及 GLM 补充 |
| 自动配对 | 店铺/平台/金额的唯一对应；支持一对一及有唯一整组金额对应的一对多/多对一；歧义和其他组合保留人工选择，不自动核对 |
| 费用处理 | 搜索、人员/类别/问题多选、全选后单独取消、批量核对/退回/归属/子项/移动项目/回收恢复、保存核对并下一笔 |
| 公司报销表 | 兼容公开固定 A1:M36 子项合同的 XLSX 模板；可从手机导入，保留模板条目/样式，追加报销明细；长编号和等号开头内容按文本写入 |
| 最终预览 | 主表、明细、原件 PDF 与 Excel 共用费用快照及排序；本地绘制单元格、网格、合并区域和行列尺寸，不依赖 Windows Excel |
| 提交 | 锁定费用版本、原件及模板校验值；变化拦截；事务提交、防重复、保留历史快照；明确退回后才可改费用 |
| 备份恢复 | 带校验清单的 ZIP，包括项目/人员/原件/费用/报销包/模板/人工分类经验/操作记录；排除联网凭据；先预检、确认后合并，冲突整批停止 |
| 163 邮件 | 用户启用后按日期/主题只读 IMAP 收件，使用授权码；最多31天、200封、50份 PDF/图片附件，使用 PEEK，不自动改邮件状态 |
| 二维码 | 随包模型本地读取；上海/江西税务既有官方入口可尝试领取，校验 PDF 对应票号；其余入口提示人工领取，不把二维码当发票 |
| 明暗主题 | 手机界面切换并在重开后保留 |

Windows 微信桌面采集由手机系统分享导入替代；打印交付使用文件另存/系统分享，不绑定电脑或打印驱动。真实邮箱/税务/云模型账号尚未接入，本轮只用合成材料及模拟服务验证适配器，不能把它们称为真实账号端到端成功。真机兼容性尚待实际 Android 手机验证。

## 从电脑版迁移

`scripts/export-android-migration.py` 仅在显式指定项目与输出路径后，GET 读取本机独立发票工作台的现行读视图及原件，生成同格式迁移 ZIP。保留整数分、人员 ID、手工项目子项、核对/提交/删除状态；不修改电脑版，不携带 API token、AI/邮箱凭据。不自动导出真实账本。

```powershell
./.venv/Scripts/python.exe scripts/export-android-migration.py `
  --project-id '<明确选择的项目 ID>' --output '<私有目录>/android-migration.zip'
```

之后在手机“管理与收件 → 备份与恢复”选择 ZIP，查看预检计数后恢复。手机使用和后续备份无需连接电脑版。历史原始工作台仍保留，不用此格式替代电脑版完整历史归档。

## 开发构建

已验证的工具组合：Android SDK API 35 / Build Tools 35.0.0、AGP 8.9.2、Gradle 8.11.1、JDK 21；最低 Android 8.0（API 26）。仅产生 debug APK，不创建发布签名或商店发布配置。

```powershell
./android/build-debug.ps1 -SdkRoot E:/Tools/Android/sdk `
  -JavaHome 'E:/Tools/Android/jdk21/jdk-21.0.12.1+1' `
  -GradleHome E:/Tools/Android/gradle/gradle-8.11.1 -ConnectedTests
```

输出 `android/app/build/outputs/apk/debug/app-debug.apk`，调试包名 `cn.qiuc.invoice.debug`。源码不读取私有工作台或模板；测试仅用合成材料。完整电脑版 pytest 和 mypy 仍按项目要求执行。

当前实际验收设备为 Android 15 模拟器；API 26 是声明的最低版本，其余安卓版本与真实手机尚未验收。运行 `scripts/start-invoice-emulator.ps1` 可恢复本机专用模拟器，不清空其数据。

首阶段证据保留在 `workspace/android-migration-20261017/verification.json`。0.2 证据为 `workspace/android-completion-20261017/`：Android 单元 8 项、设备 32 项和迁移导出 3 项通过；电脑版 pytest 3283 通过、mypy 81 文件通过。Mobile MCP 实操覆盖预览/提交/退回/人员/筛选/批量/合成迁移包恢复/拍照/备份另存/主题；最终 Excel 使用独立读取器验收，源 PDF 页另做文字和渲染像素对比。以该目录最终 `verification.json` 为验收收据。

## Mobile MCP

本机安装固定版 `@mobilenext/mobile-mcp@1.0.11`，位置为用户 `.codex/tools/mobile-mcp`，Codex 用户配置仅新增 `mobile-mcp`。使用既有 `E:/Tools/Android/sdk`，关闭匿名遥测，使用安卓本地驱动。没有连接云设备。

专用模拟器名 `Invoice_Assistant_API35`，ADB 标识 `emulator-5560`。旧 `Logistics_API35` / `Logistics_API30` 不改动。当前对话通过真实 MCP stdio 客户端调用已安装服务；新的工具配置在客户端重新载入时提供给新会话。

```powershell
node scripts/mobile-mcp-call.mjs mobile_list_available_devices '{}'
node scripts/mobile-mcp-call.mjs mobile_take_screenshot '{"device":"emulator-5560"}' workspace/android-migration-20261017/screen.png
```

`scripts/mobile-mcp-call.mjs` 为本地验收入口，截图保存在指定私有证据目录。`MOBILE_MCP_ROOT`、`ANDROID_HOME`、`ANDROID_AVD_HOME` 可覆盖本机默认路径。

## 参考

- [Mobile MCP](https://github.com/mobile-next/mobile-mcp)
- [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Android AGP 8.9 兼容性](https://developer.android.com/build/releases/agp-8-9-0-release-notes)
- [ML Kit 中文识别与随包模型](https://developers.google.com/ml-kit/vision/text-recognition/v2/android)
- [PDFBox Android](https://github.com/TomRoush/PdfBox-Android)
