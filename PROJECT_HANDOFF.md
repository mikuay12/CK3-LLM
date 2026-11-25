# CK3 项目交接

更新于 2026-11-24。环境为 Windows、PowerShell、Python 3.14，适配 CK3 `1.20.1.2`（游戏目录 `C:\Games\Crusader Kings III`，依赖位于 `.deps`）。

- 架构图与模块关系：[ARCHITECTURE.md](ARCHITECTURE.md)（图示见 `docs/architecture/`）
- 接口与运行示例：[README.md](README.md)
- 最新实机全量验收记录：[docs/LIVE_VERIFICATION_REPORT.md](docs/LIVE_VERIFICATION_REPORT.md)
- 协议与内存偏移资料：[PROTOCOL_FINDINGS.md](PROTOCOL_FINDINGS.md)

---

## 一、目标与职责边界

让用户与大模型在同一联机房间各控制一个角色：本机真实 CK3 为房主 **A**，Python 独立联机客户端为虚拟机 **B**。

- **真实 CK3（A）**：负责世界推进、规则校验与行动结算。
- **虚拟机（B）**：负责联机同步、进程外只读内存查询（`ReadProcessMemory`）、零注入写时复制 Unicorn 原生求值（`ShadowEvaluator`）、按角色过滤的通知投递，以及提交大模型指令。
- **大模型**：负责全部玩法决策（宣战、行军、建设、外交、内政、事件选项、NPC 请求、决议与活动选择）。
- **数据原则**：业务数值一律读取游戏原生内存或通过 `ShadowEvaluator` 直接调用 `ck3.exe` 原生函数，不复算游戏公式；尚未定位的原生结果明确返回 `null` 与 `native_result_not_located`。自动暂停默认关闭。

---

## 二、当前已实机验收能力（2026-11-19 实机复测）

| 模块 | 已具备且实机验证通过的能力 |
| --- | --- |
| 联机底座 | 热加入、选角、准备、正常断开（`native_disconnect=confirmed`）、同身份重连（`machineid` 递增）与持续同步；带 `request_id` 的去重与 `action_feedback`（`submitted` / `host_echoed` / `delivery_unconfirmed`） |
| 世界与内存查询 | 人物（含 `+0x68` 原生年龄）、资源、领地、头衔、邻国、战争、军队、战斗、围城、建筑、内阁任务、派系（`+0x10` ID）、阴谋、宫廷职位（预筛 `0.12s`）、骑士团、秘密、任务委托（`0x5d34c30`）、GUI 消息流（`message_feed`）。`armies` / `council_tasks` / `court_positions` / `factions` / `schemes` / `secrets` / `task_contracts` 已选角时默认过滤当前角色（传 `"all": true` 可查全局）；`sieges` / `armies` / `war_theater` 均支持 5 次 `live_scan` 并发重试 |
| `ShadowEvaluator` 零注入原生求值 | 实机通过：围城进度%/需求/驻军上限/每日速度与 ETA、兵团与军队总人数、移动段消耗/比例/锁定/ETA、战报双方伤亡、省份补给上限（`CProvince::GetSupplyLimitFor`）、施工结束日期/剩余天数/进度%、人物互动 `CalcAIAccept`/`CalcAIIntermediaryAccept`（含原生 `PopulateScopes` `0x3088D40` 与 AVX2 降级/桩） |
| 战区与地图 | 省份地形、跨省寻路 `route`（`compute=true`）、一站式低 token 战区简报 `war_theater`（支持独立与附庸战争，实机 5/5 推进中战争 `1.36s~1.84s` 通过） |
| 玩法指令（97 原生命令 / 79 模型动作） | 已实机生效：`set_character_focus`、`change_council_task`、`send_interaction`（`gift_interaction` / `sway_interaction` / `arrange_marriage_interaction`，自动原生求值 `ai_accept`）、`select_event_option`、`reply_character_interaction`、`execute_decision`（含 `landed +0x430` 冷却验证）、`join_activity_command`、`decline_activity_invite_command`、`set_activity_intent_command`、`add_construction`、`cancel_construction`、`add_rally_point`、`remove_rally_point`、`raise_troops`、`move_unit`、`split_half_army`、`merge_units`、`disband_army`、`hire_mercenary` |
| 静态目录与模型运行时 | 501 决议、23 类活动、463 种非宣战人物互动、6 类定义目录（573 项）；`--model-config` 接入 Chat Completions 兼容服务，支持紧凑中文简报（`--llm-format compact_text`）、旧工具结果折叠与块级裁剪（`fold_old_tool_results`）、弹窗优先门控（`--modal-gate strict`）、UTF-8 文本记录（`--model-transcript`）、实时玩家对话（`chat_channel.py`）与双层记忆库（`llm_memory.py`，内置 `memory_manage` 工具） |
| 通知、管理台与 Agent 适配 | 统一 Web 管理台（`control_panel.py`，含 API 配置、首个预设提示词、四分区通知管理与 `$TITLE$` 中文注释、实时对话框、记忆与经验库）；通知支持队列合并、一次性去重、`--notification-cooldown` 冷却、瞬态读冲突静默及弹窗热内存预附带详情（含事件中文后果摘要与固定权重概率）；通用持久化游标监听器 `agent_watch.py`（兼容 Antigravity 与 Codex CLI） |

