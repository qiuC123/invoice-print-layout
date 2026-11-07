---
name: invoice-consolidate
description: 手动整理发票工作台的审计、疑点、分类、经验、偏好和识别纠正，只生成私有知识待审草稿；不写回工作台、不自动采用、不调度或发消息。
---

# 手动整理发票知识

仅在用户明确要求整理时运行。知识是寻找证据的线索，不能替代原件或用户决定。
代码仓库公开：任何真实对象、商家、人员、金额、项目、原话和来源记录只能留在用户指定的、Git 忽略的 `agent-knowledge/`。
只读取已有知识和下面的只读 CLI；只写 `agent-knowledge/pending/`，包括 `pending/SUMMARY.md`。不直接读 SQLite，不调用写接口、OCR、分类、`Preferences.apply`，不发消息或卡片。

## 读取位置与批次边界

1. 确认知识目录已被 Git 忽略。读取 `state.json` 的 `adopted_through` 和已有 `INDEX.md`，按需读知识页。首次没有状态时使用空位置，不创建正式文件。
2. 已有非空 `pending/` 时保留待审批次并报告位置，不覆盖它。同一时间只有一个整理器/采用者操作该目录。
3. CLI 使用已配置的本机 `http://127.0.0.1:8765` 与固定 `workspace_id`；通过每个命令自带的 `/api/runtime` 校验身份。换 URL 必须同时指定正确的身份。不要自动启动、重启或连接另一服务。
4. 全工作区整理省略 `--project`，否则会漏掉无项目/已删除对象。新命令中的 `audit/classification/experience/preferences/corrections` 拒绝项目过滤；`attention list --all` 也不要加过滤。
5. 旧状态身份不符、格式未知、来源不可读或分页不完整时停止，保留原状态，不把失败当空表/删除。不要自行迁移陌生状态。私有 P0 的状态若没有 `adopted_through`，保留其他顶层字段，从空位置完整建立首个指纹清单。

采用位置协议（以下仅为结构示意，运行时使用实际只读响应）：

```json
{
  "adopted_through": {
    "workspace_id": "<绑定的工作区身份>",
    "audit_id": 0,
    "corrections_id": 0,
    "snapshots": {
      "attention": {"fingerprint": "<快照指纹>", "keys": {}},
      "classification": {"fingerprint": "<快照指纹>", "keys": {}},
      "experience": {"fingerprint": "<快照指纹>", "keys": {}},
      "preferences": {"fingerprint": "<快照指纹>", "keys": {}}
    }
  }
}
```

`keys` 是该来源完整的 `稳定键 → 内容指纹` 映射，不存原始业务内容。位置只在采用后生效。

## 增量读取

所有命令加全局 `--json`；下面命令中的占位参数由当前状态/上一页响应提供。

| 来源 | 命令 | 增量依据 |
| --- | --- | --- |
| 审计 | `invoice-assistant --json audit list --after-id <id> --limit 200` | SQL `id`，含 `at/object/decided_by/evidence/project_id/data` |
| 识别纠正 | `invoice-assistant --json corrections list --after-id <id> --limit 200` | SQL `id`，含 `material_id/field/old_value/new_value/source/at` |
| 全状态疑点 | `invoice-assistant --json attention list --all --offset 0 --limit 200` | `object` |
| 分类尝试 | `invoice-assistant --json classification list --offset 0 --limit 200` | `item_id`；含 `input_key/evidence_key/result/expense` |
| 分类经验 | `invoice-assistant --json experience list --offset 0 --limit 200` | `evidence_key`，含禁用状态 |
| 偏好 | `invoice-assistant --json preferences list --offset 0 --limit 200` | `id`，不更新 `last_used` |

- 追加表循环到 `has_more=false`，每次用 `next_after_id`。无行时保留原游标；不依赖日期排序。`total` 是当前游标后的剩余行数。服务缺字段/游标不前进视为失败。
- 覆盖表没有可用时间游标。先读 `--offset 0 --limit 1` 比较 `fingerprint`；相同就复用旧 `keys`，不继续读页。不同则从第一页完整读快照，后续各页带 `--fingerprint <本轮指纹>`，按 `offset + len(items)` 前进，直到 `has_more=false`。
- 指纹变化导致分页失败时丢弃本轮该表的内存结果，从第一页重取；连续两次变化则报告源正在变动，本轮不产出批次。不得拼接不同快照或把半页结果当删除依据。
- 每页 `fingerprints` 是本页稳定键到 SHA-256 的映射；全表 `fingerprint` 是完整映射的 SHA-256。算法为 UTF-8 JSON、键排序、无空白分隔、保留非 ASCII 字符。内容包括分类的当前费用字段，因此核对/软删除/项目变化也会被检测。
- 用完整新旧 `keys` 比较：新键为新增，同键异指纹为修改，旧键消失为删除候选；只有新/变的记录进入提炼。首次扫描空表只建立内存基线，若所有来源都空且没有旧键消失，仍属于无新增记录。
- 分类包含软删除、无项目和失去费用关联的尝试；`expense=null` 表示关联已不存在，不得丢掉。审计的 `project_id` 来自事件的 after/before/data，不用当前项目覆盖历史事实。纠正中的新旧值是已解码 JSON，可为 null、字符串、数值或对象。
- `experience/preferences` 复用完整的 `GET /api/preferences`，CLI 本地稳定排序分页；这两个旧接口仍需读取全表才能判断变化。其余覆盖表也需服务端完整计算指纹；P1 只减少后续拉页和提炼，未实现逐键服务器增量接口。
- 所有来源无新增/修改/消失记录时，输出必须恰好为 `nothing`，不加说明、标点或 Markdown；不创建 pending、不写任何文件、不更新时间戳。

