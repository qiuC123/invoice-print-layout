# 工作区事件推送

事件推送默认关闭。仅在当前工作区的 `工作台/project-events.json` 中明确配置本机 HTTP `url` 后开启；不读取其他工作区配置。

正式工作区上线前，由维护者创建 `E:/devlop/invoice-print-layout/workspace/工作台/project-events.json`：

```json
{"url": "http://127.0.0.1:25820/events"}
```

`GET /api/runtime` 的 `event_push` 实时显示 `已开启`、`未配置` 或 `配置无效`，修改或丢失配置无需重启即可看到。已开启代表有效配置，不代表接收服务当前可达。

未配置或无效配置下，新事件以 `disabled` 留存本地去重和限频历史，不进入待发送队列。派发时发现配置缺失，会将旧 `pending` 事件标为 `disabled`，`error=not_configured`。之后开启推送不会补发这些停用事件；启用后的新事件仍使用原来的发送、重试和去重逻辑。有效配置中的地址仍限于本机 HTTP，禁止凭据和重定向。

## 2026-11-16 泄漏入口核查

`tests/organize_nonblocking_browser.cjs` 以独立 Python 进程运行 `tests.organize_nonblocking_browser_server`。夹具在“合成整理验收项目”和“合成断连整理项目”各生成两个 `invoice.needs_attention`：商品分类疑点和材料关系疑点，共四条；随后 `make_server()` 启动事件派发线程。旧 `event_url()` 未配置也返回正式接收地址，因而可将这些事件送入正式系统。

该进程不加载 pytest conftest。原 conftest 本身也只把发送替换为会被派发器捕获的 `ConnectionError`，不能使测试失败。提交 026870b 的变更是派发恢复 pytest；cbc6798 及当时浏览器整套验证涉及此独立夹具。新夹具实跑的事件数量、项目数量与题述两组泄漏特征吻合；现有证据未保留历史临时目录到 ID 的映射，尚未把历史随机 workspace/project ID 与旧目录逐条对应，不能仅凭提交时间认定某次运行。

现在 pytest 和此独立夹具共用 socket 防线，在连接前阻止 25820；记录尝试，结束时即使发送异常被捕获也会使验证失败。浏览器驱动检查夹具退出码；夹具还断言四条疑点、两个项目、全部事件停用且无真实端口连接尝试。其他独立临时工作区由产品默认关闭推送保护。

本地只运行定向测试和两个浏览器契约，完整 pytest 留给 CI，不操作现有工作台、不写正式配置、不清理正式系统已有卡片。

## 本次本地验证

- 共享 venv 的解释器配合本 worktree 的 `PYTHONPATH=src`；已确认实际导入来源。
- 六个定向文件最终 136 passed：project_events、invoice_attention、workbench_runtime、organize_nonblocking、organize_recovery、agent_dispatch_recovery。
- 两个浏览器契约 organize_nonblocking_browser 和 organize_fixture_lifecycle 通过，包含四条疑点停用、连接防线、子进程失败传播断言。
- mypy：123 个源文件通过；git diff --check 通过。
- 较早运行出现一次派发恢复失败（49 passed / 1 failed，failed != ready），单文件复跑 11 passed，最终合跑 136 passed；未修改该测试或放宽断言。
- 最终合跑首次因未预建 basetemp 父目录出现 setup errors，补建后得到上述 136 passed；不属于产品回归。
- 未运行全量 pytest 或完整 OCR/Excel 验收；未修改正式工作区或发送真实端口请求。