---

## 三、接下来做什么（按优先级排序）

### P0：修复已暴露的实机缺口
- [x] **继承确认协议缺口修复（2026-11-24，实机症状与坏档已确认，离线原生验证通过）**：新增 `close_succession` 编码和 `player_successions` 只读查询，按 B 端自身身份确认已接管且存活的继承人，正常断开前等待标记清除；修复协议旧角色缓存覆盖实时继承人的问题。用户坏档已另建修复副本，原件保留。详见 [调查、原生证据与恢复说明](docs/SUCCESSION_PAUSE_FIX.md)。
- [ ] **继承确认完整实机复测**：自然死亡 → 自动确认 → `stop` → 房主无错误死亡弹窗且能继续推进；载入本次修复副本验证解除暂停。此前“跨代继承零 OOS”未覆盖该确认步骤，不能据此视为完整验收。
- [x] **排查并修通 `start_activity_command`（主动举办活动）**：反汇编 `CActivityPlan::IsValid`（`0x2403940`）定位空 `invitations` 触发 `INVALID_ACTIVITY_NO_INVITED_CHARACTERS`（当 `allow_zero_guest_invites == no`）；同时修复 `action_feedback.py` 对房主回传中 `tenets`/`doctrines` 构造期空串填充 `["", "", ""]` 的比对兼容；实机成功举办 `activity_feast`（活动 `1627389963`、扣金 `339.71 -> 178.72`、触发 `feast.7002`、核对 `alive +0x500` 原生冷却 `1803` 天）与 `activity_hunt`（触发 `hunt.0550`）。
- [x] **复测 `stop` 及核心高频指令**：行军窗口实机复测 `stop`（`host_echoed`，截断路径 `[2215, 2220] -> [2215]` 并停驻）；实机验收常备军全链路（`add_maa_to_regiment` / `destroy_maa_regiment` / `create_maa_regiment` 均 `host_echoed`，并在 `men_at_arms_type(s)` 接入 `0x5c7c4e0` 原生 `native_index`）；实机验收宫廷职位（`revoke_court_position` / `give_court_position`）与特化授衔（`grant_titles_interaction` + `special_fields.titles` `host_echoed` 且 `c_rouergue` 实机易主）；修复 `jomini_codec.py` 与 `shadow_evaluator.py` 中 `province`/`landed_title` 作用域 token（`0x04FF` `prov` / `0x2EA7` `lt`）及 `_load_scope_type_ids` 崩溃。
- [x] **实机验收宣战、议和、召集盟友、创建头衔与跨代继承（2026-11-24 联机实测 1066→1096 零 OOS）**：实机全流程验证 `declare_war`（`individual_county_de_jure_cb` 与 `claim_cb` 连战德斯蒙德/奥索里/伦斯特/都柏林/布雷夫尼统一全爱尔兰）、`call_ally_interaction`（`target={"type":"war","identity":...}` 实机召集房主并被接受）、`end_war_attacker_victory_interaction`、`create_title`（创建 `d_leinster` `601` 与 `k_ireland` `555`）、`disinherit_interaction` 与自然跨代继承（芒斯特/爱尔兰国王穆尔哈德 `33336` → 布赖恩 `38035`）；对照房主原生封包修复 `jomini_codec.py` 与 `shadow_evaluator.py` 中 `end_war_*` 的 `war` 作用域编码（`scope.root` 与 `scope.event_targets["war"]`，不写顶层 `target`）；修复 `llm_formatter.py` 行军通知 `unit=None` / 活动邀请 `类型=None`、`game_notifications.py` `_prefetch_event_preview` 截断与 `shown=false` 隐藏选项过滤、`local_game_bridge.py` 异常退出后 `frontend_main.gui` 持久备份自动恢复，以及 `game_actions.py` 中 `create_maa_regiment`/`add_maa_to_regiment` 的 `count` 参数说明（实际为**士兵数**而非份数，如步骑 `100`、攻城 `10`）。
- [ ] **剩余专项指令实机验收**：`join_activity_command.travel_plan_draft_created` 与取消旅行；`join_war_interaction`（`interfere_in_war_interaction`）的原生 `war` 目标字段核对；**十字军（Great Holy War）宣誓参战指令**尚未录入 `jomini_codec.py`（待从房主点击宣誓的 `events.jsonl` 抓包补齐，或通过教皇开战前提醒事件选项直接参战）；实战观察到 `military.battle_phase_changed` 在 `pursuit`/`main` 阶段的兵力数字偶尔读取异常，待后续排查。

