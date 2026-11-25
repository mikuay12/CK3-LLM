# CK3 实机接口与玩法指令全量验收报告

测试时间：2026-11-19  
游戏版本：CK3 `1.20.1.2`（本机房主 PID `31928`，房主 `LocalPlayer` 角色 `54444` Christopher）  
测试角色：独立客户端 B（`AIObserver`）托管图卢兹公爵 **Bernat**（人物 ID `65238`，主头衔 `d_toulouse` `7402`，首都省份 `2214` 图卢兹）  
离线回归：本轮修复后 `py -m unittest discover -q` 全量 **393 项单元测试全部通过**。

---

## 一、联机底座与基础观测

| # | 测试项 | 状态 | 实机证据 / 备注 |
| --- | --- | --- | --- |
| 1.1 | 热加入、选角、准备、正常断开与同身份重连 | ✅ 通过 | `machineid` 2 → 3 → 4 连续完成热加入、选角 `65238`、准备 `flags=71`、正常断开 `native_disconnect=confirmed` |
| 1.2 | `character`（含原生年龄 `+0x68`） | ✅ 通过 | `age=41, age_status=native_memory`（出生 `894.12.27`，当前 `936.5.16`），资源/特质/领地/军队完整 |
| 1.3 | `observe`（个人观察全量 + 增量 delta） | ✅ 通过 | `t-obs-full` 返回完整字段，`t-obs-delta` 同日返回空增量 `data={}` |
| 1.4 | 实时通知推送（`game_notification`） | ✅ 通过 | 实机收到 `liege_changed`、`succession_changed`、`traits_changed`、`family_changed`、`titles_changed`、`faith_changed`、`pending_event`、`pending_interaction`、`activity_invitation`、`military.movement_started` / `movement_stopped` |

## 二、世界查询与 `ShadowEvaluator` 实机求值

| # | 测试项 | 状态 | 实机证据 / 备注 |
| --- | --- | --- | --- |
| 2.1 | `siege`（进度%、总需求、驻军上限、每日速度与 ETA） | ✅ 通过 | `native_shadow_unicorn`：`22.509%`、`274.42`、`387`、`1.05/天`、`203天`（`0.16s`） |
| 2.2 | `army`（兵团与军队汇总人数） | ✅ 通过 | `native_army_regiment_sum`：`654 / 664`（`0.01s`） |
| 2.3 | `unit_movement`（路段消耗/比例/锁定/ETA/方位） | ✅ 通过 | `native_shadow_unicorn`：`cost=43.0`、`fraction=0.66666`、`locked=True`、`eta=5天`、`SW`（`0.01s`） |
| 2.4 | `combat_result`（战报双方伤亡总数） | ✅ 通过 | `native_shadow_unicorn`：`58.79` / `84.19` |
| 2.5 | `province`（地形 + `ShadowEvaluator` 补给上限） | ✅ 通过 | 修完 AVX2 桩后实机读出 `2214` 图卢兹 `terrain=plains`、`supply_limit=4745` |
| 2.6 | `route`（`compute=true` 跨省寻路） | ✅ 通过 | `2214 -> 2220`：`hops=2`、`cost=96.11`、`route=[2214,2215,2220]`、`cardinal=E` |
| 2.7 | `war_theater`（一站式低 token 战区简报） | ✅ 通过 | 修完围城预筛与头衔图遍历后，连续 5 场推进中战争 5/5 通过（`1.36s~1.84s`） |
| 2.8 | `council_tasks`（默认本角色过滤） | ✅ 通过 | 精准返回图卢兹 6 个内阁任务 |
| 2.9 | `factions`（修复 `+0x10` ID 偏移 + 默认本角色过滤） | ✅ 通过 | 全世界 252 个派系正常读取，本角色过滤精准返回针对 Bernat 的自由派系 `201326664` |
| 2.10 | `schemes` / `secrets` / `task_contracts` / `holy_orders` | ✅ 通过 | 修复 `task_contract` 表指针 `0x5d34c30` 后全部正常（115 委托、35 骑士团） |
| 2.11 | `court_positions`（动态运行稳定性与预筛修复） | ✅ 通过 | 加入 `live_scan` 并在读全对象前按 `+0x120/+0x124` 预筛后，实机 `0.12s` 一次成功返回 3 个宫廷职位（编年史官/总管/御医） |
| 2.12 | `holding_buildings` + `construction.timing`（施工中 `ShadowEvaluator`） | ✅ 通过 | `2226` 开工 `cereal_fields_02` 后原生读出 `paid_cost=250.0`、`end_date=940.1.1`、`days_remaining=1084`、`progress_percent=1.004%`（`native_shadow_unicorn`） |
| 2.13 | `message_feed`（本机 GUI 消息流缓存） | ✅ 通过 | 读出 5 条缓存及中文标题与日期 |
| 2.14 | `decisions` / `activity_types` + 原生冷却读取 | ✅ 通过 | `invite_knights_decision` 执行后立即读出 `remaining_days=3621, on_cooldown=true, status=native_memory` |
| 2.15 | 6 类静态定义目录（`definition_catalog.py`） | ✅ 通过 | 全部 6 类正常返回：`council_task_types`（72）、`court_position_types`（81）、`court_position_tasks`（85）、`scheme_countermeasures`（23）、`laws`（201）、`men_at_arms_types`（111） |
| 2.16 | `ShadowEvaluator` `CalcAIAccept`（含 `PopulateScopes`） | ✅ 通过 | 6 类互动均原生算出真实接受度（`100.0` / `-542.75` / `-320.0` / `-1973.0` / `1.0`） |

