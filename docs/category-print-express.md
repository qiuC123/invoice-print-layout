# 打印与快递类别升级

本次升级依据 2026-11-07 的类别决定：打印独立分类，同张打印收据顺带的小物件一起算打印；所有快递及同城配送统一使用「快递」。公开测试只用合成材料。

材料创建和复核页面的报表类型依次为「拓展、车费、生活费、人员、现场购材料、快递、打印、其他」。快递与打印是可单独选择的业务类型，所以选项为八项；现有公司模板仍保留六个一级区域，新增类型使用末尾区域中的独立费用行。明确的「图文快印服务／数码快印服务」可建议打印；「价格标签打印纸／复印纸」属于商品名称，不能截取其中的打印／复印二字当作服务。识别规则版本随修正更新，旧的商品提取缓存不再复用。

## 类别与承运商逐处判断

| 文件或用途 | 处理 |
| --- | --- |
| `workbench.py`、`expense_classifier.py`、`auto_verify.py` | 固定类别、材料规则、Jev Choice 清单和规则输出改为快递并加入打印。旧类别输入「顺丰」兼容为快递。 |
| `agent_combinations.py`、`agent_relations.py`、`agent_tools.py` | 组合、匹配、草稿中的类别判定与输出改快递；原有顺丰发票服务商谓词保留。 |
| `agent_transport.py`、`agent_product_support.py` | 已有顺丰和同城格式识别结果归快递；承运方身份及运单标题保留。 |
| `agent_content.py`、`agent_product_rules.py` | 打印服务及同份完整来源中的小物件归打印；「顺丰快递费」仍是商品／服务名称，顺丰运单表头和证据匹配保持。 |
| `courier.py`、`mail163.py` | 不改。所有「顺丰」均用于服务商、邮件过滤、运单类型或格式校验，并非费用类别。 |
| `courier_proofs.py` | 类别改快递，兼容旧类别与 `sf` 栏目；明确项目名称中的顺丰、顺丰同城继续作为已有凭证规则入口。 |
| `reimbursement.py` | 类别映射及通用快递错误提示改快递；`d.kind == '顺丰'` 的专用发票号码校验保留。加入打印收据／发票与支付记录导出。 |
| `report_types.py`、`report_snapshot.py`、`report_excel.py` | 新项目类型 `printing`、`express`；新栏目同名，旧 `sf` 兼容。快递使用第 29 行，打印与其他费用分组分配第 29–34 行空位，已有快递行优先保留；溢出仍沿用现有末行规则。公司模板的一级区域及固定行不变。 |
| `web/app.js`、`web/report.js` | 类别从后端清单加载；栏目选择加入快递和打印；凭证检查、类别汇总与旧预览兼容同步。默认不再把快递统称为“顺丰同城”，已保存的名称保留。 |
| `android/` | 固定清单、Jev 条目、材料规则、分类、报表类型、页面联动及付款附件选择同步。旧顺丰类别在读取／保存入口兼容；不改承运商文本，不在本任务中运行手机数据库升级。 |
| 测试 | 类别预期更新；用于票面、邮件、运单、商家／服务名称的顺丰保留；迁移及旧输入兼容测试故意使用旧类别。 |

类别支持所有快递平台，不代表增加了所有平台的安全 PDF 裁剪模板。顺丰／同城已有格式沿用验证；其他格式不能证明配对或裁剪边界时仍进入人工检查，本次未扩展承运商版式解析器。

## 启动迁移

`ExpenseStore` 初始化时运行 `2026-11-07-express-category`。在同一 SQLite 写事务内检查 `workbench_migrations`；成功后记录完成标记，重复启动不重复更新或备份。新库或没有旧类别的库只记录标记，不生成空备份。

有旧值时，先使用 SQLite `Connection.backup()` 保存一致性副本，沿用已有迁移工具的数据库备份方式，兼容 WAL。路径为当前工作区下 `backups/2026-11-07-express-category-时间戳-随机后缀/expenses.sqlite3`，不会覆盖历史副本。备份通过完整性检查后才更新类别；失败则启动报错，类别和完成标记均不提交。

迁移字段限定为：

- `expenses.data` 的 `category` 及嵌套分类结果，`report_section: sf → express`。
- `category_experience.category`，`category_attempts.result` 的 `category`、`managed_category` 等分类字段。
- `expense_preferences.data` 中 `kind=category` 的 `value`，以及 `kind=report_section` 的旧 `sf`。
- `intake_queue.data` 与 `material_matches.data` 内同名类别字段，覆盖识别、登记和撤销快照，避免重放恢复旧类别。

商家、标题、项目名称、显示名（包括「顺丰同城」）、展示合并组、金额、编号、状态、原文、附件及事件文字不变。已经生成的 PDF、Excel 和清单文件不重写；新版预览和新报表使用新类别。既有缺材料确认的哈希在读取时兼容旧类别拼写，确认记录本身不重写；金额或原件有变化仍须重新核对。旧事件中的「自动分类已保存：顺丰」也能继续识别。

新分类规则使缓存输入键变化，旧自动缓存可能重新判断；已有人工分类、已核对及已提交费用保持保护。不批量把历史「其他／材料采购」猜改为打印。

## 回滚

1. 由维护者停止工作台，保存当前整个工作区的另一个备份，避免丢掉升级后新增业务。
2. 部署升级前版本。否则新版本再次启动会重新执行类别迁移。
3. 从迁移目录选定对应数据库备份，用下面的 SQLite 备份 API 恢复；工作区路径必须替换为实际部署位置。此操作必须在停机后进行，不能直接覆盖运行中的 SQLite 文件或单独删除 WAL。

```python
from contextlib import closing
from pathlib import Path
import sqlite3

source = Path('workspace/backups/选定的迁移目录/expenses.sqlite3').resolve()
target = Path('workspace/工作台/expenses.sqlite3').resolve()
assert source.is_file() and target.is_file()
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as backup:
    assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with closing(sqlite3.connect(target.as_uri() + '?mode=rw', uri=True)) as database:
        backup.backup(database)
        assert database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
```

4. 启动升级前版本并核对账目。迁移副本只备份数据库；它不代替附件和整个工作区的日常备份。恢复该副本会回到迁移前状态，升级后新增记录须从第 1 步的备份另行恢复，不能丢弃。

本次开发不访问正式服务或真实台账；真实迁移由部署新版后首次启动触发。
