# CK3 独立客户端协议调查

## 目标和当前进展

### 2026 年 11 月 24 日继承确认与退出暂停修复

`close_succession`（token `0x3c00`，vtable `0x4770590`，Write/Read `0x28a1cf0/0x28a1d90`）须传继承后的 `character` 和客户端 `machineid`。原生 helper `0x28a1ba0` 校验角色与玩家身份后，清理 `CPlayerInGame +0x260` 的 `has_open_succession` 及 `+0x268/+0x2c8` 的可选继承数据。新增编码已与房主实际继承封包及原生序列化逐字节比对，并在 Unicorn 中执行原生状态清理验证。

一次长局测试未发送此命令。`autosave_exit.ck3` 保留 `name="Antigravity" character=38035 player=-1 has_open_succession=yes` 与 `previous_played=33336`，确认继承人接管不代表继承确认完成。客户端现按自身身份自动确认并在正常断开前复查；原件保持不变，另建只清理该离线记录的坏档修复副本。完整证据、验证边界和复测步骤见 [继承暂停修复说明](SUCCESSION_PAUSE_FIX.md)。

### 2026 年 11 月 24 日召集盟友参战实机验证

`call_ally_interaction` 必须在普通 `target` 和 `scope.event_targets.target` 中携带战争作用域。运行中原生 scope 注册表确认 `war` token 为 `0x2b3f`，本次 scope type index 为 16；该 index 由 `ShadowEvaluator` 动态读取，不硬编码到求值逻辑。已补齐作用域编码、原生求值注册表映射及参数说明，并用 `NativeSerializerOracle` 核对带战争目标的完整命令字节。

客户端 B 控制芒斯特 Murchad（33336），向房主英格兰 Harold（32396）发送战争 100663342（`refused_liege_demand_war`）的召集请求，收到 `host_echoed`。游戏日期 `1079.12.14` Harold 加入防守方；`war` 查询确认参战名单包含两位玩家，事件 88（`call_ally.0100`）提供接受参战回信及正确战争目标。记录位于 `captures/connect-local-20261124-101533-200fb88f/events.jsonl`。相关战争与沙箱回归共 16 项通过（1 项旧捕获缺失跳过）。

### 2026 年 11 月 17 日决议与活动发现接口

`planning_catalog.py` 复用安装脚本解析器和本地化读取，提供 `decisions`、`decision`、`activity_types`、`activity_type` 查询。本机 501 个决议、23 类活动的索引和全部详情读取通过检查，记录在 `captures\events-20261116\planning_catalog_validation.json`。决议返回条件与效果原文、确认文字及 `execute_decision` 参数。活动类型返回阶段、配置类别与选项，逐项 `details_query` 可读取完整脚本。默认规则保留为数据，具体决议及选项由大模型决定。

目录标记来源为 installed_game_scripts。当前角色的显示和执行状态、费用及剩余冷却的原生结果待定位，查询中保持空值。`activities` / `activity` 复用已有原生活动对象布局，提供筹备中、进行中及保留的完成实例查询，按 `completed` 原生标记筛选。详情提供模型指定的参加参数，具体参加资格仍待原生核对。

主动举办使用 `CStartActivityCommand`，token `0x362c`，主 vtable `0x477c070`，Write/Read 为 `0x2989dc0/0x2989e70`。主对象 `+0x20` 保存完整规划数据，Write 将其放入 data token `0xf0`，调用 `0x2404d20`。校验路径 `0x2989db0` 将规划数据传给 `0x2403940`。

规划数据的类型指针、主持者、意图和开始日期分别位于 `+0/+8/+0x10/+0x20`，guest_delay_days 和 invite_duration 位于 `+0x28/+0x2c`。options、phases、invitations、special_guests 位于 `+0x98/+0xb0/+0xc8/+0x1b0`，可选 travel_plan 位于 `+0x1c8`，初始化标记为 `+0x520`，variables 位于 `+0x528`。这些字段及子结构的完整参数构造、字节对照与实机执行仍待完成，当前操作目录尚未注册该命令，活动详情明确返回 command_not_implemented。静态证据为 `activity_plan_writer.txt`、`activity_plan_components.txt` 与 `decision_activity_execution_layout.txt`。

### 2026 年 11 月 17 日人物请求与活动邀请

窗口相关决策由接入的大模型提交。查询、通知与内存读取提供当前实例和操作参数，回复命令必须携带模型选择。普通事件的 `events` 默认使用当前控制角色，列表与详情读取时复读接收者及实例身份，避免旧页或读取中变化造成错误归属。

`CPendingCharacterInteraction` 的数据库指针位于全局 `0x5d33c78`，主 vtable 为 `0x4769590`，实例 ID 位于 `+0x10`。`CCharacterInteractionData` 从 `+0x18` 开始，定义指针位于 `+0x18`，scope 与 original_scope 为 `+0x20/+0x188`。actor、recipient、secondary_actor、secondary_recipient、intermediary、puppet_or_actor 依次位于 `+0x2f0` 至 `+0x304`，选项 vector 位于 `+0x318`。排队天数与 AI 回复延迟位于 `+0x5b8/+0x5bc`。`+0x5c0` 的 intermediary_status 使用 none、pending、accept、decline，值依次为 0、1、2、3。`+0x5c4/+0x5c5/+0x5c6` 为保存的 AI 中间人接受标记、AI 接受标记和通知标记。读取器从同一次 fresh 字节块解码归属和阶段，再复读校验。

沿已核对的应用和场景对象链取得 `CIngameInterfaceHandler` 后，其 `+0xf8` 指向 `CCharacterInteractionNotificationWindow`，vtable 为 `0x455ce98`。窗口 `+0x3f8` 的 vector 保存 `0xc8` 字节的 pair，`+0` 为待回复实例 ID，`+8` 开始为六条已生成的 CUTF8String。`GetNotificationText` 的实际函数 `0x1371f20` 在 intermediary_status 为 1 时使用前两条 opening/body，其余状态使用第三、四条。`GetSigned/GetSignature` 使用第五、六条。详情直接读取匹配实例的这四项文字，保留原生富文本，状态为 partially_cached，刷新时间为 untracked。正文尚未生成、原生动态标题和按钮可用性仍有缺口。

`reply_character_interaction` 的 token 为 `0x28b4`，vtable `0x449bd98`，Write/Read 为 `0x29752d0/0x2975360`。`character_interaction` 使用 token `0x28a9` 和 U32，回复字段 `accept` 使用 token `0x28b0` 和 I32。原生界面 Accept、Reject、Block、Acknowledge 的提交值依次为 0、1、2、4，模型接口使用同名语义值。回复为必填项，新增 I32 枚举编码保留原生整数格式。

`CActivity` 数据库指针为全局 `0x5d32c90`，主 vtable `0x473ea08`，ID 位于 `+8`。其序列化基类位于 `+0x10`，vtable 为 `0x473e9d0`。以下均为主对象偏移。类型与主持者为 `+0x3a0/+0x3a8`，阶段 vector 为 `+0x3c8`，每项 `0x38` 字节，`+0/+8` 为阶段定义与省份 ID。当前阶段索引为 `+0x3e0`，创建日期与 active_start_date 为 `+0x3ec/+0x3f4`，active/completed 为 `+0x420/+0x421`，attending vector 为 `+0x528`。

原生 `Activity.IsInvited` 的注册回调 `0x9e5500` 调用 `0x9e1e90`，从角色 `+0x1b0` 取得 alive，再从 alive `+0x550` 的原生活动 ID vector 查找活动 `+8` 的 ID。`activity_invitations` 与单项详情使用这份角色列表并复读校验。历史快照中 Richard `84327` 收到活动 `570425364` 的加冕邀请，主持者为 `55750`，阶段省份为 `2575`。该历史记录用于布局研究，新查询的实际运行仍待验收。

`join_activity_command` 的 token 为 `0x37df`，vtable `0x477c680`，Write/Read 为 `0x298a610/0x298a770`。对象 `+0x20/+0x24` 为 activity/character U32，token 为 `0x2816/0x6ec`，默认 `0xffffffff` 省略。`+0x28` 的 travel 为 0 时使用默认 yes 并省略，值为 1 时写 token no `0x40f`，字段 token 为 `0x3627`。`+0x390` 的 check_start_date 默认 true，字段 token `0x3614`。原生旅行草案位于 `+0x30`，其 `+0x358` 为 false 时整块省略，当前编码覆盖该省略形式。`decline_activity_invite_command` 的 token 为 `0x39ca`，vtable `0x477c520`，Write/Read 为 `0x298a9d0/0x298aa70`，包含同样的 activity/character 引用。

人物请求与活动邀请分别新增 `state.pending_interaction`、`state.activity_invitation` 通知及对应筛选组，目录共 1073 个类型。37 种命令通过 592 次原生序列化字节对照。邀请归属通过 256 次原生 `IsInvited` 函数对照，信件阶段选择通过 4 次原生分支对照。复查为 `captures\events-20261116\verify_window_memory.py`，结果为 `native_window_memory_validation.json`，完整回归日志为 `window_interface_tests.log`。正常 SDK 的新增操作下发、重复请求与房主回传关联已通过离线桩。

用户当前要求保持游戏关闭。新增回复和邀请操作的实机执行、原生按钮可用性、普通事件复杂动态正文、活动邀请正文和活动专用窗口仍待补齐。原生窗口管理器 `0xc48fd0` 的该生成路径检查本机角色或 manager `+0xa8` 指定角色，B 的待处理事件存在时可能缺少对应原生 GUI 缓存。

### 2026 年 11 月 17 日当前查询来源与实机验收

默认查询按用户要求提供已确认的原生内存字段、已求值缓存和安装资源元数据。围城与施工的百分比、最终剩余天数、ETA、年龄、部分汇总人数及脚本条件结果的原生缓存仍待定位，当前返回空值与 `native_result_not_located`。原始日期、进度点、分组人数及已有原生聚合缓存继续读取。本文保留各日期的实验记录，当前接口约定见本节与 `PROJECT_HANDOFF.md`。