### P1：补齐大模型决策所需的原生求值与弹窗信息
- [x] **决议与活动的原生可执行性、最终费用（`ShadowEvaluator` 零注入）**：已接入 `CDecision::IsShown`（`0x31135B0`）、`CDecision::IsValid`（`0x31136C0`）、`CDecision::GetCost`（`0x1474800`）+ `CalculateCost`（`0x311D090` 10 槽资源全映射）+ `CanAffordCost`（`0x311B560`），以及 `CActivityType` `is_shown`（`+0x40` + `0x37407C0`）、`CanStart`（`0x2BCBEB0`）与基础费用（`+0x1368` + `0x311D090`/`0x311B560`），`decision`/`activity_type` 查询实机返回 `native_shadow_unicorn` 结果。
- [x] **普通事件原生选项可用性（`ShadowEvaluator` 零注入）**：已接入 `CTrigger::IsFulfilled`（`0x37407C0`）直接基于待处理事件原生 `CTopScope`（`CEventData +0x0`）求值每个 `CEventOption` 的 `trigger`（`+0x080`）与 `show_as_unavailable`（`+0x150`），`event_options` 实机返回 `available`/`shown`/`show_as_unavailable`（`native_shadow_unicorn`）。
- [x] **建筑报价、工期与合法性零注入求值（脱离 GUI 缓存限制）**：在 `building_economy.py` + `shadow_evaluator.py` 接入 `0x2C33390`（金钱/国库）、`0x2C33580`（威望）、`0x2C33760`（虔诚）、`0x2C33BD0`（实际修正工期天数）与 `CanConstructBuilding`（`0x2C87590`）；即使房主 GUI 未打开目标地产，`construction_quote` 也可对任意省份/建筑/升级等级返回实机 `native_shadow_unicorn` 报价、工期与 `can_construct`。
- [x] **活动参加资格与 NPC 请求按钮原生可用性（`ShadowEvaluator` 零注入）**：已接入 `CActivity::CanJoin`（`0x2BCB140`）填充 `activity`/`activity_invitation` 的 `response_options[join].available`；接入 `0x308AA50`/`0x308AE50` + `auto_accept`/`block` 触发器（对齐 `CReplyCharacterInteractionCommand::IsValid` `0x29751C0`）填充 `incoming_interaction` 的 `accept`/`reject`/`block`/`acknowledge` 四个按钮 `available`（实机返回 `native_shadow_unicorn`）。
- [ ] **剩余弹窗与细节补齐**：事件动态条件正文与 `duel`/`modifier` 动态原生精确百分比（固定权重概率与中文后果摘要已补齐）、决议专用 widget 参数、NPC 请求未生成正文、活动专用窗口；`message_feed` 仅属本机房主 GUI 缓存（B 端角色级消息使用 `player_messages`）。

