# 项目与项目时间

用户新建项目时可以填写开始和结束日期，通过侧栏切换各项目账本，并修改项目时间；结束日期为空表示进行中。

## 子功能

- `F1-create`：新建甲乙两个项目，分别有结束日期/进行中。
- `F1-switch`：从乙切回甲，标题和项目时间随之更新。
- `F1-period`：修改甲的结束日期，刷新后仍保存。

## 用户怎么进入

- `＋ 新建项目`：桌面侧栏和窄屏工具区复用 `#createProject`。
- 侧栏的 `合成验证甲 · 发票报销` / `合成验证乙 · 发票报销` 链接切换项目。
- 右键侧栏项目→`[data-project-action=period]`；窄屏项目操作按钮打开同一菜单。
- 尚未设置日期时，标题区 `未设置项目时间 · 设置` 按钮（`[data-set-project-period]`）也是入口；本轮自动流程未覆盖无日期按钮和窄屏菜单。
- 启动会进入上次有效的未报销项目，否则进入首个未报销项目；侧栏 `全部项目` 可回总览。对应 `tests/startup_project_browser.cjs`，不在本技能 F1 实测范围。
- 人员金额页可设置项目主负责人，作为手机收票所属人默认值；对应 `tests/project_main_person_browser.cjs`，不在本技能 F1 实测范围。

## 用 Playwright 操作

前提：空项目列表、页面体检通过。完整命令为 `node .claude/skills/verify-workbench/scripts/verify.cjs run <新名称>`，该段实现及证据编号为 `F1-projects`。

- 点 `#createProject` → 在 `#project-create [name=name/start_date/end_date]` 填 `合成验证甲 / 2026-10-01 / 2026-10-31` → 点 `创建项目`；等待新 URL 和 `#projectPeriod` 显示完整区间。
- 同入口建乙，填 `2026-11-01` 且结束日期为空 → 显示 `项目时间：2026-11-01 起，进行中`。
- 点无障碍链接 `合成验证甲 · 发票报销` → 标题回到甲，材料账本为空。
- 右键 `#projectLinks [data-project-menu-id="甲的ID"]`，点 `[data-project-action=period]` → 在 `#projectActionDialog [name=end_date]` 改为 `2026-10-30`，点 `保存项目时间`。
- 刷新网页 → 页面及只读 `/api/state` 都显示甲结束于 `2026-10-30`；留 `F1-project-switch-period.*`。

## 坑

- 同一个项目 ID 同时挂在侧栏链接和隐藏操作按钮上，选择器必须限定 `#projectLinks`，否则 strict mode 失败（脚本问题）。
- 新建后发生整页跳转；只等 URL 字符串不等页面就绪会在旧页面点击。
- 未检查：窄屏布局、无日期设置链接、日期重叠提示、清空日期及非法区间。不能用当前通过结果代表这些入口。