B 在沃里克省份 1603 执行 `pastures_04`，原生施工记录保存费用为 500 gold，随后槽位更新且施工对象清空，确认完工。另在省份 480 的候选列表读取到 `pastures_01` 为 150 gold、657 天，`hill_farms_01` 为 100 gold、985.5 天，`hunting_grounds_01` 为 100 gold、657 天，金额与界面一致。省份、付款角色与槽位不匹配时返回 unavailable。关闭列表后实测仍可保留已求值缓存，刷新时间标记 untracked。证据为 `captures\economy-20261116\live_candidate_quote_validation.json`。

集结与移动实测由 B 提交省份 1604 为目标，游戏生成路径 `[1604]`，只读查询确认实际到达，随后解散并核对 `unit_ids=[]`。证据为 `native_only_sdk_validation.json`。本轮实机已结束，用户最新要求暂不开游戏。

### 2026 年 11 月 17 日升级报价缓存

`HoldingView.GetTrack` 的回调 `0x12b9f10` 转到 `0x12a71f0`，返回 `CHoldingView +0x1c8` 内嵌的 `SGUITrackItem`。track 的父窗口、当前建筑、下一等级、选中等级和槽位分别位于 `+0x450/+0x458/+0x460/+0x468/+0x470`。

`GetNextCost` 的原生 getter 为 `0x12add30`，金钱 breakdown 在 track `+0x478`，国库分支在 `+0x558`。`GetNextPrestigeCost` 为 `0x12aded0`，breakdown `+0x638`。`GetNextPietyCost` 为 `0x12adf40`，breakdown `+0x718`。`GetNextConstructionTime` 为 `0x12adfb0`，breakdown `+0x7f8`。最终结果继续使用 breakdown `+0x78` 的 fixed64、初始化 kind/state 与 `+0x38` 货币字符串。

`GetSelectedCost` 的 `0x12ae020` 和选中等级工期 `0x12ae220` 使用同组 breakdown。读取器在选中等级为当前或下一等级时读取默认升级缓存，更远等级预览存在目标歧义时返回 unavailable。金钱与国库两套缓存同时已求值时标记货币歧义。该路径复用 `construction_quote`，返回 `cache_kind=upgrade_track`，覆盖初始化、真实零值、角色和槽位、两种货币及并发变化的离线回归已通过，实机核对待做。

静态证据集中在 `captures\economy-20261116\track_*`，读取过程使用进程外内存访问。费用和工期来自已保存的缓存字段。

### 2026 年 11 月 17 日退出与收件人路由

正常 stop 和 duration 退出发送 `ClientRequestDisconnect`，最多等待 3 秒确认再结束 Nakama。主循环异常及 Ctrl+C 也尝试该握手。收到房主 `ServerRequestDisconnect` 时回复携带原始 reason 的 `ClientRespondDisconnect`。游戏层握手新增 `--timeout` 上限。

实机发现收到的 Nakama 广播可能包含收件人为其他玩家的 `0xcafe` 帧，其中断开响应曾清空本机会话和同步状态。正常入口现在根据 connected.user_id 校验 PDX recipient，保留原始记录并标记 `routing_ignored=different_recipient`。`ServerRespondDisconnect` 另与本客户端已发出的请求和房主来源关联。

同一 identity `ck3-ai-native-reader-20261117` 以机器 6、7、8 连续热加入。6 和 7 正常退出均收到房主确认，8 收到房主主动断开后正确应答并结束。旧遗留 peer 已通过同身份原生断开请求清理。强制终止后的自动恢复和长期同步仍待验收，包序号与旧会话的独立影响仍待核对。

### 2026 年 11 月 17 日原生 GUI 消息正文

沿应用、当前场景和 GUI idler 到 `CIngameInterfaceHandler` 后，其 `+0x478` 指向 `CMessageFeedHandler`，vtable `0x44fb590`。显示项的指针数组位于 handler `+0xe8`，容量和数量为 `+0xf0/+0xf4`。`CPlayerMessageItem` 的 `+0` 为父 handler，`+8` 开始保存 SMessage，分配大小为 `0x1250`。标题、正文与效果文字的已生成字符串位于 `+0x11d0/+0x11f0/+0x1210`，创建和到达日期为 `+0x1178/+0x1180`。普通类型与筛选定义的 key 为对象 `+0x18`，缺省筛选对象使用 `0x4e756c6c` 标记。

`message_feed` 查询逐项复读对象链、数组、父引用与字符串，保留原生富文本控制标记，支持分页、fields 和输出预算。实机确认集合有 21 条并读取前三条，分别为围攻获胜、围攻中抓获的俘虏和家族关系终止，标题与界面对应。接收角色归属仍待定位，响应使用 `scope=local_game_gui` 与空的 `recipient_character_id`，按角色过滤、自动通知接入和日志待补。证据为 `captures\notifications-20261115\live_gui_message_feed_validation.json`。

### 2026 年 11 月 17 日建筑原生费用与报价缓存

建筑费用查询按用户要求优先读取游戏保存的结果。`CBuildingType +0x6f518/+0x6f520/+0x6f528` 分别保存基础金钱、威望和虔诚费用，`+0x6f500` 是前级建筑指针。施工记录位于省份 `+0x688`，其 `+0x20` 保存十项 fixed64 资源费用。金钱、威望、虔诚和国库对应索引 0、1、2 和 6。原生写入器 `0x247d170` 将这一结构交给 `0x2408cc0`，token `0x2b25` 为 cost，内部资源写入器为 `0x311d450`。`holding_buildings.construction.paid_cost` 直接读取该记录，同时提供原生 `base_days_remaining`。

GUI 根指针为全局 `0x5ccd808`，由 `CApplication` 构造函数在 `0x3939f4c` 发布。具体应用类型为 `CInterfaceApplication`，vtable `0x44abf28`。沿应用 `+0x28`、当前场景 `+0x10`、图形 idler `+0x88`、界面 handler `+0xd0` 到达 `CHoldingView`。后四个 vtable 分别为 `0x44e6408`、`0x44cc6f0`、`0x44cab38` 和 `0x4553820`。实机已确认这五级对象类型。应用 `+0x78` 保存待切换场景，主循环在 `0x393bdaf` 将它安装到当前场景 `+0x28`。读取器和独立回归覆盖当前场景与待切换字段的差异。`0x5ccafd8` 保留为先前调查线索。

`CHoldingView +0xd8` 是当前省份，`+0xe8/+0xf0/+0xf4` 是候选建筑指针 vector、容量和长度。`SGUIPotentialBuildingItem` 的 `+0/+8/+0x10` 是父窗口、建筑与槽位。金钱或国库、威望、虔诚和工期 breakdown 分别从 `+0x18/+0xf8/+0x1d8/+0x2b8` 开始，最终 fixed64 结果位于各 breakdown `+0x78`，即 item `+0x90/+0x170/+0x250/+0x330`。原生 getter 为 `0x12ac8d0/0x12ac990/0x12aca00/0x12aca70`。货币标记直接读取 breakdown `+0x38` 的原生字符串。

构造函数 `0x12ac4b0` 将结果清零、kind `+0x88` 设为 1、state `+0x8e` 设为 `0xc`。费用和工期求值将 kind 设为 0 并清除 state 的 `0x04` 位，工期还设置 `0x20` 位。读取器按这些标记区分未求值与真实零值。付款角色上下文来自 `view +0x1c0` 所指省份，`0x12b2360` 读取其普通头衔持有者，男爵领缺少持有者时使用父头衔持有者。查询校验该当前角色与请求角色一致，复读原生对象链、候选 vector、身份、结果和货币字符串，变化时返回不可用。

`construction_quote` 已接入基础费用、原生报价缓存和候选工期。每项未求值的动态字段为空。缓存更新时间、特殊建筑的其他缓存入口和完整合法性继续待补，当前缓存明确标记 `freshness=untracked`。候选列表已完成上述实机核对，默认升级缓存的实机核对待做。离线验证执行原生构造初始化和四个 getter，价格与工期计算及最终缓存写入保留原生代码，本地化说明与 GUI 展示使用桩。初始化、报价与工期、施工保存费用各完成 2,048 项检查，复查脚本为 `captures\economy-20261116\verify_building_economy.py`，结果为 `native_building_memory_validation.json`。

### 2026 年 11 月 16 日人物互动扩展

`send_character_interaction` 的共同写入函数为 `0x3087700`。原生定义 `+0x26f8` 的类型编号 6 对应婚姻，13 对应宣战，40 对应普通脚本互动。附加数据 token 表位于 `0x4748fe0`。婚姻附加对象使用 `CMarriageOffer` 的 vtable `0x44c9bf8`，通用 Write `0x3f90dc0` 调用空字段写入器 `0x854980`，线上因此保留 `arrange_marriage_interaction={}`。拟结婚双方位于共同数据 `+0x2e0/+0x2e4` 的 secondary_actor / secondary_recipient。

普通脚本互动赠金、招揽封臣和邀请入廷使用共同字段及作用域，类型 40 跳过附加对象。发送选项使用共同数据 `+0x300` 的布尔 vector，线上按总数及 true 索引稀疏写入。`interaction_catalog.py` 按安装脚本中的 `send_option` 顺序映射命名标记，生成相同顺序的布尔数组与事件目标。`ai_accept` 和 `ai_intermediary_accept` 保持 fixed64 编码，当前 `send_interaction` 要求调用方提供 `ai_accept`。

四类互动分别完成 256 次原生字节比较，共 1,024 次，另逐例验证解码后的重编码。婚姻覆盖六个安装脚本标记，招揽封臣覆盖八个标记的全部组合，邀请入廷覆盖三个标记。证据与复查脚本位于 `captures\model-runtime-20261116\native_interaction_validation.json` 和 `verify_interactions.py`。实际执行、选项顺序及动态接受值求解继续待验收。

### 2026 年 11 月 16 日当前施工工期与进度

施工记录位于省份 `+0x688`，其中 `+0x18` 是剩余基础时间 fixed64。`CConstruction` 的结束日期函数为 `0x24763a0`，进度函数为 `0x24761c0`，实际工期修正函数为 `0x2c33bd0`。结束日期采用当前游戏日期加上修正后的剩余天数，正数不足一天向上取整。进度按开工至当前的小时数除以开工至预计结束的小时数，再转成 0 至 100 的百分比。