## 核对、提炼与办法统计

1. 新记录先核对已有知识：矛盾标「有冲突」，列双方来源，不擅自选边。来源被明确推翻的条目从拟生效页面移除，原文及推翻证据移入 `pending/pending-delete.md` 供审查。源键消失本身不证明结论错误，先标来源缺失/冲突。
2. 一条规律至少有两条独立出处，或一条明确用户决定。重复引用、同一次决定产生的审计/分类/纠正、`resolved_with` 联动记录不能算多条独立证据。不足门槛仅列待核实，不升级为规律。
3. 每条知识包含来源表及稳定键/审计 ID、存在的日期、决定者、状态（确认/推断/有冲突）。缺失日期/理由标缺失，不用读取时间补造；分类表没有时间列。
4. 用户原话在 `precedents.md` 逐字保留，并标对应对象、日期和原话出处；只在来源确实保存用户逐字表达时引用。`decided_by=user` 或 `source=human` 可证明决定归属，但不保证 reason/evidence.statement 是原话。系统生成的「已保留你的分类」等只能标系统说明；没有原话就注明「用户原话未记录」。
5. 从 evidence.method 和结果中的 `办法：` 行归纳查证办法，在 `methods.md` 累计尝试、成功及来源。按独立决定/查证事件去重；未解决、重试、联动副本不计成功。成功需有明确解决结果且未被推翻；被推翻时撤销对应成功计数，不能重复累计旧来源。成功次数 ≥5 标「建议写成代码」，仅建议，不写代码或改工作台。
6. 拟生效 `INDEX.md` ≤150 行，每个知识 Markdown 页面（含 methods/precedents/pending-delete）≤80 行。优先合并重复、清理最旧且无支持的推断；必要时拆页并更新索引。不可截断用户原话或删掉有依据的确认记录来满足行数，无法满足时不交付该批次。

## 写出待审批次

完成全部来源读取、比对和行数检查后再写入；草稿只能在 `pending/` 内。每个待修改页面都写完整替换版，未变页面不复制。
逻辑删除通过替换原页面并从 INDEX 移除条目完成；整页不再适用时用注明已撤回及出处的短页替换，不依赖采用脚本删除文件。

- `pending/state.json` 只需包含新的 `adopted_through`，包括完整游标和各表的完整键指纹清单；不能只保存本轮变化的键。即便新增记录没有达到提炼门槛，仍可产出只更新位置的批次，并在摘要列出略过的记录及原因。
- `pending/SUMMARY.md` 写新增/修改/冲突/删除各自数量，每项附来源（表＋键/ID）、影响页面、理由；说明删除仅为待审撤回，附游标前后值和快照变化。数量按知识条目计算，来源变化另外列示，避免混淆。
- 被推翻条目放 `pending/pending-delete.md`（必要时拆成编号页），原有效知识仍保持不变，直到用户采用。
- SUMMARY、state 是批次元数据；SUMMARY 可分页链接明细，不受知识单页 80 行约束。不可嵌套写 `pending/history/` 或 `pending/pending/`，不可使用链接或目录联接。

向用户报告 `pending/SUMMARY.md` 位置供审查。用户决定采用/退回时才执行仓库脚本：

```text
python scripts/knowledge_adopt.py adopt --knowledge <私有知识目录>
python scripts/knowledge_adopt.py reject --knowledge <私有知识目录> --reason <用户意见>
```

采用会先快照旧知识（不含 pending/history），再逐文件替换（包括 SUMMARY），最后只更新正式 state 的 adopted_through 并清空 pending；保留其他状态字段。退回只记录理由并清空 pending，不推进游标，下次会重新考虑这些记录。
替换仅单文件原子化；普通写入异常会回滚已替换文件，保留草稿和快照。进程被强制中止/机器断电后先检查快照、正式 state 与 pending，恢复完整快照后再重试，不声称整个目录事务原子化。
