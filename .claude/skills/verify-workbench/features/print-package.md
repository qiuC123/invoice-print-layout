# 打印预览与报销包

用户点击费用可看单笔最终打印页，也可以选中多笔打开整批报销预览，最后制作 PDF、MD 和 Excel。PDF 中发票文字应可提取，页数应与材料及汇总表对应。

## 子功能

- `F5-single-vector-pdf`：单笔真实打印预览、下载并检查 1 页 PDF。
- `F5-batch-preview`：10 笔加汇总表的整批打印预览，检查本样本的 11 页 PDF；实际包含在 `F5-batch-package` 检查段。
- `F5-batch-package`：制作并保存 PDF＋MD＋Excel；总 PDF 再做文字与页数检查。

## 用户怎么进入

- 待核对或已核对列表选费用→右侧最终打印页→`放大／下载打印 PDF`；本轮下载检查在已核对阶段执行。
- 顶部 `报销预览`（有勾选则按勾选，否则按当前待提交范围）；自动整理助手也有本次整理结果预览入口，本轮未执行助手预览。
- 已核对列表全选→`制作报销包`→上传合成模板→`开始制作 PDF＋MD＋Excel`。

## 用 Playwright 操作

前提：F4 全部已核对；Windows 原生 Excel COM、Node.js 和 README 所述 `@oai/artifact-tool` 运行环境可用。模板是 `materials/synthetic-template.xlsx`，无需真实公司资料。单笔预览不依赖 Excel；整批预览和制作都依赖 Excel COM，CI 不运行这两步原生导出。

- 选第一笔，等 `#expensePrintPdf` 可见 → 下载它的真实 `/export/` 文件为 `F5-single.pdf`；调用 `fixture.py pdf` 断言 1 页及该发票的虚构票号文本（`99000000000000000001` 至 `99000000000000000010`）。
- 勾 `全选当前列表`，点 `#exportSelected`，等 `#reportDialog[open]` 可见后在 `#reportTemplateFile` 接受合成模板，等 `#createReport:enabled` 后关闭制作窗。
- 点 `#previewReport` → 等真实 `/api/report-preview` 响应成功、`#previewFrame` 内 `.print-page img` 出现，保存整批预览截图；下载响应给出的真实 PDF。
- `fixture.py pdf <文件> 11 <10个合成标记>` → 页数固定 11、所有发票标记可提取、有 PDF 字体；保存 `.inspection.json` 和第一页 PNG。页数与文本都不能直接相信服务响应。
- 关闭 `#closePreview`，重新点制作报销包、`#createReport` → `#reportDownloads` 出现 `下载总 PDF / 下载 MD 清单 / 下载 Excel 报销表`；逐个保存，重复检查总 PDF 的 11 页与文本。

## 坑

- 公司模板和 Excel 运行环境是整批预览的真实前提。缺少时记环境失败，不能 mock `create_excel` 或把单笔预览当整批成功。
- 制作窗先异步读取状态再显示，必须等待窗口可见才选模板；对隐藏输入提前 `setInputFiles` 会与打开窗口的状态读取/表单重置竞态，无法代表用户操作。
- 原生 Excel 在复跑中偶发返回 `RPC_E_CALL_REJECTED`，预览和制包都可能遇到。脚本保存真实响应和失败截图；已观察到同一组合先成功、后失败，稳定性问题需产品侧处理，不能用 mock 或反复重跑把当前失败改写为通过。
- 不往票面增加 `SYNTHETIC-*` 等任意页尾文字来做标记；产品会保守地视为未提取商品，检查文字时使用本来的虚构票号。
- 模板仅是符合单元格契约的虚构 XLSX；视觉效果不代表真实公司模板。未使用真实模板。
- 矢量检查证明合成发票文字保留；付款 PNG 仍然是图片。没有检查 QR 可扫性或打印机驱动/纸张输出。
- 未检查：未勾选时按筛选范围预览、自动整理助手预览入口、多种费用排版、移动端、物理打印。