地产修正所有者从省份 `+0x710` 取得。已有地产标记在所有者 `+0x628`，对应建筑时间修正 `0x1e0`，新地产使用 `0x1e5`。建筑定义 `+0x6f510` 提供地产类型，该类型 `+0xd8/+0xe0` 的 uint16 分别提供两种时间的额外修正编号。总乘数累加省份、伯爵领及受益角色缓存修正，最小值来自全局 `0x5c7ddf0`。类型无效时按原生行为回退到当前地产类型。

生产查询新增 `holding_buildings.construction.timing`。512 组结束日期和 512 组进度原生比对通过，覆盖建筑与地产、占领状态、承租和受益角色、修正下限、零剩余时间和正负临界值。结果位于 `captures\model-runtime-20261116\native_construction_timing_validation.json`，复查脚本为 `verify_construction_timing.py`。候选建筑费用和开工前工期的原生缓存入口已于 11 月 17 日接入，可用条件和界面核对继续待做。

### 2026 年 11 月 16 日事件与围城连续观察

事件目录在每次描述时使用该实例的作用域替换人物和头衔名称，静态模板缓存保留原文。`event_evaluation.py` 为已支持的常量、角色作用域存在性、布尔作用域和逻辑组合返回真、假或未知，条件详情通过 `condition_results` 提供结果。资源比较、脚本触发器、条件分支正文及复杂本地化函数继续保留未知状态。实际选项可用性与名称索引映射继续待原生验收。

围城 `projection` 使用通过内存一致性验证后的跨游戏日进度点样本，返回实测日均速度及最近速度延续时的预计剩余天数和日期。重复查询共享样本，失败或重试中的临时读取不提交样本。阶段、效果、主持军队、对象身份或世界变化以及时间回退会重建基线。原生瞬时速度仍涉及 `0x2487060` 的驻扎兵团计算和 `0x2640c70/0x30d4650` 的兵团属性，静态拆解保存在同目录 `siege_*_native.txt` 与 `regiment_stats_native.txt`，后续可继续还原。

### 2026 年 11 月 16 日围城进度换算

`Siege.GetProgress` 的回调为 `0xce1960`，调用 `0x25252a0` 取得 fixed64 比例并转成界面百分比。总进度点由 `0x2526600` 计算，使用省份 `+0x730` 的基础驻军、`+0x70c` 的驻军兵团、`+0x850` 的要塞等级，以及 `0x5c7e4e4` 的最少驻军、`0x5c7e690` 的每级要塞士气和 `0x5c7e698` 的基础士气。

驻军上限修正编号为 `0x1d8`。未被占领时叠加省份 `+0x30`、伯爵领 `+0x98` 及受益角色的缓存修正，被占领时采用省份修正。聚合表的键指针、长度和值指针在容器 `+0x68/+0x74/+0xd0`。头衔 `+0x12c` 为承租方头衔引用，原生函数读取其 `+0x128` 得到角色。256 组总进度与 256 组比例比对通过，复查脚本为 `captures\model-runtime-20261116\verify_siege.py`。

界面时间通过 `0x143b5c0` 调用 `0x25253e0`，按剩余进度和 `0x2527a50` 的每日速度向上取整。每日速度还涉及驻扎攻城器械、主将及双方角色修正，当前继续待接入。

### 2026 年 11 月 16 日地产建设

`CAddConstructionCommand` 的主 vtable 为 `0x477cd48`，Write/Read 为 `0x298f210/0x298f320`，token 为 `0x2d89`。对象 `+0x20/+0x24/+0x28/+0x2c` 依次是 character u32、province i32、slot index i32 和 building type i32。`CCancelConstructionCommand` 的主 vtable 为 `0x477cf08`，Write/Read 为 `0x298f680/0x298f710`，token 为 `0x2d8b`，包含 character 和 province。新增两条命令通过 512 次原生序列化比对。

建筑定义库指针为 `0x5c7c4c8`，其 `+0x50` vector 的顺序对应命令 `type`，原生查找为 `0x2992de0`。定义的 key 位于 `+0x18`，基础天数位于 `+0x58`。CHolding 位于省份 `+0x620`，Write 为 `0x246e4f0`。普通建筑 vector 在 holding `+0x10`，记录 16 字节，包含建筑类型指针和 `+8/+9` 的 disabled/ruined。施工记录位于 holding `+0x68`，其建筑、槽位、开工日期和角色偏移为 `+0/+8/+0xc/+0x70`。类型索引与普通槽位已运行原生函数核对。当前施工动态工期、保存费用及候选报价缓存已经接入，完整合法性继续待补。

### 2026 年 11 月 15 日军队与战报读取

目前支持 29 种命令。新增军队命令的序列化信息如下，RVA 绑定本文所列 CK3 可执行文件。

| 命令 | token | 主 vtable RVA | Write RVA | Read RVA |
| --- | --- | --- | --- | --- |
| raise_troops | `0x3216` | `0x45448e0` | `0x2999420` | `0x2999590` |
| move_unit | `0x27fb` | `0x477bda8` | `0x29766e0` | `0x2976a30` |
| disband_army | `0x2bae` | `0x477bb80` | `0x2977280` | `0x29772e0` |
| stop_gathering_army | `0x3472` | `0x477bc48` | `0x2977280` | `0x29772e0` |
| hire_mercenary | `0x31c2` | `0x477e608` | `0x29a10d0` | `0x29a1240` |

`raise_troops` 的 rally_points 记录由 province 与 color 两个 int32 组成。`move_unit` 使用 unit 引用与目标省份，当前编码器使用游戏寻路。`hire_mercenary` 的 character 在命令 `+0x20`，mercenary_company 在 `+0x24`，flags 在 `+0x28`，二进制写入时省略默认值 3。最新 8 轮验证共完成 232 次 Python 与原生序列化字节比较。

正常实时读取采用 Windows 进程外只读句柄，以下偏移相对数据库返回的主对象。

| 对象 | 数据库全局 RVA | ID 偏移 | 主 vtable RVA |
| --- | --- | --- | --- |
| combat_result | `0x5d34fb8` | `+8` | `0x4757750` |
| combat | `0x5d32e80` | `+8` | `0x474def8` |
| siege | `0x5d33c88` | `+8` | `0x474a5b8` |
| mercenary_company | `0x5d32f20` | `+0x10` | `0x47699c8` |

`combat_result` 的 province pointer 在 `+0x18`，winning_side 在 `+0x20`，start/end_date 在 `+0x2c/+0x34`，interested characters vector 在 `+0xb8`，双方结果子对象在 `+0xe8/+0x138`。结果子对象的 commander、main participant 在 `+8/+0xc`，initial/surviving soldiers 在 `+0x10/+0x18`，participant IDs vector 在 `+0x20`。winning_side 的 0/1 分别表示进攻方与防守方，角色胜败还需结合其参战方。

`combat` 的双方子对象在 `+0x20/+0x368`，子对象 vtable 为 `0x4756988`，Write 为 `0x26593e0`。军队 ID vector 在子对象 `+0x10`，leader/commander 在 `+0x70/+0x74`，fighting/initial/initial levy fixed64 在 `+0x98/+0xa8/+0xb0`。主对象的 phase 在 `+0x6b0`，phase_days 在 `+0x6b4`，province pointer 在 `+0x6b8`，combat_width 在 `+0x6c4`，advantage fixed64 在 `+0x6c8`。阶段天数在转换到 main 时归零，实机通知已核对。phase 的 0/1/2 分别为 maneuver/main/pursuit。fixed64 比例为 100000。

`mercenary_company` 的 culture/county/title 引用在 `+0x20/+0x24/+0x2c`，regiment IDs vector 在 `+0x30`，hired_by/hired_until 在 `+0x48/+0x4c`。雇佣价格与有效条件涉及角色修正，当前查询尚未重建计算。

地图邻接来自 province `+8` 的地图定义对象，其省份 ID 在 `+0x24`，邻接 vector 在 `+0x50`，每条记录 48 字节，crossing、目标省份、经过省份依次位于 `+0/+4/+8`。crossing 的 0/1/2/3 对应 normal/strait/river/large_river。国家边界在排除无主海域及荒地后聚合。未建造男爵领的 holder 为空时，仍沿事实领主解析其所属国家。

证据位于 `captures\army-control-20261115` 和 `captures\direct-memory-20261115\neighbors_validation.json`。当前顶部通知覆盖军事事件，围城查询、其他横幅类别和自行宣战的完整 interaction 编码继续研究。

目标是开发独立程序 B，完成 CK3 游戏级入局、角色选择、世界快照接收，并逐步支持发送玩家命令。

已实现独立 Python 进程的 Nakama 收发、PDX 外层编解码、Jomini 命令解析、24 种游戏命令编码、13 种控制消息编解码、本地身份、初始兼容性握手、玩家登记、串行及快速文件传输、分块校验、整包校验、二进制世界解析、SQLite 快照库、同步进度提取和离线通信回放。游戏命令完成 3,072 次原生字节对比，控制消息完成 1,664 次原生字节对比。86 项离线回归检查通过。

2026 年 11 月 14 日，独立 B 通过本机房间服务连接真实 CK3 房主，取得玩家编号 2，登记为 AIObserver，接收并解析完整世界快照，随后通过联机指令选中东法兰克国王路德维希并设置准备状态。房主回传及其实际玩家对象均确认选角和准备成功。测试结束时 B 已释放角色并正常退出。运行方式见本目录 `README.md`。

生活重心指令已实机执行。接收进度应答试验让 B 持续在线超过 700 个游戏日；该试验没有在 B 端模拟世界。大模型决策循环、持续世界状态更新和长期一致性仍需实现和验证。

2026 年 11 月 14 日晚，B 接入用户的多人存档，收到 `29,670,114` 字节、232 块的快照，解析为 `159,745,194` 字节世界。SQLite 入库日期为 `923.10.16`，包含 `89,398` 条人物记录、`13,269` 个省份、`19,809` 个有效头衔和 1 个已删除头衔槽位；收到的游戏命令独立留痕。B 选择赞吉王国国王 Juma（人物 `33588240`），房主回传确认选角和准备。个人摘要、头衔、指定人物字段和空增量查询均已通过该真实房间验证。