### P2：真实大模型长局闭环与稳定性验收
- [ ] **独立模型服务真实长局验收**：使用 `--model-config` 接入真实大模型持续游玩，核对 AI 自主决策、通知唤醒、上下文预算与长期同步/OOS 一致性。

---

## 四、运行与验证入口

```powershell
# 1. 启动统一 Web 管理台（http://127.0.0.1:8766/：API 配置、预设提示词、通知管理、实时对话、记忆与经验库）
py control_panel.py

# 2. 启动房主游戏 A（建房后在 captures\local-game-* 下生成 session_profile.json）
py local_game_bridge.py

# 3. 接入虚拟机客户端 B（接入外部大模型时加 --model-config model_config.json；管理台第 1 页可直接复制完整命令）
py ck3_client.py connect-local `
  --session-profile "captures\local-game-本次目录\session_profile.json" `
  --live-memory-pid 当前游戏PID `
  --model-config model_config.json `
  --llm-format compact_text --modal-gate strict `
  --model-transcript --notification-cooldown 60 `
  --duration 0 --quiet --stdin --ack-received-turn

# 4. Agent 通用持久化游标监听（自动定位最新 events.jsonl，防漏消息，兼容 Antigravity 与 Codex CLI）
py agent_watch.py --include-context                # Antigravity 后台阻塞唤醒
py agent_watch.py --timeout 25 --include-context   # Codex CLI 带超时轮询续接

# 5. 独立只读内存查询 / 全量离线回归与原生字节校验
py live_objects.py --pid 当前游戏PID --stdin
py -m unittest discover -q
py verify_jomini_native.py --cases 16
```

---

## 五、核心代码索引与注意事项

| 模块 | 文件 |
| --- | --- |
| 联机、协议与沙箱求值 | `ck3_client.py`、`shadow_evaluator.py`、`jomini_codec.py`、`session_codec.py`、`session_state.py`、`nakama_native.py`、`pdx_nakama_codec.py`、`transfer_protocol.py`、`snapshot_transfer.py` |
| 本地房间与启动桥接 | `local_game_bridge.py`、`local_game_bridge.js`、`local_frontend.js`、`local_nakama.py`、`mock_nakama.py` |
| 内存读取与战区/寻路 | `process_memory.py`、`memory_world.py`、`live_objects.py`、`war_theater.py`、`army_navigation.py`、`building_economy.py`、`gui_message_feed.py` |
| 模型工具、管理台与通知 | `model_runtime.py`、`llm_formatter.py`、`llm_memory.py`、`chat_channel.py`、`agent_watch.py`、`control_panel.py`、`control_panel.html`、`game_actions.py`、`action_feedback.py`、`game_notifications.py`、`notification_catalog.py`、`notification_policy.py`、`notification_settings.py` |
| 目录与事件/互动/活动 | `event_catalog.py`、`event_evaluation.py`、`interaction_catalog.py`、`pending_interactions.py`、`activity_invitations.py`、`planning_catalog.py`、`definition_catalog.py`、`game_labels.py` |
| 快照解析与原生验证 | `world_snapshot.py`、`world_store.py`、`ai_observation.py`、`verify_jomini_native.py`、`verify_session_native.py`、`verify_world_native.py`、`verify_local_backend.py` |

- 军队 `army` 与移动单位 `unit` 编号独立，`move_unit` 用 `unit`，`disband_army` / `split_half_army` 用 `army`。
- `ShadowEvaluator` 严格零注入（只读 `ProcessMemory` + Unicorn 私有 CoW 影子页），绝不向 `ck3.exe` 写入内存或注入线程；Unicorn 2.x 不支持 AVX2，因此影子页内已将 MSVC CRT `__isa_available` 降级为 SSE4.2 并桩掉 `__std_find_trivial_{1,2,4,8}`。
- 内存偏移绑定 `ck3.exe 1.20.1.2`（SHA-256 校验）。验证和游玩产生的抓包保存在本地 `captures/`。