## 三、核心玩法指令实机验收

| # | 指令 / 模块 | 状态 | 实机证据 / 备注 |
| --- | --- | --- | --- |
| 3.1 | `set_character_focus`（切换生活重心） | ✅ 通过 | `null` → `stewardship_domain_focus`，实机确认生效 |
| 3.2 | `change_council_task`（变更内阁任务） | ✅ 通过 | 大法官 Tièrri `4782`：`task_domestic_affairs` → `task_foreign_affairs`，实机确认生效 |
| 3.3 | `send_interaction`：`gift_interaction`（自动 `ShadowEvaluator` 求值） | ✅ 通过 | 给封臣 Bermond `33592574` 赠金：Bernat `460.92 → 310.92`（`-150`），Bermond `277.63 → 427.63`（`+150`） |
| 3.4 | `send_interaction`：`sway_interaction`（开启密谋） | ✅ 通过 | `host_echoed`，实机 `schemes` 确认新建 `sway` 密谋 `469762181`（进度 4） |
| 3.5 | `event` / `event_options` / `select_event_option`（待处理事件） | ✅ 通过 | 连续处理 `771`（`vassal_interaction.0022` 选项 `0`）、`774`（`health.1001` 选项 `3`）及后续治疗事件 `781`（`health.3103` 选项 `0`），均 `host_echoed`，事件队列清空且角色 `ill` 痊愈 |
| 3.6 | `incoming_interaction` + `reply_character_interaction`（回复 NPC 请求） | ✅ 通过 | 实机收到并回复 2 笔请求：`1660944390`（`spiritual_counsel_interaction`）与 `3590324263`（房主 Christopher `invite_to_council_position_interaction`），均 `host_echoed` 且待回复队列清空 |
| 3.7 | `activity_invitation` + `join_activity_command` + `decline_activity_invite_command` + `set_activity_intent_command` | ✅ 通过 | 实机收到 2 份邀请（`654311459` 狩猎、`1224736805` 加冕）：低 token 摘要正常；拒绝加冕与参加狩猎均 `host_echoed`，受邀列表清零，狩猎参与人数 `16 → 17` 包含 `65238` 并触发旅行事件；`set_activity_intent_command`（`reduce_stress_intent`）`host_echoed` |
| 3.8 | `add_construction` + `cancel_construction`（建设开工/施工进度/取消） | ✅ 通过 | `2226` 槽位 `2` 开工 `cereal_fields_02`（`605`）`host_echoed`，读出 `ShadowEvaluator` 施工进度后执行 `cancel_construction` `host_echoed`，施工清空并退还 250 金 |
| 3.9 | `execute_decision`（执行决议 + 核对原生冷却） | ✅ 通过 | 执行 `invite_knights_decision` `host_echoed`，威望 `411.75 → 271.07`（`-150`），原生冷却立即变为 `3621` 天 |
| 3.10 | `add_rally_point` + `remove_rally_point`（增删集结点） | ✅ 通过 | 省份 `2226` 新增并移除集结点，均 `host_echoed` |
| 3.11 | `raise_troops` + `move_unit` + `split_half_army` + `merge_units` + `disband_army` | ✅ 通过 | 全流程实机通过：集结单位 `1006632968` → 行军至 `2215` 并确认到达 → 半数分兵为 `[1006632968, 1140850700]` → 合并回 `[1006632968]` → 解散后确认 `unit_ids=[]` |
| 3.12 | `start_activity_command`（主动举办宴会/狩猎） | ✅ 通过（2026-11-20） | 定位 `CActivityPlan::IsValid`（`0x2403940`）要求非空 `invitations`，实机举办 `activity_feast`（活动 `1627389963`、扣金 `339.71→178.72`、`feast.7002`、原生冷却 `1803` 天）与 `activity_hunt`（`hunt.0550`）均生效；同步修复 `action_feedback.py` 对 `tenets`/`doctrines` 空串填充的回传比对 |
| 3.13 | `stop`（中途停止行军） | ✅ 通过（2026-11-20） | 行军至 `2220` 途中发送 `stop` `host_echoed`，因 `leg_progress_fraction=0.75891` 已锁段，路径截断 `[2215, 2220] → [2215]` 并于 `2215` 触发 `movement_stopped` |
| 3.14 | 常备军全链路（`add_maa_to_regiment` / `destroy_maa_regiment` / `create_maa_regiment`） | ✅ 通过（2026-11-20） | 实机依次扩编 `15144`、解散 `15144`、新建 `regiment=43`（`light_footmen`），三项均 `host_echoed`；接入 `0x5c7c4e0` `native_index` |
| 3.15 | 特化互动与宫廷职位（`grant_titles_interaction` / `revoke_court_position` / `give_court_position`） | ✅ 通过（2026-11-20） | 修复 `shadow_evaluator.py` `_load_scope_type_ids` 与 `jomini_codec.py` `prov=0x04FF`/`lt=0x2EA7` token 后，授予 `c_rouergue`（`7418`）给 `61070` `host_echoed` 且 `titles_changed` 生效；免职与复任 `antiquarian_court_position` 均 `host_echoed` |
| 3.16 | `ShadowEvaluator` 决议/活动原生求值（`decision` / `activity_type`） | ✅ 通过（2026-11-20） | 实机零注入求值 `invite_knights_decision`（`shown=true, valid=true, can_afford=true, prestige=150.0`）、`activity_feast`（`shown=true, available=false, cost.gold=60.0, on_cooldown=true`）与 `activity_pilgrimage`（`shown=true, cost.gold=45.0`），均返回 `native_shadow_unicorn`；二测 `start_activity_command`（`feast-echo-1`）已实机确认 `host_echoed` |
| 3.17 | `ShadowEvaluator` 建筑报价/工期/合法性（`construction_quote`） | ✅ 通过（2026-11-20） | 脱离房主 GUI 缓存，实机零注入求值图卢兹 `2214` 的 `pastures_01`（`gold=150, days=572.8675, can_construct=false, ignore_cost=true`）与 `castle_02`（`gold=550, days=1432.16875, can_construct=false`） |
| 3.18 | `ShadowEvaluator` 事件选项原生可用性（`event_options`） | ✅ 通过（2026-11-20） | 直接基于 `CEventData +0x0` 原生 `CTopScope` 调用 `CTrigger::IsFulfilled`（`0x37407C0`），实机求值事件 `798`（`prison.9499`）与 `800`（`feast.7002`）选项返回 `available=true, shown=true, show_as_unavailable=false, live_option_mapping=native_shadow_unicorn` |
| 3.19 | `ShadowEvaluator` 活动参加资格与 NPC 请求按钮可用性 | ✅ 通过（2026-11-20） | `activity`（`2030043137`）经 `CActivity::CanJoin`（`0x2BCB140`）返回 `join.available=false`；`incoming_interaction`（`1795162115` `release_from_prison_interaction`）经 `0x308AA50`/`0x308AE50` + `auto_accept`/`block` 触发器返回 4 个按钮的原生 `available`（均 `native_shadow_unicorn`） |