入局时房主曾发送 `pause_game_command {player:2,value:true}`。房主玩家解除自己的暂停只会发送 `{player:1,value:false}`，不会清除 B 的暂停。B 在完成世界加载后手工发送 `{player:2,value:false}`，房主回传确认，turn 继续增长。原型现在会在收到加载确认后自动解除这次加入暂停；自动路径已通过离线回归，下一次真实入局仍需验证。测试结束时 B 释放角色并正常断线，房主留在房间。

## 本机版本

| 项目 | 结果 |
| --- | --- |
| 安装目录 | `C:\Games\Crusader Kings III` |
| 启动器标记版本 | `1.20.1.2 (Crozier)` |
| 可执行文件大小 | `101125752` 字节 |
| 可执行文件 SHA-256 | `52c4678588c11ef6dd49c815198bc0108134d69699a30b90bc66f080268e8110` |
| Nakama DLL SHA-256 | `9e1892c3f35ee70a7f52be21d80607593fbc7c2bf9f85937cd9f38f95147ab46` |
| Nakama SDK 版本 | `2.4.1` |

游戏版本来自 `launcher/launcher-settings.json`。SDK 版本来自 DLL 的 `getNakamaSdkVersion` 导出跳转目标中的常量。下文 RVA 均相对于对应 PE 模块的加载基址。

分析程序读取安装文件。生成的脚本、依赖和证据保存在本调查目录。

## 可复用的公开 SDK

游戏通过 `nakama-cpp.dll` 的 C 接口完成实时通信。已在导入表和调用位置确认以下入口。

| 入口 | 本机可执行文件中的调用位置 RVA |
| --- | --- |
| `NRtClient_connect` | `0x3d52d34` |
| `NRtClient_joinMatch` | `0x3d54628` |
| `NRtClient_sendMatchData` | `0x3d54fe5` |
| `NRtClient_setMatchDataCallback` | `0x3d52cbe` |

