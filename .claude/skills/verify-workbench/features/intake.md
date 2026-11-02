# 收材料

用户从材料合成台选择文件或整个文件夹，也可把文件、目录拖进主页面或合成台。原件先保存，支持的材料进入整理，重复原件不重复生成费用。

## 子功能

- `F2-upload-files`：`＋ 收材料` 上传 7 组 PDF/PNG，共 14 份。
- `F2-folder`：导入包含子目录的文件夹，收 2 份，跳过 1 份 TXT。
- `F2-drop-files-desk` / `F2-drop-folder-main`：合成台拖文件、主页面拖文件夹，各新增 1 笔。
- `F2-drop-folder-desk` / `F2-drop-files-main`：反向入口重复上传相同原件，费用维持 10 笔。

## 用户怎么进入

- 选项目→`材料合成台`→`＋ 收材料` 或 `导入文件夹`。
- 向主页面拖文件/文件夹；或者先打开材料合成台再拖入。四种拖放组合均分别执行。
- 较旧的收件区入口、邮件、二维码链接和手机连接不在本次范围，不能因合成台通过而宣称这些入口通过。
- F22 `手机连接 → 收票 App` 的地址和配对状态独立从 `/api/phone-link/receipt-app` 读取，不应被慢的旧手机状态阻塞。`tests/receipt_devices_browser.cjs` 覆盖模拟响应下的配对、撤销和失败恢复；本技能没有实测 Android 真机上传。

## 用 Playwright 操作

前提：`F1` 创建并选中甲，自动整理助手加载完毕。完整脚本 `verify.cjs run <新名称>` 的 `F2-*` 段按下列顺序执行。

- 点按钮 `材料合成台`，等 `#matchingDeskDialog` → `page.waitForEvent('filechooser')` 与点击 `＋ 收材料` 配对，`chooser.setFiles()` 接受 01–07 组原件 → 等真实台账出现 7 笔草稿，留上传中和整理后证据。
- 点 `导入文件夹`，同样接受 `materials/folder/` → `#notice` 的上传汇总有 `跳过 1`，子目录中的第 08 组进入账本。汇总可能被整理完成提示替换，要在任务完成前留证据。
- 在 `#matchingDeskDialog` 落点用 CDP `Input.dispatchDragEvent` 依次发送 `dragEnter/dragOver/drop`，`data.files` 为第 09 组两个绝对路径 → 9 笔。
- 点 `#matchingClose`，对 `main` 发送真实 `materials/drag-folder/` 路径 → 递归收第 10 组，10 笔。
- 再对合成台拖同一目录、对主页面拖第 09 组 → 各自提示重复/已收过，数量不增加。每种入口各留截图、ARIA 和服务状态。

## 坑

- 原生文件窗口及“要上传这些文件吗”确认窗：**确认进不去（当前 Playwright 脚本）**。已试可见按钮触发 `waitForEvent('filechooser')`、`chooser.setFiles(directory)`，只能向浏览器交付目录，原生窗口不在 page DOM；文件夹递归上传通过不代表窗口交互经过检查。
- 不用 `page.evaluate` 伪造 `webkitGetAsEntry` 证明目录可拖入。CDP 真实目录拖放若不支持，记“确认进不去”，保存 CDP 错误和尝试；不能用文件选择器的成功顶替。
- 上传后下一入口先确认合成台状态，必要时通过可见按钮重新打开；自动整理面板正常完成时隐藏，仅异常或待人工处理时显示。
- 自动整理模块未加载时，上传可能只识别出材料而不创建整理任务；先做页面体检，保留网络失败，不直接写入费用补前提。