---

## 四、本轮共定位并修复的 8 项实机问题汇总

1. `shadow_evaluator.py`：降级 MSVC CRT `__isa_available`/`__isa_enabled`（`0x5443098`）至 SSE4.2，避免 Unicorn 2.x 触发 AVX2 `UC_ERR_INSN_INVALID`。
2. `shadow_evaluator.py`：桩掉 AVX2 编译的 `__std_find_trivial_{1,2,4,8}`（`0x3FA3230..0x3FA3400`），使 `CProvince::GetSupplyLimitFor` 与复杂互动触发器正常求值。
3. `shadow_evaluator.py`：补全 Windows TEB 自指针 `gs:[0x30]` 及 `GetSystemTimeAsFileTime`/`SetFilePointerEx`/`GetLastError`/`WriteFile` IAT 桩。
4. `shadow_evaluator.py`：`evaluate_interaction_acceptance` 增加选项缓冲区并调用原生 `CCharacterInteraction::PopulateScopes`（`0x3088D40`），彻底修复 `scope:actor`/`scope:recipient` 未绑定问题。
5. `ck3_client.py`：`get_shadow_evaluator()` 补查 `DirectObservationQueue.reader` 与 `progress_world`。
6. `memory_world.py`：`CFaction` ID 偏移 `0x8 → 0x10`；`CTaskContract` 全局表偏移 `0x5d34c18 → 0x5d34c30`；`court_positions` 等列表加入 `live_scan` 并按 `+0x120/+0x124` 预筛。
7. `war_theater.py`：改用轻量 `siege_context` 预筛围城、单次内存遍历 `de_facto_liege_id` 头衔图支持附庸战争与独立战争、隔离无关引用、仅对最终目标求值补给上限。
8. `live_objects.py`：`council_tasks` / `court_positions` / `factions` / `schemes` / `secrets` / `task_contracts` 已选角时默认过滤当前角色（支持 `"all": true` 查全局）。