已经找到与 DLL 版本一致的官方开源标签 `v2.4.1`，提交为 `0823d425fe8939a18e81e840dc096b319d8930fa`。该版本包含 C 接口头文件，可用于外部程序的函数签名和结构体定义。[实时客户端接口](https://github.com/heroiclabs/nakama-cpp/blob/v2.4.1/include/nakama-c/realtime/NRtClient.h)、[客户端工厂](https://github.com/heroiclabs/nakama-cpp/blob/v2.4.1/include/nakama-c/ClientFactory.h)、[会话接口](https://github.com/heroiclabs/nakama-cpp/blob/v2.4.1/include/nakama-c/NSession.h)

可执行文件同时包含 Steam 网络和 Nakama 网络实现。本次实机使用 Nakama 后端，通过本机服务完成认证、建房、独立 B 入局及游戏消息收发。

## 本机自建房间与自定义后端地址

用户已明确将目标调整为完全离线、本机通信。游戏内创建及加入房间流程应接入自建服务，地址和端口可配置，使用本地玩家身份。

本机原生函数 RVA `0x3d73fc0` 读取后端命令行配置。

| 参数 | 目标值示例 | 读取后的结构偏移 |
| --- | --- | --- |
| `nakama_key` | 自建服务的客户端 key | `0x00` 的字符串 |
| `nakama_host` | `127.0.0.1` | `0x20` 的字符串 |
| `nakama_port` | `7350` 或自选端口 | `0x40` 的整数 |
| `nakama_ssl` | `0` | `0x44` 的布尔值 |

函数首先逐一检查四个参数是否存在，任意一项缺失都会进入默认配置分支。端口与 SSL 字符串通过整数转换处理，SSL 在整数非零时开启。

已使用 Unicorn 执行该原生函数并读取输出，也执行原生参数扫描函数 `0x3f65cb0` 和修剪逻辑 `0x3f661e0`。`-name=value`、`--name=value`、端口 `7350`、`19735` 和 SSL 值 `0`、`1` 的 8 组参数检查通过。真实游戏已使用 `-nakama_key=ck3-local -nakama_host=127.0.0.1 -nakama_port=7350 -nakama_ssl=0` 连接本机服务。

主菜单 `frontend_main.gui` 的创建及加入按钮分别受 `StatusIsLoggedIn`、`StatusIsUserNameEmpty` 和 `JominiIsMultiplayerAccessible` 控制。未登录时显示的同名按钮调用 `FrontEndMainView.OnLogin`。已登录分支调用 `OnHost` 和 `OnMultiPlayer`。

原生可用性函数 RVA `0x380aea0` 同时检查网络后端状态、游戏校验状态、账号状态和用户名。原生 Nakama 登录入口 `0x3d63a40` 还检查待处理的凭据。`local_game_bridge.py` 与配套运行时脚本接通本地身份及 Nakama 会话，并在运行期间切换菜单入口，游戏退出后恢复界面文件备份。

游戏使用 RPC `create_jomini_match` 和 `list_matches` 管理房间。创建回包处理函数 `0x3d64fe0` 读取 `match_id` 后继续连接。列表回包处理函数 `0x3d65bc0` 读取 `matches` 对象并迭代以房间 ID 为键的项目。`local_nakama.py` 已提供本地认证、房间 RPC 和定向消息中继，实机已完成主菜单建房、选角进入对局及独立 B 加入。房间列表和游戏内加入页面的完整人工操作流程仍需单独验收。

建房后启动器自动导出 `session_profile.json`，包含本地服务地址、房间、房主、可执行文件哈希及当前会话的兼容性字节。`ck3_client.py connect-local --session-profile PATH --duration 0 --quiet --stdin` 读取该文件完成本地认证、握手、玩家登记、世界接收和载入确认。

已核对两个公开项目。`Multiplayer Stability Improved` 调整游戏速度、事件和界面等同步相关内容。`CK3MPS` 提供联机环境检查及 parity-room 数据交换。当前检索范围内尚未找到可验证的 CK3 自定义 Nakama 后端及完全离线建房现成 Mod。[Multiplayer Stability Improved](https://steamcommunity.com/sharedfiles/filedetails/?id=3669815510)、[CK3MPS](https://github.com/Danissemo/CK3MPS)

## 从二进制确认的通信层次

1. Nakama 实时房间和 MatchData 消息。
2. PDX Nakama 包装层，附带发送序号与来源用户字段。
3. PDX 网络帧，包含接收目标和多个子消息。
4. Jomini 会话与游戏命令，包括玩家登录、选角和文件传输。

### Nakama 操作码

发送路径在 RVA `0x3d67710` 设置操作码 `255`。接收监听器在 RVA `0x3d68f80` 所在函数识别该值，再调用 `0x3d67a50` 的包装层接收处理。

这些操作码只描述当前找到的游戏数据路径。其他操作码的用途和完整会话协议还需继续核对。

### PDX 包装层尾部

发送构造函数 RVA 为 `0x3d678c0`。当前正常游戏数据发送路径追加以下结构。

| 尾部内偏移 | 长度 | 静态分析结果 |
| --- | --- | --- |
| `0` | `2` | 小端整数 `64`，用于核对尾部结构大小 |
| `2` | `6` | 填充区，含义未确定 |
| `8` | `8` | 小端数据包序号 |
| `16` | `48` | 来源用户的字节字符串区 |
| `64` | `1` | 尾随零字节 |

接收函数 `0x3d67a50` 从负载末端定位结构，检查大小字段，并使用序号进行去重和重连同步。本次握手、串行快照及玩家指令的真实负载均通过该外层解析，其他发送路径仍需分别核对。

### PDX 网络帧

帧重置函数 RVA 为 `0x3d47be0`。头部大小为 48 字节。

| 帧内偏移 | 长度 | 静态分析结果 |
| --- | --- | --- |
| `0` | `2` | 小端魔数 `0xCAFE` 或 `0xBCA5` |
| `2` | `2` | 子消息数量 |
| `4` | `4` | 帧总字节数，包含 48 字节头部 |
| `8` | `40` | 接收目标字符串区 |

接收路径 `0x3d49480` 检查魔数。`0xCAFE` 路径包含接收目标核对。`0xBCA5` 路径直接进入子消息处理，完整路由语义仍待实测。

每个子消息从以下结构开始。

| 子消息内偏移 | 长度 | 静态分析结果 |
| --- | --- | --- |
| `0` | `4` | 后续记录长度，数值等于有效负载长度加 8 |
| `4` | `4` | 未完全确定的字段，已找到的普通发送函数写入 `5` |
| `8` | `4` | 未完全确定的字段，接收端将其交给上层回调 |
| `12` | 可变 | 子消息有效负载 |

记录构造函数为 `0x3d47c70`，记录解析函数为 `0x3d49400`。发送函数具有 1 MiB 帧缓冲上限，并会按大小或调用参数触发发送。

Jomini 回调 `0x3807ab0` 已确认 `word_8` 的两个分发值。`0` 进入命令反序列化函数 `0x37fe630`，`1` 进入方向相关的会话控制消息分发。原型保留原有字段名，并在解析结果中增加 `channel`。`word_4` 的完整语义仍待确认。

## Jomini 游戏命令编码

命令发送路径 `0x381a7a0` 在 `0x381a98f` 调用类型 getter，写入类型 token 和等号，再调用命令的 `Write`。该路径在 `0x381a9f3` 交给连接处理器，连接处理器最终将相同字节交给 PDX 网络上下文。接收路径 `0x3807ab0` 在 `0x3808bac` 调用命令反序列化器 `0x37fe630`。

`CWriter` 的二进制路径使用小端 token 和带类型的数值。相关函数的实际输出已通过 CPU 模拟器比对。

| 编码 | 布局 | 写入函数 RVA |
| --- | --- | --- |
| 标识符 | `u16 token` | `0x3f9b9d0` |
| 等号、开始对象、结束对象 | token `1`、`3`、`4` | `0x3f9b9d0` |
| 有符号整数 | `u16 0x000c` + `i32` | `0x3f9bc60` |
| 无符号整数 | `u16 0x0014` + `u32` | `0x3f9bd10` |
| 单精度浮点数 | `u16 0x000d` + `f32` | `0x3f9bb70` |
| 64 位无符号整数 | `u16 0x029c` + `u64` | `0x3f9bdc0` |
| 定点数 | `u16 0x0167` + `i64`，比例为 `100000` | `0x3f9bf20` |
| 布尔值 | `u16 0x000e` + `u8` | `0x3f9be70` |
| 字符串 | `u16 0x000f` + `u16 字节数` + 原始字节 | `0x3f9bfc0` |
| 二进制数据字段 | `u16 0x00f0` + `u16 0x01fc` + `u32 字节数` + 原始字节 | `0x3f9c360` |

二进制数据字段由原生函数直接写入 `data` token 与二进制标记。编解码器按该结构处理其长度及内容。

本版本内置 token 表位于 RVA `0x46fcbf0`，提取出 8,764 个 ID 和名称。提取脚本使用可执行文件 SHA-256 和准备指令锚点核对版本。

命令公共写入函数 `0x37fdf10` 生成以下结构。标识符通过上述表映射为数值 token。

```text
COMMAND_TYPE = {
    default_state = {
        identity = UINT32
        sender = INT32
        receiver = INT32
        attribs = UINT32
    }
    object = { COMMAND_FIELDS }
}
```

准备、选角与快速文件传输命令如下。串行文件传输和其他玩家命令见后文。

| 类型 | token | 特有字段写入 RVA | 特有字段读取 RVA |
| --- | --- | --- | --- |
| `set_ready_command` | `0x6a6` | `0x368d4b0` | `0x368d540` |
| `set_character_controller` | `0x6de` | `0x368b3c0` | `0x368b490` |
| `filetransfer_fast_cmd_request_handshake` | `0x618` | `0x3832f40` | `0x3834d60` |
| `filetransfer_fast_cmd_handshake` | `0x619` | `0x3835420` | `0x3835480` |
| `filetransfer_fast_cmd_initialize` | `0x61a` | `0x3832f40` | `0x3832f50` |
| `filetransfer_fast_cmd_chunk` | `0x61b` | `0x38341c0` | `0x3834230` |
| `filetransfer_fast_cmd_chunkstatus` | `0x61c` | `0x3836410` | `0x3836450` |
| `filetransfer_init_command` | `0x6a0` | `0x368bab0` | `0x368bba0` |

准备指令体含 `machineid` 的 `i32` 和 `status` 的布尔值。选角指令体含 `machineid` 的 `i32`、`playable` 的 `u32`。`leave_ai_on` 为真时追加布尔字段。

文件传输公共字段写入函数为 `0x3832740`，读取函数为 `0x38326d0`。字段为字符串 `filename`、`u32 size` 和 `i32 checksum`。握手追加 `u32 filetransfer_fast_fld_chunk_size`，分块消息追加 `i32 chunk_id` 和原始二进制数据。

快照协商、分块确认和整包 checksum 已按后文实现。实机的串行传输及世界解析已通过，快速协议通过本地合成对端验证。

## 快照传输协商与分块确认

本次真实房主建立 `CJominiSerialFileTransfer`。已实现并验证以下串行流程。

```text
A → B  filetransfer_init_command
B → A  filetransfer_request_chunk_metas_command
A → B  filetransfer_send_chunk_metas_command
B → A  filetransfer_request_chunk_command
A → B  filetransfer_send_chunk_command
B → A  filetransfer_chunk_recieved_command
重复请求与确认，直至收到全部分块
```

| 串行命令 | token | 虚表 RVA | 特有字段写入 RVA |
| --- | --- | --- | --- |
| `filetransfer_request_chunk_metas_command` | `0x6a1` | `0x4954d00` | `0x3832f40` |
| `filetransfer_send_chunk_metas_command` | `0x6a2` | `0x4954d98` | `0x38334b0` |
| `filetransfer_request_chunk_command` | `0x6d6` | `0x4954e30` | `0x3833df0` |
| `filetransfer_send_chunk_command` | `0x6d7` | `0x4954ec8` | `0x38341c0` |
| `filetransfer_chunk_recieved_command` | `0x6d8` | `0x4954f60` | `0x38345f0` |

`recieved` 保留游戏线上命令的原有拼写。元数据为公共文件字段、`i32 checksums[]`、`u32 sizes[]` 和 `i32 machineid`。请求包含 `i32 chunk_id`，数据回复包含块编号及原始字节。确认包含公共字段、`i32 machineid`、`i32 chunk_id`、`u32 chunks_completed` 和 `u32 chunks_total`。

接收器一次请求一块，先验证块长度和 checksum 再确认，元数据或分块在一秒未收到时重试。本次共接收 70 块，常规块大小 128000 字节。各块元数据长度之和必须与声明文件长度一致。

前两次实机崩溃来自原型向串行传输发送了快速握手。两份转储均显示 `0xc0000409`、fast fail 7 和故障 RVA `0x42426a9`。调用链 `0x3834933 → 0x3825d30 → 0x382623f` 进入字符串复制及异常大小分配，快速处理函数将串行对象的 `+0x58` 位置解释为字符串。当前 `connect-local` 默认使用串行协议，后续完整传输、选角和准备已通过实测。

快速传输的接收流程由命令执行函数和快速传输管理器还原，现有合成测试覆盖该路径。

```text
A → B  filetransfer_init_command
B → A  filetransfer_fast_cmd_request_handshake
A → B  filetransfer_fast_cmd_handshake
B → A  filetransfer_fast_cmd_initialize
A → B  filetransfer_fast_cmd_chunk
B → A  filetransfer_fast_cmd_chunkstatus
A → B  按位图补发缺失分块
B → A  完整位图
```

| 原生入口 | RVA |
| --- | --- |
| 初始传输通知执行 | `0x368b8d0` |
| 请求握手执行及处理 | `0x38347c0`、`0x3825d30` |
| 握手执行及处理 | `0x3834e60`、`0x38262c0` |
| 初始化执行及处理 | `0x38354e0`、`0x38266b0` |
| 分块执行及写入 | `0x3835a70`、`0x3826aa0` |
| 状态反馈执行及处理 | `0x38360a0`、`0x3826cf0` |
| 接收端更新 | `0x3827480` |
| 发送端更新 | `0x38270b0` |
| 位图已收数量统计 | `0x3827ea0` |

分块计数由 `ceil(size / chunk_size)` 得到。原生构造器的默认块大小为 `1024`，接收器使用线上握手值。`0x3826c9b` 至 `0x3826cc3` 的原生指令设置已收位图，布局为 `bitmap[id >> 3] |= 1 << (id & 7)`。位图长为 `ceil(chunk_count / 8)`。

接收更新函数使用 RVA `0x4a07b68` 的双精度常量 `0.3` 作为心跳间隔。构造回复时属性初值为 `0x0f`，`PostCommand(14)` 经 `0x3803240` 追加 `0x20`，形成线上属性 `0x2f`。此路径从会话成员 `+0x3ec` 填写发送机器编号。

`transfer_protocol.py` 等待本机编号后发送请求，按来源 presence 定向回复，反馈部分和完整位图。`snapshot_transfer.py` 按来源用户及文件元数据隔离接收状态。协商后的非法长度和越界块在写入前被拒绝，原始网络负载保留。

## 线上世界容器与整包 checksum

热加入构造函数 `0x369a670` 先通过游戏适配器构造世界，再生成条目名为 `hotjoin_%d` 的 ZIP。`0x369a939` 调用 ZIP 条目构造函数 `0x39ac020`，压缩方法参数为 `8`。ZIP 结束标记为 `0x06054b50`，最终容器进入文件传输管理器。

`0x369aa62` 至 `0x369aada` 计算热加入数据校验值，串行传输的通用文件校验函数 `0x382fac0` 使用同样算法。两个原生入口均已在 CPU 模拟器中与 Python 实现对照。真实传输确认，分块 checksum 对应线上 ZIP 字节，热加入公告中的整体 checksum 对应 ZIP 内 `hotjoin_0` 的解压内容。

```text
first = 0
second = 0
for byte in checked_bytes:
    first += byte
    if first > 65535:
        first -= 65535
    second += first
    if second > 65535:
        second -= 65535
checksum = (first << 16) | second
```

值等于 `65535` 时原生保留该值。高 16 位存放第一个和，结果通过 `i32` 字段传输。`TransferChecksum` 支持按接收顺序重组后的分块增量计算。

重组输出包含声明和计算的 checksum、SHA-256、原始文件名及来源用户。`verify_transfer_checksum` 先检查整体传输字节，热加入 ZIP 在该值不匹配时进一步核对同名 ZIP 条目的解压内容。事件分别记录 `transport_checksum`、`computed_checksum`、`checksum_scope` 和 `checksum_verification`。只有 checksum 通过的重组文件会自动进入后台世界解码队列。校验失败的完整字节保留在捕获目录，整包失败后的自动重传恢复仍需实现。

本次线上 ZIP 的 checksum 为 `1062022225`，公告值为 `-768910587`，后者与 `hotjoin_0` 解压内容一致。首次接收日志保留修复前的整包校验失败记录，随后出现 `transfer_checksum_reverified` 和 `world_snapshot_decoded`。当前源代码通过全部 402 个真实接收帧的离线回放，自动重组、ZIP 条目校验及后台解码均成功。

`world_snapshot.py` 读取原始二进制世界、ZIP 的 `gamestate` 或 `hotjoin_*` 条目以及 SAV 包装，通过记录索引按需查询人物、省份和头衔。原生定点文本格式函数 `0x3f7fea0` 确认比例为 `100000`。日期按每年 365 天、每小时一个单位和 5000 年偏移换算。

辅助格式验证读取同版本本地存档的 101,693,418 字节二进制世界，得到 79 个顶层段名、36,451 名在世人物、15,305 名保留的死亡人物、13,269 个省份和 19,018 个头衔。人物 16933 Eudes 的生日、技能和头衔与同日期文本存档核对一致。本次真实网络世界的内容与校验结果见后文。

## 会话控制消息

`word_8 = 1` 时，子消息使用独立的控制消息结构。分发函数 `0x3807ab0` 通过连接角色选择解释表，再读取首字节的类型 ID。

| 偏移 | 长度 | 含义 |
| --- | --- | --- |
| `0` | `1` | 方向相关的消息类型 |
| `1` | `4` | 小端消息总长度 |
| `5` | 可变 | 消息体 |

客户端请求类型表位于 `0x3808c6c`，包含 `ClientRequestHandshake`、`ClientRequestConnected`、`ClientRequestDisconnect`、`ClientRespondPassword`、`ClientRespondAddPeer` 和 `ClientRespondDisconnect`，类型依次为 `1` 至 `6`。

服务端消息表位于 `0x3808c88`，包含 `ServerRequestPassword`、`ServerRequestAddPeer`、`ServerRequestRemovePeer`、`ServerRequestDisconnect`、`ServerRespondHandshake`、`ServerRespondConnected` 和 `ServerRespondDisconnect`，类型依次为 `1` 至 `7`。`ServerRespondHandshake` 的消息体首个 `u32` 在 `0x38087fe` 读取并保存为结果码。

`ClientRequestHandshake` 接收处理位于 `0x3801780`。发送入口位于 `0x3800100`，打包函数为 `0x380ca70`。消息体依次包含 `u32` 名称字节数、含 NUL 的名称、16/16/33 字节的三个兼容性字段、一个字节标志和 `u32` 平台标识。空名称由原生构造路径写为零长度。默认平台常量为 `0x4e4d5453`。

三个兼容性字段依次来自会话对象的字符串成员 `+0x1e8`、`+0x208` 和 `+0x228`。字段实际生成来源仍待追踪。编码器保留原始字节，不为这些值赋予未经确认的含义。

控制消息的前五字节为紧凑头部，完整发送长度取自消息自己的虚函数。该长度包含写入器未赋值的尾部区域。例如头部型消息原生长度为 16，单 `u32` 消息为 24。默认编码对尾部填零，解析时保留原字节。

| 控制消息 | 原生总长度 | 消息体字段 |
| --- | --- | --- |
| `ClientRequestHandshake` | `90 + 名称字节数` | 名称、三个兼容性字段、字节标志、平台 |
| `ClientRequestConnected` | `16` | 头部型 |
| `ClientRequestDisconnect` | `24` | `u32 reason` |
| `ClientRespondPassword` | `36 + 名称字节数` | 名称、16 字节密码摘要 |
| `ClientRespondAddPeer` | `16` | 头部型 |
| `ClientRespondDisconnect` | `24` | `u32 reason` |
| `ServerRequestPassword` | `16` | 头部型 |
| `ServerRequestAddPeer` | `32` | 两个 `u32`、一个字节标志 |
| `ServerRequestRemovePeer` | `32` | 三个 `u32` |
| `ServerRequestDisconnect` | `24` | `u32 reason` |
| `ServerRespondHandshake` | `24` | `u32 result_code` |
| `ServerRespondConnected` | `16` | 头部型 |
| `ServerRespondDisconnect` | `24` | `u32 reason` |

上表名称字节数包含末尾 NUL，原生空名称使用零。密码响应的打包函数为 `0x380cd70`，删除 peer 的打包函数为 `0x380cf00`。密码摘要算法及其输入来源仍待还原。

`ServerRequestAddPeer` 接收函数 `0x3800db0` 在偏移 `5`、`9` 和 `13` 读取两个 `u32` 和字节标志。标志非零时，首个编号通过 `0x37fe810` 写入会话成员 `+0x3ec`。原型据此整理 `local_machineid`，第二个 `u32` 仍以 `peer_value` 原值保留。

该函数在 `0x3800ef5` 构造 `ClientRespondAddPeer`，随后检查分配本机标志。标志非零时在 `0x38010c6` 再构造 `ClientRequestConnected`。`session_responses` 已按相同次序自动回复来源 presence。执行完整原生处理函数的 128 次检查验证两种分支的回复字节、顺序及本机编号写入，连接列表维护和日志通过桩处理。实机 B 从房主导出的会话配置读取初始握手字节，收到结果码 2、编号分配及 `ServerRespondConnected`，随后登记玩家成功。兼容性字节的独立生成仍需追踪。

`ClientRespondDisconnect` 的字段在现有 API 中命名为 `reason`。原生 `0x38014a7` 将本机机器编号写入这个位置，该字段的统一命名与其他断线路径仍需进一步核对。

## 玩家同步与游戏行动的新增结构

| 命令 | token | 体写入函数 | 体读取函数 |
| --- | --- | --- | --- |
| `sync_players_command` | `0x6a5` | `0x368d170` | `0x368d1e0` |
| `add_player` | `0x69f` | `0x36893d0` | `0x36894c0` |
| `set_player_here_command` | `0x2a9` | `0x368a5f0` | `0x368a650` |
| `set_observer_command` | `0x5bf` | `0x368e7b0` | `0x368e840` |
| `add_perk` | `0x3350` | `0x28981b0` | `0x28982f0` |
| `pause_game_command` | `0x2e59` | `0x29946c0` | `0x2994750` |
| `set_speed` | `0x29ed` | `0x29943a0` | `0x2994400` |

玩家同步体含 `machineid`、`players`、`character`、`flags` 四组并行数组。写入顺序按此排列，空数组省略。前三组分别使用 `u32`、字符串和 `u32`，标志在原生对象中按字节存储，线上仍逐项写为 `u32`。数组使用匿名对象成员，保留通用解析树，同时向调用方提供普通 JSON 数组。

执行函数 `0x368cd30` 按相同下标关联编号、名称、可玩对象和标志，并将 `character` 数值传给角色控制命令的 `playable` 成员。接收侧整理成玩家列表，出现数组长度不一致或重复编号时保留上一份有效列表。实机确认人物 11190 可直接用于本次角色控制指令。`set_player_here_command` 设置标志掩码 `0x40`，`set_ready_command` 设置或清除 `0x02`，其余标志位仍需还原。

`add_perk` 对应 `CAddLifestylePerkCommand`。体字段依次为 `u32 target` 和字符串 `perk`，源对象成员为 `+0x20` 的人物引用，以及 `+0x28` 的 perk 数据库对象指针。写入器从数据库对象 `+0x18` 的脚本名称生成字符串。原生验证构造含正确类型标记和名称的对象，直接执行原写入器，Python 运行时使用脚本名称生成同样字节。

暂停命令体为 `i32 player` 和 `bool value`，速度命令体为 `i32 speed`。这些编码已经接入 JSON 指令入口，游戏对人物控制权、技能条件、暂停权限和速度范围的实际执行仍需互操作验证。

## 入局和快照传输的具体线索

已经在本机可执行文件中找到以下注册名称。

| 过程 | 标识 |
| --- | --- |
| 入局处理 | `join_server_message` |
| 登录结果 | `join_game_login_accept`、`join_game_login_decline` |
| 密码请求 | `join_game_password_required` |
| 玩家同步 | `sync_players_command` |
| 角色控制 | `set_character_controller` |
| 准备与开局 | `set_ready_command`、`start_game_from_lobby` |
| 排队传输存档 | `queue_save_transfer` |
| 热加入 | `accept_hotjoin_command`、`hotjoin_sync_command` |

文件传输至少包含快速与串行两条实现。已定位到快速传输相关类型及其虚表。

| 类型 | 虚表 RVA |
| --- | --- |
| `CJominiFastFileTransferRequestHandshake` | `0x4954ff8` |
| `CJominiFastFileTransferHandshake` | `0x4955090` |
| `CJominiFastFileTransferInitiate` | `0x4955128` |
| `CJominiFastFileTransferChunkData` | `0x49551c0` |

对应标识包括 `filetransfer_fast_cmd_request_handshake`、`filetransfer_fast_cmd_handshake`、`filetransfer_fast_cmd_initialize`、`filetransfer_fast_cmd_chunk` 和 `filetransfer_fast_cmd_chunkstatus`。

串行路径的元数据请求、分块请求、发送和确认已实现，命令结构和实机验证见前文。

上表包含从通用 token 表找到的名称。`join_server_message`、`join_game_login_accept`、`join_game_login_decline` 已定位到 GUI 事件投递路径。实际会话控制消息的类型及结构见前文。24 种已实现命令的类型 ID 和字段顺序已完成原生字节比对，其余消息的语义与状态转换仍需继续还原。

本次真实 MatchData 捕获包含添加玩家、设置角色控制器、准备、载入确认及断线消息，结合房主实际玩家对象完成独立 B 入局及控制验证。

## 已实现的原型

`ck3_client.py` 通过 `ctypes` 调用游戏随附的 Nakama C API，支持导入会话 JWT、本地认证、实时连接、加入房间、接收及定向发送 MatchData。收到的 opcode `255` 逐层交给 PDX 和 Jomini 编解码器。逐行 JSON 指令支持 `send_game_command`，已实现命令直接由结构化字段生成游戏消息字节。`connect-local` 自动握手、登记玩家并在世界解码成功后发送载入确认，各类自动指令与传输指令共用递增的 identity。

`snapshot_transfer.py` 提取已解析分块命令中的二进制数据，保存乱序块，检查相同编号重传内容，满足连续编号和声明长度后重组并校验文件。`transfer_protocol.py` 在串行模式发送元数据请求、块请求和逐块确认，在快速模式发送握手请求、初始化和接收位图。`world_snapshot.py` 在后台解析通过校验的世界快照，`world_store.py` 在同一后台流程中原子导入 SQLite，按人物、省份和头衔查询。原始快照仍用于未索引路径；命令只留痕，不直接改写世界状态。

`pdx_nakama_codec.py` 接受从 Nakama `MatchData.data` 取出的完整字节负载，解析外层结构，并保留内部 Jomini 消息的原始字节。

```powershell
py -m ck3_llm.protocol.pdx_nakama_codec self-test

py -m ck3_llm.protocol.pdx_nakama_codec decode payload.bin
```

自检覆盖两种魔数、多条消息、空消息列表、包序号、来源字段，以及截断和非法记录长度。结果均通过。真实房主的 402 个接收帧另已通过离线回放，重组一份完整世界并更新选角及准备状态。

`verify_jomini_native.py` 在 Unicorn 中执行本机游戏的真实命令写入函数、公共写入函数及数值写入函数，用输出流桩收集实际字节。24 种命令的 3,072 次对比均一致。生活重心与决议的数据库引用执行原生命名对象写入器 `0x38451d0`，决议选项执行原生名称池读取、作用域列表及值写入路径。名称池、作用域注册表和线程局部数据使用离线构造的对象。

`verify_session_native.py` 另完成 13 种控制消息的 1,664 次比对。握手、密码响应和删除 peer 使用完整原生发送函数，其余字段形状执行头部型、单 `u32` 型和双 `u32` 加字节型的原生内联写入布局，并使用各类型自己的长度函数。内存复制、栈探测及传输回调通过模拟器桩处理，尾部未赋值区域清零。实机会话另验证了无密码握手、玩家编号分配、连接确认和正常断线。

`verify_world_native.py` 完成 192 项数值写入、64 项定点文本、64 项位图写入、64 项位图计数、64 项文件 checksum 和 64 项热加入 checksum 检查。checksum 用例覆盖空数据、全零数据、恰好达到 `65535` 的边界和跨分块更新。

`demo --transfer-protocol` 已分别通过 Protobuf 和 JSON 实时协议的本地测试。测试对端等待玩家注册应答，再协商快照、倒序发送并省略一块，根据部分位图补传，收到完整位图后结束。接收器输出合成世界中的人物 12345 和头衔 `c_fixture`。原始包、校验结果和世界观察保存在每次运行的捕获目录。

## 2026 年 11 月 14 日真实游戏验证

本次只有一个真实 CK3 进程 A，独立 Python 程序 B 经本机 Nakama 服务连接。用户使用 867 年开局，网络快照日期为 `868.4.1`。

| 项目 | 实际结果 |
| --- | --- |
| B 捕获目录 | `captures/connect-local-20261114-120927-28ebe5f0` |
| 压缩快照 | `transfers/58d3fab6d67a1830af7848b2/received.bin` |
| ZIP 大小 | `8,913,658` 字节，共 `70` 块 |
| ZIP SHA-256 | `e9df1ac4419e234cd6c0dc79134b8ad53170652a21bf8ba34996fd9bb6258dc7` |
| ZIP 条目 | `hotjoin_0`，解压大小 `60,181,746` 字节 |
| 去掉 SAV 包装后的世界 | `60,181,722` 字节 |
| 在世及保留死亡人物 | `31,062` 和 `11,530` |
| 省份及头衔 | `13,269` 和 `18,452` |
| 房主玩家 | LocalPlayer，编号 `1`，人物 `12803` Charles |
| B 玩家 | AIObserver，编号 `2` |
| B 选角 | 东法兰克国王 Ludwig，人物 `11190` |
| 人物观察 | 同次捕获目录的 `ai_observation.json` |

B 在完整世界解码后发送 `set_player_here_command`、`set_character_controller` 和 `set_ready_command`，三条命令均收到房主回传。只读检查房主实际玩家对象得到 `LocalPlayer machineid=1 flags=215 character=12803` 与 `AIObserver machineid=2 flags=71 character=11190`。

用户要求继续正常游玩后，B 发送 `playable=4294967295`、`leave_ai_on=true` 释放角色，再完成原生断线流程并正常退出。房主实际玩家列表只剩 LocalPlayer。当前测试记录没有确认退出后的游戏暂停状态。

## 已保存通信中的后续指令

对本次 `402` 个接收帧进行离线统计，共有 `1,719` 条游戏命令，包含 `58` 个命令名称，另有 `5` 条会话控制消息。通用 Jomini 解析器已经输出行动字段，以下三种行动的原生读写函数也已通过文件静态分析定位。

| 行动 | token | 实际接收的字段 | 写入 RVA | 读取 RVA |
| --- | --- | --- | --- | --- |
| 生活重心 `set_character_focus` | `0x2c3c` | `u32 target=12803`、字符串 `focus_type=martial_strategy_focus`、`u32 actor=12803` | `0x2897ba0` | `0x2897c90` |
| 决议 `execute_decision` | `0x2e64` | `u32 character=29880`、字符串 `decision=hold_mystical_communion_decision` | `0x289a310` | `0x289a4b0` |
| 事件选择 `select_event_option` | `0x27d4` | `i32 data=4`、`i32 option=0` | `0x29cafa0` | `0x29cb030` |

生活重心样本位于 `000494-rx.bin`，决议位于 `000054-rx.bin`，事件选择位于 `000081-rx.bin`。三种行动已接入 `send_game_command`，抓包中的 1 条生活重心、13 条决议和 3 条事件选择共 17 条消息均可重新编码成完全相同的字节。生活重心和 NPC 事件选择已通过实机执行与实际状态检查，决议仍需实机验证。

决议还支持可选 `puppeteer`，其值为 `4294967295` 时省略。`parameters` 已支持具名布尔 `event_targets`，覆盖 `000314-rx.bin` 中的 `pay_homage_decision`，其余 12 条决议使用普通字段。布尔值的 `identity` 按 `u64` 写入，零值省略，作用域 `type` 使用 token `0x500`。作用域列表写入函数为 `0x26202e0`，单个作用域写入为 `0x88ad30`。其他作用域类型和 `local_variables` 仍需还原。

`hotjoin_sync_command` 在 `000012-rx.bin` 中携带 `tick=316909`、`pause_game=false`、`host_has_started_game=true` 和编号列表 `[2]`。首条 `client_ping` 位于 `000053-rx.bin`，其 `turn=316909`、`machineid=1`，与热加入 tick 相同。

捕获中 `daily_tick` 与 `event_tick` 各出现 `36` 次，已解析的命令体均为空对象。持续世界更新还需要结合这些命令的原生执行路径、时间队列及游戏模拟语义还原。后续同步实现应将初始快照、热加入 tick 和有序命令流关联，核对每个状态更新的起点。

`SynchronizationState` 已接入正常接收入口，记录热加入公告、各玩家的 `client_ping.turn` 和公告之后的 tick 接收计数。实机捕获回放得到起始 tick `316909`、房主最后报告 turn `316943`、公告之后 `35` 次 daily tick 和 `35` 次 event tick。首条 daily/event tick 出现在热加入公告之前，因此统计总数各为 `36`。这些字段可作为后续模拟与命令队列的对照。

`client_ping` 的原生执行函数 `0x2994500` 按 `machineid` 找到玩家对象，将 `turn` 存入玩家对象 `+0x74`。默认配置 `game/common/defines/00_defines.txt` 设置 `LAG_DECREASE_SPEED_DAYS=15` 和 `LAG_PAUSE_DAYS=30`，分别对应客户端落后时降速和暂停。虚拟客户端的进度上报、实际世界执行及校验需要联合验证，先前等待状态的具体触发路径仍需核对。

`ck3_client.py replay CAPTURE --quiet` 已提供可复用的离线入口，按保存的原始接收顺序和时间重走正常解析、重组及世界解码。完成事件统计协议解析、快照 checksum 和世界解码错误，任一阶段失败都会返回失败状态。该入口另外覆盖了捕获文件被修改及非法世界内容的回归检查。

## 独立 B 后续需要实现的部分

1. 还原热加入结束、命令排队、时间推进和校验一致性的完整行为，验证 B 持续在线时房主能正常运行。
2. 原生主机内存快照已提供按需刷新的完整世界，继续扩展动作结果、事件选择与决策调度。
3. 继续验证决议和更多事件选项的执行效果，扩展决议参数、外交和军队行动。
4. 接通大模型观察、决策和结构化指令提交循环，记录命令执行反馈。
5. 追踪兼容性字节的独立生成及密码摘要，完成游戏内加入页面的人工验收和整包失败自动恢复。

自动人物观察默认选取 `currently_played_characters` 的首个人物，可用 `--character` 指定 B 控制的角色。初始世界、选角和准备已通过真实房主验证，持续对局仍需后续测试。

## 证据和复现

| 文件 | 用途 |
| --- | --- |
| `local_game_bridge.py`、`local_game_bridge.js` | 本地身份、菜单回调和房主会话配置导出 |
| `local_nakama.py` | 本机认证、房间 RPC 与消息中继 |
| `ck3_client.py` | 独立客户端入口、事件捕获、逐行 JSON 指令与离线回放 |
| `inspect_network.py` | 读取 PE 导入、导出、哈希和网络字符串 |
| `network_inventory.json` | 本次版本对应的提取结果 |
| `trace_network.py` | 根据导入、字符串、RTTI 和展开信息定位代码 |
| `code_references.json` | 引用位置和虚表结果 |
| `pdx_nakama_codec.py` | 外层负载编码、解码和自检 |
| `session_codec.py` | 控制消息结构化编码和解析 |
| `session_state.py` | 接收到的会话编号、peer、玩家名单与同步进度 |
| `transfer_protocol.py` | 串行分块请求及确认、快速传输位图和补传反馈 |
| `snapshot_transfer.py` | 分块保存、重组和 checksum |
| `world_snapshot.py` | 二进制世界索引和后台解码 |
| `verify_session_native.py` | 原生控制消息布局的离线执行验证 |
| `verify_world_native.py` | 原生数值、位图和 checksum 验证 |
| `verify_local_backend.py` | 自建后端启动参数原生解析验证 |
| `fast_transfer_state_disassembly.txt` | 快速传输协商和位图状态证据 |
| `world_snapshot_disassembly.txt` | 热加入 ZIP 和整包 checksum 证据 |
| `world_scalar_disassembly.txt` | 世界数值和日期编码证据 |
| `jomini_player_disassembly.txt` | 玩家名单、玩家操作及角色行动写入证据 |
| `session_wire_disassembly.txt` | 原生控制消息构造及写入证据 |
| `transport_disassembly.txt` | PDX 帧接收与路由证据 |
| `packet_writer_disassembly.txt` | 子消息写入、发送和操作码证据 |
| `envelope_disassembly.txt` | 帧初始化、排队与接收监听器证据 |
| `suffix_disassembly.txt` | 包序号和尾部构造证据 |

静态分析使用 Python 3.14。反汇编脚本从本目录的 `.deps` 加载 Capstone。

## 完整当前世界的内存导出

2026 年 11 月 14 日完成真实调用与通信交叉核对。热加入调用方 `0x369a7b0` 通过全局 `0x5c7f4b8` 指向的适配器调用 vtable `+8`，实际方法为 `0x25b6670`。自然生成的完整缓冲区与 B 接收的 ZIP 条目逐字节一致，SHA-256 为 `8fbfdb94640c8d1d97e5973acae626a71c772b36d2712ca13e071176864517ee`。

输出描述为 24 字节，字段为数据指针 `+0`、容量 `+8`、长度 `+0xc`、allocator `+0x10`。调用方使用 `0x54f2230` 的原生 allocator，读取后执行其 vtable `+0x10` 释放数据，第三个参数为 `1`。`live_world.js` 在成功、校验失败和异常退出路径均释放缓冲区。

完整读取入口在主线程的 in-game update `0x25b4f60` 中服务请求。`CGameSynchronizationManager` 位于全局 `0x5c7f410` 所指对象，其 `+0x68` 为实际同步 tick。当前世界全局 `0x5c7dbe8` 所指对象的 `+8` 为编码日期。前后时钟和控制角色一致后才发布快照，忙碌或时点改变时继续在后续更新中重试。玩家 `+0x74` 的网络上报值另行记录，实机中该值落后实际同步 tick 1。

`world_index.c` 由 Frida 内置 CModule 编译，扫描带长度的字符串、blob、定点数、数值、布尔值、RGB 和嵌套对象。Python 校验预索引的连续性、属性键和对象边界。根、在世人物、重复死者记录、省份及两层头衔共六组真实索引与原 Python 解析器相同，另外通过 52 组原生边界样例。完整索引保留重复属性及删除槽位。

Juma `33588240` 的角色控制、准备和 `set_character_focus` 已通过 B 的正常命令入口执行。房主回传保留 sender、identity 和字段，新的完整快照读到 `stewardship_domain_focus`，同日 delta 包含 focus 和 resources。推进测试的实际 tick `339644`、`339646`、`339648` 对应日期 `930.7.15`、`930.7.17`、`930.7.19`，结束后已暂停房主并正常退出 B。

首次卸载探针时发生过访问异常。当前显式移除主线程 hook 并 flush，等待 250 ms 后释放 CModule，再 detach 会话。修改后完成六轮读取和退出、多次真实 B 断线以及推进中的读取。证据位于 `captures\native-world-20261114`。

待处理的玩家事件以重复根属性 `player_event` 保存，包含 `id`、`character`、`event`、`scope` 和 `expiration_date`。活动事件出现后，当前世界从 79 个 section 变为 80 个。事件数量为 1 时解析为对象，多个时解析为列表，查询层统一过滤到指定角色。

Juma 的 `player_event.id=723` 对应 `clan_event.2002`。发送 `select_event_option(data=723, option=1)` 后，房主回传该命令，新快照中的 NPC 事件数从 1 变为 0。对比 `opinions.active_opinions`，税吏 `16812894` 对 Juma 新增 `fp3_thankful_tax_collector_opinion`，值为 `20`、期限为 `3650` 日，与脚本的 `clan_event.2002.b` 相符。房主的 `722` 号事件仍保留。完整证据为 `event_action_validation.json` 与 `event_effect_validation.json`。

## 进程外对象读取与语义查询

2026 年 11 月 15 日新增 `process_memory.py`、`memory_world.py` 和 `live_objects.py`。版本校验后，通过 `OpenProcess(0x410)` 与 `ReadProcessMemory` 读取请求字段。局部 256 字节页缓存在每次查询开始时清空。查询前后读取世界日期与实际同步 tick，另核验已使用的对象编号和槽位指针。单个实体与指定国家查询在推进时最多重试三次。全世界人物、头衔与国家枚举允许跨更新读取，仍核验实体引用，模型响应以 `observed_from` 和 `date` 标明游戏日期范围。世界对象被替换时重新尝试。内部运行信息保存在 `reader.runtime`。

各对象数据库的槽位指针为数据库 `+0x20`，长度为 `+0x2c`，每槽 16 字节，实体指针位于槽 `+8`。槽号为引用编号的低 24 位，实体中的完整编号用于校验代际。以下全局为当前可执行文件的 RVA。

| 对象 | 数据库全局 | 实体编号偏移 | 主 vtable |
| --- | --- | --- | --- |
| 人物 | `0x5c7c4f0` | `0x18` | `0x47748a0` |
| 头衔 | `0x5d32b08` | `0x10` | `0x4723198` |
| 文化 | `0x5d33300` | `0x10` | `0x474c960` |
| 仪派 | `0x5d33308` | `0x08` | `0x4748f20` |
| 信仰 | `0x5d33310` | `0x08` | `0x4740c00` |
| 封臣契约 | `0x5d33b88` | `0x08` | `0x4746150` |
| 战争 | `0x5d32e68` | `0x08` | `0x4743d48` |
| 军队 | `0x5d32e58` | `0x10` | `0x4746d80` |
| 行军单位 | `0x5d33390` | `0x10` | `0x4744430` |
| 基础兵团 | `0x5d33b68` | `0x10` | `0x4755708` |
| 已部署兵团 | `0x5d34328` | `0x10` | `0x4755918` |

人物核心写入函数 `0x2941030` 关联 first_name `+0x20`、birth `+0x60`、culture `+0xb0`、rite `+0xb4`、基础属性 `+0xc0`、traits `+0xf8` 以及五组数据指针。六项当前属性位于 `+0xd8`，与原生 getter `0x28be6c0` 一致。alive、family、court、landed 和 dead 指针分别为 `+0x1b0`、`+0x1a8`、`+0x1b8`、`+0x1c0` 和 `+0x1d0`。

dead data 写入函数 `0x2969be0` 在 `+4` 保存死亡日期。Christopher `54444` 的死亡日期为 `933.3.28`，出生为 `878.9.20`，去世年龄 54。身份信息的年龄计算已使用死亡日期。CFaith 的 key、name、description 和 rite 引用数组分别位于 `+0xe0`、`+0x108`、`+0x1c8` 和 `+0x20`。CRite 的类型指针、父信仰编号、宗教领袖编号、动态 key 与当前名称分别位于 `+0x4b0`、`+0x4b8`、`+0x4c0`、`+0x518` 和 `+0x540`。

alive 写入函数 `0x2953070` 关联金钱 `+0x100`、虔诚 `+0x110`、威望 `+0x130`、影响力 `+0x150`、月收入 `+0x2b0`、生育力 `+0x2d8`、健康 `+0x2e8` 和压力 `+0x2f8`。资源及健康使用 64 位定点数，比例为 100000。landed 写入函数为 `0x295efc0`，直属头衔为 `+0x1e0` 的引用数组，法律为 `+0x200` 的类型指针数组，封臣契约为 `+0x218` 的引用数组，政体类型指针为 `+0x3f8`。

头衔写入函数 `0x230f960` 关联 key 类型指针 `+0x48`、事实领主头衔 `+0xe8`、法理领主头衔 `+0x108` 和持有者 `+0x128`。`+0xe4` 的 capital 为头衔引用。CSubjectContract 的序列化子对象起于 `+0x10`，其 vassal 与 liege 人物指针实际位于主对象 `+0x20` 和 `+0x28`。

`CGameState +0xa0` 指向世界数据，省份管理器在世界数据 `+0x118`，省份指针数组在该管理器 `+0x28`，数量为 `+0x34`。CProvince 的编号为 `+0x10`，关联男爵头衔为 `+0x738`，占领者为 `+0x73c`，county data 指针为 `+0x848`。CCountyData 的发展度、发展进度、控制力、文化与仪派分别为 `+0x38c`、`+0x390`、`+0x398`、`+0x388` 和 `+0x384`。实机验证男爵头衔 `9471` 对应省份 `12786`。

当前玩家事件数组在世界数据 `+0x34480+0x1f18`。事件的角色编号、事件实例编号、定义指针和到期日期分别为 `+0x1b8`、`+0x1bc`、`+0x1b0` 和 `+0x1a8`。定义的脚本名称为 `+0x10` 的 C++ 字符串。先前使用的 `CEventManager` 全局属于另一组运行对象。

活跃关系数组在世界数据 `+0xa510+0x20`。角色 alive data `+0x38` 是其他角色对其持有的 opinion 对象指针数组，由原生 `0x2956800` 注册。opinion 的 owner 与 target 人物指针在 `+0x38`、`+0x40`，临时修正指针数组在 `+8`。修正对象的类型指针、起始日、到期日和 value 分别为 `+8`、`+0x10`、`+0x18` 和 `+0x24`。从当前内存再次读到税吏对 Juma 的 +20 修正，日期为 `930.7.19` 至 `940.7.19`。

军队的 regiments 引用对应 CArmyRegiment。基础 CRegiment 供地产征召兵、驻军等使用。CArmyRegiment 的当前与满编人数位于 `+0x38` 和 `+0x3c`，军队引用在 `+0x140`。CUnit 关联人物拥有者 `+0x174`、军队 `+0x178`、当前位置省份指针 `+0x20`、路径指针数组 `+0x38`。这两类兵团的区分已有回归覆盖。

landed data 的 units 位于 `+0x278`，保存 4 字节实体引用。CUnit 的路径数组保存图节点指针，节点首个 DWORD 为省份编号。指定国家查询沿封臣契约和人物直属头衔遍历，避免每次查询扫描全世界。动态新增头衔发生名称索引未命中时刷新元数据，缓存省份映射在使用前核对关联头衔。

13:29 的注入式对象读取试验后观察到旧游戏进程退出，系统日志为 `ntdll.dll`、`0xc0000005`。具体根因仍待定位。随后按用户要求重启，当前直接查询采用上述进程外访问。证据及静态反汇编位于 `captures\direct-memory-20261115`，实机交叉核对报告为 `live_query_validation.json`，连续读取报告为 `continuous_read_validation.json`。
