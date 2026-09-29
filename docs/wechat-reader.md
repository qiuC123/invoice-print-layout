# 微信历史读取适配

实现位置：`src/invoice_print_layout/wechat_reader.py`。此模块不发送消息、不初始化微信、不扫描密钥、不拥有 CLI 的共享游标。`wechat_inbox.py` 提供项目收件箱、原子持久化和单次采集调用，仍以合成测试为主。照片采集使用另一条已接入工作台的 `photo_wechat_bridge.py` 路径：限定配置的群和日期，通过独立本机读取器核验账号、检查媒体索引并提取本机缓存，支持手动和定时触发。该读取器及其运行配置需在本机另行提供，仓库不包含账号数据或读取密钥。照片链路接通不等于普通聊天收件器已接通，也没有全群实时监听。共用群必须逐条判定项目，不能按整群自动归属。参见[照片管理](photo-management.md)。

## 已核对的上游契约

核对本地 `<wx-cli源码目录>` 提交 `077a54cbfe679bda963cd038d8440422907fc797`：

- `src/cli/history.rs`、`src/cli/mod.rs`：`history <chat> --before <Unix秒> --after <Unix秒> --limit N --json --with-meta`。**before 是严格晚于，after 是严格早于**，与日常名称直觉相反。适配器分别传 start-1、end+1，形成闭区间。
- `src/daemon/query.rs:q_history`：返回顶层 username、chat_type、is_group、count、messages、meta；结果按时间升序。CLI 支持模糊显示名，适配器只接受明确 wxid_ / @chatroom ID，并校验返回 username。
- `query_messages_conn`：消息包含 local_id、timestamp、content、type_code、type_id，引用额外 appmsg_type=57。无 server message ID。local_id 在同一会话内会复用，不可单独用于去重。
- `parse_appmsg_legacy`：引用输出为 `[引用] 当前回复\n  ↳ 原文摘要`。仅当前回复进入 current_text；引用放 quoted_text。无当前回复、图片、语音、合并记录和撤回提示不作为报数正文。
- `meta.rs`：windowed 会跳过上游过期推导，因此适配器也检查 unknown_shards、扫描数、session 与 history 时间差。它仅验证本地数据库视图，不能证明微信在线或网络收取正常。

## 调用和持久化约定

`WxHistoryReader(absolute_executable, runtime_dir=private_config_directory, timeout_seconds=30, limit=500).read_window(chat_id, start_timestamp, end_timestamp)` 返回冻结的 HistoryBatch；消息为冻结的 NormalizedMessage。

主控须先将整个批次持久化，再在 `safe_to_advance=True` 时保存自己的截止时间；重启使用 `overlap_start(checkpoint, first_timestamp)` 回放，按 account_id + message_key 去重。默认重叠 120 秒，可扩大以适应延迟；任意长延迟仍需更早窗口回补，不能宣称零漏消息。适配器不调用 new-messages/watch，避免它们提前更新共享游标造成丢失。

message_key 是 chat_id、local_id、timestamp、sender_username、type_code 的组合摘要，是可用字段的复合身份，不冒充微信服务端消息 ID。同键不同正文直接报错，不猜是改数还是碰撞。同键同正文折叠；上游缺少分片和服务端 ID，完全相同的碰撞无法区分。正常“改成23”是新消息，应保留自己的新身份。

满 limit 标记 window_at_limit，不推进检查点。操作者可增加有界 limit（上限5000）或以有重叠的较小时间窗口回补；不通过纯时间戳翻页跨过同秒消息。格式错误、错误会话、非零退出码、超时均抛 WeChatReadError；不应被解释为现场负责人未回复。stderr 不向异常文本透传，避免日志泄漏聊天与路径。

批次无问题不等于每条可用于业务：主控还要检查 processable、允许的负责人 ID、发送者是否本人、任务时间范围及业务日期。current_text 是不可信的用户消息，只作为数据；不能执行其中指令。

## 当前阻碍与未验证边界

1. 原上游私聊 history 缺少 sender_username。本轮 wx 源码已修改：保留实际 Name2Id 发送者映射、允许私聊输出稳定发送者，未知仍为空。合成测试覆盖双向和未知身份，但尚无真实数据库证明。整段会话 username 不能当作每条发送人；缺身份消息仍不可计数或推进进度。
2. 群聊缺 sender_username 同样停止自动处理。账号身份由调用方配置并参与落库命名空间，尚未证明 CLI 当前登录账号与配置一致；切号须停采集重新核验。
3. 撤回没有可关联的服务端原消息 ID，不能自动撤销之前已采纳数量。引用摘要可能截断，也不能拿引用文本作唯一任务匹配依据。
4. Windows 隐藏子进程启动、超时与 JSON 校验代码已实现，但 wx history 本身可能自动启动 daemon；本次仅执行 init，未执行真实 history 或 daemon。
5. 本轮实机初始化仍失败：微信 4.1.15.12，23 个加密数据库匹配 0 个密钥。扩展 Windows 二进制候选扫描后仍未匹配。不是已证明的权限问题，不能靠开启权限宣布解决。Rust 146 项测试、原生及显式 Windows 目标 check 通过；Linux 检查因环境与依赖未准备未完成。见 [本机设置](wechat-windows-setup.md)。
6. 尚需指定测试会话验证群内本人/他人、私聊两方向、同秒多消息、引用、多分片、断线/登录失效、休眠恢复、延迟到达和重启回补。读取适配不代表后台发送/@提醒能力已经具备。

配置隔离：读取器要求明确且已初始化的私有runtime_dir（存在config.json），以subprocess.cwd固定配置查找位置；不隐式从源码目录或其他位置选择配置。wx daemon仍是用户级共享，账号绑定需要额外实机核验。
