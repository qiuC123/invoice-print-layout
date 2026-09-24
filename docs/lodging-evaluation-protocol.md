# 住宿意图小样本验证协议

2026-09-22。只评估意图选择，不评估任意实体抽取、真实写表、多人权限或生产登记正确率。

- 数据：`tests/fixtures/lodging_intents.json`，50条完全合成中文样例，20条开发示例、30条保留测试示例。酒店/人员/记录号均为虚构，无真实票据、住宿台账或截图上传。
- 意图：试算、新开、续住、修改已存记录、查询、撤销草稿、补充草稿、需澄清、其他。明确要求新开但缺日期仍标为新开意图，执行资格由未来的字段校验负责。
- 基线：冻结的关键词规则，固定顺序、遇到无法识别时输出clarify。它是最小保守基线，不代表纯脚本能达到的最好水平。不会在看测试结果后针对样例调规则再报告原始成绩。
- Jev：官方 `https://api.typesafe.ai/v1/systemone`；请求固定 `jev-1.13.0`，保存返回的实际模型名。仅发送消息、合成本人草稿上下文、固定消息时间和选项；绝不发送预期答案或开发/测试划分。
- 提示和数据在首次实测前固定，以SHA-256记录。样例和标签由同一作者制作，因此这是探索性保留测试，不是独立盲测，也不能证明泛化到所有中文。
- 串行最多50次，每次超时30秒；首次错误停止，不自动重试。不创建API密钥或购买额度。凭据从用户指定的本机env文件加载，不打印、不上传到模型状态。
- 指标：总正确率、测试集正确率、误判为写入意图的样例、0.8置信阈值下覆盖与误判、请求往返p50/p95、返回的输入token和按公开费率估算的美元成本。收费账单不可用时不得称为实际扣费。
- 0.8为预设观察阈值，未经校准；confidence是分布集中程度，不是实际正确率。关键词规则没有模型置信度，不能对它使用此阈值作可靠性声明。
- 写入意图误判定义：期望不属于create/extend/amend，但模型或规则判为这些之一。它是语义误判指标，不是实际写表事故；此次没有任何写表工具。
- 即使全对，最多支持进入受控联调。出现高置信错误时，检查错误类别、保留澄清/人工处理，不直接允许该类自动写表。

复现（从项目根目录，用新输出目录防止覆盖证据）：

```powershell
.\.venv\Scripts\python.exe scripts/lodging_eval.py --output workspace/lodging-eval/offline-new
.\.venv\Scripts\python.exe scripts/lodging_eval.py --live --output workspace/lodging-eval/live-new
```

官方资料：[快速开始](https://docs.typesafe.ai/introduction/quickstart)、[Choice](https://docs.typesafe.ai/primitives/choice)、[Confidence](https://docs.typesafe.ai/confidence)、[公开定价](https://typesafe.ai/blog/introducing-system-one-models-and-jev)。此次每请求一个意图问题，后续多问题或长上下文需要重新测费用与延迟。
