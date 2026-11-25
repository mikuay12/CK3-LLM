"""Query semantic CK3 objects directly from a running game process.

The local execution environment owns memory layouts and synchronization.
Model-facing responses contain game information and bounded query results.
"""

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import time

from ck3_llm.world.ai_observation import DEFAULT_MAX_CHARS, _field, _json
from ck3_llm.world.memory_world import MemoryWorld, page
from ck3_llm.gameplay.game_labels import GameLabels
from ck3_llm.world.world_snapshot import format_game_date


CHARACTER_FIELDS = {
    "identity": "姓名、性别、存活状态、出生与死亡日期、文化、信仰和家族。年龄的原生结果待定位，age 为空",
    "controller": "控制这个角色的玩家，或游戏 AI",
    "skills": "当前外交、军事、管理、谋略、学识和勇武",
    "base_skills": "六项基础属性",
    "traits": "特质编号和游戏脚本名称",
    "resources": "金钱、可花费威望与虔诚、累计名望 prestige_accumulated 与累计虔诚 piety_accumulated、影响力、月收入、压力、健康和生育力；累计值独立于可花费余额",
    "focus": "当前生活重心",
    "family": "父母、配偶、订婚对象和子女编号",
    "court": "雇主、所在宫廷、军队和骑士状态",
    "domain": "政体、主头衔、领主、直属领地、首都、封臣契约及继承人",
    "military": "兵力、征召兵、战争、恐怖值和暴政",
}
QUERY_FIELDS = {
    "player_successions": {"items": "原生玩家继承确认状态，含角色、machineid、has_open_succession 与 has_succession_data；machineid=-1 为离线历史玩家。只读诊断不等于已确认继承"},
    "character": CHARACTER_FIELDS,
    "characters": CHARACTER_FIELDS,
    "vassals": {**CHARACTER_FIELDS, "contract": "与指定领主的封臣契约"},
    "realm": {"ruler": "统治者的角色信息", "primary_title": "统治者的主头衔"},
    "title": {"id": "头衔编号", "key": "头衔脚本名称", "tier": "头衔等级",
              "holder": "持有者", "controller": "持有者的玩家或游戏 AI 控制状态",
              "de_facto_liege_id": "事实领主头衔编号", "de_jure_liege_id": "法理领主头衔编号",
              "capital_title_id": "首都头衔编号", "de_jure_vassal_ids": "法理下属头衔编号",
              "heir_ids": "头衔继承人编号", "lessee_id": "承租方头衔引用，可查询该 title 的持有者", "province_id": "男爵领对应的省份编号",
              "laws": "该头衔的继承和其他法律"},
    "titles": {"items": "按 holder、tier、liege、de_jure_liege 筛选头衔。realm 为统治者编号或头衔名称时，查询整个国家内的领地"},
    "realms": {"items": "独立统治者的主头衔、统治者和玩家控制状态"},
    "province": {"title": "关联男爵领、持有者和领主关系", "occupying_character_id": "占领者编号",
                 "realm": "持有者所属的独立国家、统治者和控制状态", "occupying_realm": "战时占领者所属的国家",
                 "holding_type": "地产类型", "fort_level": "要塞等级", "monthly_income": "地产月收入",
                 "terrain": "省份地形（山地/丘陵/平原/森林/湿地等）",
                 "supply_limit": "挂载 ShadowEvaluator 时由原生 CProvince::GetSupplyLimitFor 计算的补给上限",
                 "county": "所属伯爵领的发展度、控制力、文化和信仰",
                 "levy_id": "征召兵团编号", "garrison_id": "驻军编号"},
    "holding_buildings": {"slots": "id 省份的建筑槽位索引、当前建筑脚本名称、禁用与废墟状态",
                          "construction": "正在施工的建筑、目标槽位、开工日期和出资角色",
                          "construction.paid_cost": "直接读取施工记录保存的金钱、威望、虔诚和国库已支付费用",
                          "construction.base_days_remaining": "施工记录原生保存的剩余基础天数",
                          "construction.timing": "直接读取剩余基础时间，尚未定位原生缓存的剩余天数、结束日期和进度百分比为空，并返回 unavailable",
                          "slot_count": "普通建筑槽位数量，特殊、奇观与公国建筑分别提供脚本名称"},
    "building_types": {"items": "当前内存中的建筑类型编号、脚本名称与基础施工天数，可按 search 筛选"},
    "building_type": {"id": "供 add_construction.type 使用的本次载入建筑类型编号，id 也可按脚本名称查询",
                      "base_construction_days": "直接读取建筑定义中的基础天数，原生报价缓存通过 construction_quote 查询",
                      "base_cost": "安装定义载入后的基础金钱、威望和虔诚费用，当前报价使用 construction_quote",
                      "availability": "requires_game_evaluation 表示需结合当前地产和角色条件核对"},
    "construction_quote": {"building": "请求中指定建筑类型编号或脚本名称，id 指定省份，character 默认当前控制角色，index 可指定槽位",
                           "cost": "直接读取匹配省份、建筑、槽位和付款角色的候选列表或默认升级面板原生报价缓存。gold_or_treasury 为金钱或国库金额，cost_units 保留原生货币标记，未求值的字段为空",
                           "quote_cache": "cached、partially_cached 或 unavailable，cache_kind 表示 candidate_list 或 upgrade_track。freshness=untracked 表示缓存更新时间待定位，reason 说明不可用原因",
                           "timing": "原生 GUI 缓存中的开工前工期，单位为天，未求值时为空",
                           "availability": "完整建设条件待游戏求值"},
    "neighbors": {"items": "与 id 所属国家接壤的独立国家、统治者、控制状态、边界段数与示例。id 接受角色编号或头衔名称，默认查询当前控制角色。include_straits=true 包括海峡连接"},
    "borders": {"items": "id 所属国家的边界省份对、双方领地和邻国。other 可按邻国统治者筛选，include_straits=true 包括海峡"},
    "adjacencies": {"items": "id 省份的相邻省份、所属国家和跨越类型。normal 为普通相邻，可能连接海域或荒地，river 与 large_river 为河流，strait 为海峡，impassable 为禁止通行的边。include_straits=true 包括海峡"},
    "province_map": {"position": "该省份军队图标的地图 x/z 坐标，来自安装目录的 player_stack locator",
                     "map_type": "陆地、海域、河流、湖泊或不可通行区域", "land_passable": "安装地图中的陆地通行分类"},
    "route": {"route": "挂载 ShadowEvaluator 或传入 compute=true 时基于原生邻接图返回最短路径省份列表、跳数 hops、总移动消耗、过河/海峡统计 crossings、方位 direction 及按移速估算天数 estimated_days",
              "straight_line_map_distance": "起点与终点的直线地图距离",
              "timing": "传入 speed 或 unit_id 时返回 cached_speed_estimate"},
    "war_theater": {"summary": "一站式低 token 战区简报：传入 id=战争编号，默认以当前控制角色视角返回双方领地规模(counties/fortified/occupied)、双方首都与安全距离、战争目标地块、前线高价值围城目标 Top N、己方受威胁领地、双方活跃部队(含地形与补给上限)、进行中战斗/围城及敌我距离速查"},
    "unit_movement": {"movement": "直接读取下一省份、最终目的地和当前路段消耗。进度比例、锁定状态及 ETA 的原生结果待定位，字段为空",
                      "movement.direction": "原生朝向结果待定位，字段为空。map 提供安装地图位置",
                      "cached_movement_speed": "原生缓存的移动速度，尚未初始化时为空，后续修正变化可能改变速度",
                      "map": "当前省份的地图位置及通行分类"},
    "events": {"items": "待处理事件编号、脚本事件、对应角色和到期日，id 或 character 指定接收角色，默认当前控制角色"},
    "event": {"scope": "待处理事件的接收角色、根作用域与关联人物或领地",
              "definition": "安装脚本中的标题、正文及条件文本，本地化动态表达式通过 resolved 标明",
              "option_count": "当前内存中事件定义的原生选项数量",
              "options_query": "按事件实例编号分页查看选项，默认使用当前控制角色"},
    "event_options": {"items": "事件的选项索引、名称模板及条件标记。id 为待处理事件实例编号",
                      "conditions": "使用 fields 查询脚本中的选项显示和可用条件，原生求值结果待定位",
                      "condition_results": "原生条件结果尚未定位，value=null，并标记 native_result_not_located",
                      "script": "使用 fields 查询该选项的完整脚本及效果，动态文本与条件保留原始表达式",
                      "availability": "当前标记为 requires_game_evaluation，实际可见和可用状态待原生核对"},
    "actions": {"items": "已实现的玩法操作名称与详情入口，可用 search 搜索。模型决定具体操作"},
    "declaration_preview": {"can_send": "零注入执行原生宣战可发送性检查；character 为宣战者，target 为目标角色，casus_belli 为理由，titles 为头衔数组，claimant 为宣称者",
                            "reason": "游戏生成的拒绝原因正文，区分名望、已集结军队及其他规则条件"},
    "decisions": {"items": "安装游戏的决议目录，支持按名称搜索和分页，详情提供执行参数。当前角色的显示和可执行结果待定位，shown、available 为空"},
    "decision": {"description": "决议的本地化说明与确认文本，动态表达式保留原文",
                 "conditions": "安装脚本中的显示和执行条件原文，由游戏核对运行时结果",
                 "effects": "决议的效果脚本，动态效果说明待定位",
                 "cost": "最终费用的原生结果待定位，数值为空，script 提供安装定义",
                 "cooldown": "接入内存读取时从 landed +0x430 直接读取原生冷却到期日与剩余天数，未连接实时内存时返回原生结果待定位",
                 "action": "execute_decision 的操作名称与决议参数，角色默认当前控制角色。模型决定是否执行及具体参数"},
    "activity_types": {"items": "安装游戏的活动类型目录，支持名称搜索和分页。当前角色可举办状态待原生求值"},
    "activity_type": {"description": "活动类型的本地化说明和原始条件",
                      "phases": "活动阶段的脚本编号、名称和详情入口",
                      "options": "活动配置类别与可选项目，默认返回摘要，details_query 继续读取脚本与条件",
                      "host_intents": "主持者意图的安装脚本定义",
                      "cost": "最终费用原生结果待定位，script 提供安装定义",
                      "cooldown": "接入内存读取时从 alive +0x500 直接读取原生冷却到期日与剩余天数，未连接实时内存时返回原生结果待定位",
                      "hosting_status": "已接入 start_activity_command 举办参数与原生序列化，实机效果待游戏验收",
                      "action": "start_activity_command 的操作名称与默认活动类型、主持角色参数，其余规划数据由模型补全"},
    "interactions": {"items": "已编码的人物互动目录，覆盖400种无特化负载的常规与头衔/宝物目标互动，支持按 search、group 筛选和分页"},
    "incoming_interactions": {"items": "指定 character 收到的原生待处理人物请求，包含本角色作为待回复中间人的请求，character 默认当前控制角色"},
    "incoming_interaction": {"interaction": "id 为原生待处理实例编号，返回互动类型、发送者、接收者与中间人",
                             "scope": "游戏保存的作用域、已选择的选项和排队天数",
                             "display": "按原生实例 id 和回复阶段读取已有 GUI 信件正文缓存，保留 opening、description、signed 和 signature。动态标题和未生成的正文返回空值",
                             "response_options": "接受、拒绝、阻止和确认的操作参数，具体可用状态待原生求值。回复由模型选择"},
    "activity_invitations": {"items": "直接读取指定 character 的原生活动邀请列表，character 默认当前控制角色，包含活动类型、主持者和开始日期"},
    "activities": {"items": "直接读取世界中的原生活动实例，默认 completed=false，包含筹备中和进行中的活动。列表的参加资格待原生求值",
                   "details_query": "按原生活动编号读取详情，character 默认当前控制角色"},
    "activity": {"activity": "原生活动实例的类型、主持者、阶段、省份和参加者",
                 "response_options": "模型可选择 join_activity_command 参加。available 为空，具体资格由游戏核对，完整邀请回应使用 activity_invitation",
                 "display": "动态正文原生结果待定位，当前为空"},
    "activity_invitation": {"activity": "id 为受邀活动实例编号，按角色校验原生邀请归属",
                            "phases": "原生活动阶段名称和省份编号，current_phase 是当前阶段索引",
                            "attending_character_ids": "原生参加角色列表",
                            "response_options": "由模型选择参加或拒绝。原生按钮可用性待定位，已支持可选 travel_plan_draft_created 旅行草案",
                            "display": "原生动态邀请正文待定位，当前为空"},
    "interaction": {"send_options": "指定互动的命名布尔选项及原生数组索引，来自当前安装脚本",
                     "conditions": "显示、可用、费用或接受条件的脚本，运行时求值待接入",
                     "availability": "requires_game_evaluation 表示需结合当前条件核对可执行性"},
    "action": {"parameters": "指定操作的参数类型、必填项、默认值与枚举取值",
               "entry_point": "操作通过 SDK stdin 提交的请求类型",
               "availability": "目录列出已实现的操作接口，当前可执行性需结合游戏状态核对"},
    "action_status": {"status": "通过 SDK 连接查询带 request_id 操作的发送与房主回传状态，id 为原请求编号",
                      "effect_verification": "后续由模型查询游戏状态核对实际效果"},
    "players": {"items": "联机玩家及其控制的角色"},
    "culture": {"name": "当前文化名称", "key": "文化模板名称"},
    "faith": {"key": "信仰脚本名称", "name": "信仰名称", "description": "信仰描述", "rite_ids": "所属仪派编号"},
    "rite": {"key": "仪派脚本名称", "name": "仪派名称", "faith_id": "所属信仰编号", "religious_head_id": "宗教领袖编号"},
    "contract": {"vassal_id": "封臣编号", "liege_id": "领主编号", "type": "契约类型"},
    "war": {"attacker": "进攻方参战角色、加入日期和伤亡", "defender": "防守方参战角色、加入日期和伤亡",
            "casus_belli": "宣战理由", "target_title_ids": "战争目标头衔", "start_date": "开战日期"},
    "wars": {"items": "当前战争，可按 character 筛选角色参与的战争"},
    "army": {"unit": "拥有者、位置和行军状态", "current": "原生总人数待定位，字段为空", "maximum": "原生满编总人数待定位，字段为空",
             "regiments": "所属兵团", "commander_id": "指挥官", "supply": "补给"},
    "armies": {"items": "当前已建立的军队"},
    "regiment": {"type": "兵种", "current": "原生总人数待定位，字段为空", "maximum": "原生满编总人数待定位，字段为空",
                 "strength_chunks": "直接读取七组原生当前与满编人数记录", "owner_id": "拥有者"},
    "army_regiment": {"type": "已部署兵种", "current": "当前人数", "maximum": "满编人数", "army_id": "所属军队",
                      "vassal_id": "提供兵力的封臣", "knight_id": "对应骑士"},
    "unit": {"owner_id": "拥有者", "province_id": "当前位置", "path": "从下一省份到最终目的地的行军路径",
             "movement_progress": "当前路段累计的原生移动消耗，比例请查询 unit_movement",
             "arrival_date": "原生保存字段，曾保留已过去的日期，其实时 ETA 含义尚未验证", "retreating": "撤退状态"},
    "combat_result": {"province_id": "战场省份", "winning_side": "获胜的进攻或防守方",
                      "attacker": "直接读取进攻方参战角色、指挥官、初始及幸存兵力。伤亡总数原生结果待定位",
                      "defender": "防守方对应信息", "outcome": "指定 character 的胜败结果",
                      "start_date": "战斗开始日期", "end_date": "战斗结束日期"},
    "combat_results": {"items": "保留的战报，按结束日期倒序。character 筛选对该角色保留的通知，并解释其胜败"},
    "combat": {"province_id": "正在交战的省份", "phase": "机动、主战或追击阶段", "phase_days": "当前战斗阶段经过的天数",
               "attacker_advantage": "进攻方的有符号战斗优势", "combat_width": "战场宽度",
               "attacker": "进攻方军队、拥有者、指挥官、初始兵力与当前战斗兵力", "defender": "防守方对应信息"},
    "combats": {"items": "进行中的战斗，可按 character 筛选角色作为领队或军队拥有者参与的战斗"},
    "siege": {"province_id": "围城地点", "owner_army_id": "主持围城的军队编号", "attacker": "主持军队的拥有者",
              "defender": "被围地产的持有者", "defending_realm": "被围地产所属的独立国家",
              "progress_points": "累计围城进度点",
              "progress": "直接读取驻军人数与基础人数。百分比、总进度需求与驻军上限的原生缓存待定位，相关字段为空",
              "projection": "原生围城速度与 ETA 结果待定位，返回 unavailable 及空字段",
              "phase_days_remaining": "当前围城阶段的剩余天数", "effects": "城墙缺口、饥饿、疾病、逃兵及僵持的原生计数",
              "start_date": "围城开始日期"},
    "sieges": {"items": "当前围城。character 筛选本人主持的围城或本人及其封臣领地受到的围攻，side 表示进攻或防守"},
    "mercenary_company": {"home_county": "佣兵团的驻地伯爵领", "home_realm": "驻地所属国家",
                          "hired_by_id": "当前雇主", "hired_until": "雇佣期限", "current": "原生总人数待定位，字段为空",
                          "maximum": "原生满编总人数待定位，字段为空", "regiments": "原生兵团组成与人数记录", "culture_id": "文化编号"},
    "mercenary_companies": {"items": "佣兵团，可用 realm 按驻地所属国家筛选。费用和可雇佣条件仍由游戏校验"},
    "council_task": {"id": "供 change_council_task.active_council_task 使用的原生内阁任务实例编号",
                     "type": "当前内阁任务脚本名称", "owner": "执行任务的内阁成员", "court_owner": "所属宫廷领主",
                     "target": "任务目标作用域（省份、头衔或角色）", "progress": "当前任务原生进度点",
                     "last_councillor_change": "上次更换内阁成员日期", "frozen": "任务是否冻结"},
    "council_tasks": {"items": "直接读取原生进行中内阁任务，可用 character 筛选作为宫廷领主或内阁成员参与的任务"},
    "faction": {"id": "供 join_faction/leave_faction/destroy_faction/faction_press_demand 使用的派系实例编号",
                "type": "派系脚本类型", "target": "派系针对的目标领主", "leader": "派系领袖",
                "discontent": "原生不满值", "power": "原生派系军力", "power_threshold": "原生军力阈值",
                "member_ids": "成员角色编号列表", "title_member_ids": "成员头衔编号列表",
                "created_date": "成立日期", "max_demand_date": "最后期限日期", "war_id": "已爆发派系战争编号"},
    "factions": {"items": "直接读取世界中的原生派系，可用 character 筛选针对或参与该派系的角色"},
    "scheme": {"id": "供 end_scheme 使用的原生活动阴谋实例编号", "type": "阴谋脚本类型", "status": "continue 或 invalidated",
               "owner": "阴谋主持角色", "target_type": "目标类别（character/title/culture/faith/nothing）",
               "target_id": "目标编号", "progress": "当前阴谋阶段进度", "secrecy": "原生保密值",
               "opportunities": "原生时机数", "start_date": "开始日期", "exposed": "是否已暴露", "exposed_date": "暴露日期"},
    "schemes": {"items": "直接读取原生活动阴谋，默认仅返回 continue 状态，可用 character 筛选主持者或角色目标、include_invalidated=true 包含已失效阴谋"},
    "court_position": {"id": "供 revoke_court_position 使用的原生宫廷职位实例编号", "type": "宫廷职位脚本名称",
                       "task_type": "当前执行的宫廷任务脚本名称", "employer": "宫廷雇主角色", "employee": "任职角色",
                       "hire_date": "任命日期"},
    "court_positions": {"items": "直接读取原生已任命宫廷职位实例，可用 character 筛选雇主或任职角色"},
    "holy_order": {"id": "供 hire_holy_order 使用的原生骑士团编号", "type": "骑士团脚本类型",
                   "faith_id": "所属信仰编号", "rite_id": "所属仪派编号", "title_id": "对应头衔编号",
                   "founder": "创立者", "hired_by": "当前雇主", "worldliness": "世俗度",
                   "tenet": "骑士团教义", "leased_title_ids": "承租头衔编号列表",
                   "regiment_ids": "骑士团兵团编号列表", "army_regiment_ids": "已出征军队兵团编号列表"},
    "holy_orders": {"items": "直接读取世界中的原生骑士团，可用 character 筛选创立者或当前雇主、faith 按信仰编号筛选"},
    "secret": {"id": "供 expose_secret 使用的原生秘密实例编号", "type": "秘密类型脚本名称",
               "relation_type": "脚本关系类型", "target": "秘密指向的目标作用域", "owner": "秘密所属角色",
               "exposed": "是否已揭露", "delete": "是否已标记删除", "participant_ids": "知情或参与者角色编号列表",
               "spent_by_ids": "已动用该秘密牵制权的角色编号列表", "cannot_expose_ids": "禁止揭露该秘密的角色编号列表"},
    "secrets": {"items": "直接读取原生秘密实例，默认排除已标记删除的秘密，可用 character 筛选所属者、目标或参与者，include_invalidated=true 包含待删除秘密"},
    "task_contract": {"id": "供 accept_task_contract 使用的原生任务委托编号", "type": "任务委托脚本名称",
                      "status": "not_available、available、accepted、completed 或 invalidated",
                      "tier": "委托等级", "name": "委托名称", "employer": "雇主角色", "owner": "承接者角色",
                      "target_id": "目标角色编号", "location_province_id": "委托所在省份编号",
                      "destination_province_id": "目的地省份编号", "acceptance_date": "接受日期",
                      "completion_date": "完成日期", "completed_contract_reward_desc": "完成奖励文本"},
    "task_contracts": {"items": "直接读取原生任务委托实例，可用 character 筛选雇主、承接者或目标角色"},
    "council_task_types": {"items": "安装脚本中的内阁任务类型目录，支持 search 与 group（职位）筛选"},
    "council_task_type": {"position": "对应内阁职位", "conditions": "脚本显示与有效条件", "effects": "任务效果与月度触发脚本",
                          "action": "change_council_task 参数模板"},
    "court_position_types": {"items": "安装脚本中的宫廷职位类型目录，支持 search 与 group（能力）筛选"},
    "court_position_type": {"skill": "主导能力", "conditions": "岗位显示与任职条件脚本", "cost": "薪资与罢免费用脚本",
                            "action": "give_court_position 参数模板"},
    "court_position_tasks": {"items": "安装脚本中的宫廷职位任务目录，支持 search 与 group（职位）筛选"},
    "court_position_task": {"court_position_types": "可用宫廷职位列表", "conditions": "任务显示与可用条件",
                            "cost": "任务费用脚本", "action": "start_court_position_task 参数模板"},
    "scheme_countermeasures": {"items": "安装脚本中的阴谋反制措施目录，支持 search 筛选"},
    "scheme_countermeasure": {"conditions": "反制激活条件脚本", "effects": "效果与修正脚本",
                              "action": "set_countermeasure 参数模板"},
    "laws": {"items": "安装脚本中的法律与继承法目录，支持 search 与 group（法律组）筛选"},
    "law": {"law_group": "所属法律组", "conditions": "通过与维持条件脚本", "cost": "颁布费用脚本",
            "action": "add_law 参数模板"},
    "men_at_arms_types": {"items": "安装脚本中的常备军兵种目录，支持 search 与 group（兵种类别）筛选"},
    "men_at_arms_type": {"stats": "攻击、防御、追击、掩护、攻城与编制规模", "counters": "兵种克制脚本",
                         "terrain_bonus": "地形修正脚本", "cost": "招募与维护费用脚本"},
    "relations": {"items": "与指定角色的当前关系记录，包含盟约数量、破盟状态和交战，可用 other 指定另一角色"},
    "opinions": {"items": "其他角色对 id 的临时好感修正、起止日期和朋友等脚本关系，可用 owner 指定持有这些好感的角色"},
    "important_action": {"id": "原生局势提示实例编号", "type": "提示脚本类型", "character_id": "接收角色",
                         "scope": "根作用域和命名目标，包含关联人物、头衔或其他对象"},
    "alerts": {"items": "指定 character 当前可见的原生局势提示，默认使用当前控制角色"},
    "player_messages": {"items": "指定 character 的原生保留消息队列，包含延迟或自定义消息。日常消息覆盖仍待核对"},
    "message_feed": {"items": "本机原生 GUI 消息流中的已生成标题、正文、效果文字与日期，支持分页和字段选择",
                     "recipient_status": "接收角色归属待定位，当前返回本机 GUI 缓存，按角色过滤及自动通知接入待补",
                     "freshness": "untracked 表示缓存生成和刷新时间尚未定位"},
    "notification_types": {"items": "安装目录的通知模板与虚拟机通知类型，可按 group、namespace、search 筛选"},
    "notification": {"data": "按通知 id 查询当前会话内保留的详细内容，最近交付的 2048 条可查询"},
    "notification_status": {"coverage": "各通知来源的接入范围", "source_errors": "各来源当前读取错误",
                            "pending_count": "等待发送预算的通知数量"},
}
LIST_KINDS = {"characters", "vassals", "titles", "realms", "events", "wars", "armies", "relations", "opinions", "player_successions",
              "neighbors", "borders", "adjacencies", "combat_results", "combats", "mercenary_companies",
              "council_tasks", "factions", "schemes",
              "court_positions", "holy_orders", "secrets", "task_contracts",
              "council_task_types", "court_position_types", "court_position_tasks",
              "scheme_countermeasures", "laws", "men_at_arms_types",
              "alerts", "player_messages", "message_feed", "incoming_interactions", "activity_invitations", "activities", "notification_types", "sieges", "event_options", "actions", "decisions", "activity_types", "building_types", "interactions"}


def _request(request):
    if not isinstance(request, dict):
        raise ValueError("Memory request must be a JSON object")
    command = request.get("command", "query")
    if command not in ("query", "observe", "schema", "observation_status", "notifications"):
        raise ValueError("Direct-memory commands are query, observe, schema, observation_status and notifications")
    request_id = request.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ValueError("request_id must be a nonempty string")
    budget = request.get("max_chars", DEFAULT_MAX_CHARS)
    if type(budget) is not int or budget <= 0:
        raise ValueError("max_chars must be a positive integer")
    fields = request.get("fields")
    if fields is not None and (not isinstance(fields, list) or not fields or
                               any(not isinstance(field, str) or not field for field in fields)):
        raise ValueError("fields must be a nonempty array of field paths")
    for key in ("alive", "landed", "changes_only", "include_straits", "completed", "include_invalidated", "compute", "all"):
        if key in request and type(request[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    for key in ("search", "group", "namespace"):
        if key in request and not isinstance(request[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("holder", "owner", "other", "character", "liege", "de_jure_liege", "faith"):
        if key not in request:
            continue
        value = request[key]
        if value is None and key in ("holder", "liege", "de_jure_liege"):
            continue
        if type(value) is not int or not 0 <= value < 0xffffffff:
            raise ValueError(f"{key} must be a valid entity ID")
    if "tier" in request and request["tier"] not in ("hegemony", "empire", "kingdom", "duchy", "county", "barony", "special"):
        raise ValueError("Unknown title tier")
    for key in ("offset", "limit"):
        if key in request and (type(request[key]) is not int or request[key] < 0):
            raise ValueError(f"{key} must be a nonnegative integer")
    if "limit" in request and not 1 <= request["limit"] <= 100:
        raise ValueError("limit must be between 1 and 100")
    if "max_nodes" in request and (type(request["max_nodes"]) is not int or not 1 <= request["max_nodes"] <= 50000):
        raise ValueError("max_nodes must be between 1 and 50000")
    for key in ("max_targets", "max_armies"):
        if key in request and (type(request[key]) is not int or not 1 <= request[key] <= 32):
            raise ValueError(f"{key} must be between 1 and 32")
    if command == "query" and request.get("kind") not in QUERY_FIELDS:
        raise ValueError("Unknown query kind. Use command=schema to inspect available game information")
    return command, budget


def _select(value, fields):
    if fields is None:
        return value
    selected = {}
    for path in fields:
        try:
            selected[path] = _field(value, path)
        except (KeyError, IndexError) as error:
            raise ValueError(f"Field is unavailable for this entity: {path}") from error
    return selected


def _dates(value):
    if isinstance(value, list):
        return [_dates(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: format_game_date(item) if key in ("birth", "death_date", "expiration_date", "start_date", "end_date", "created_date", "creation_date", "active_start_date", "arrival_date", "joined", "hired_until", "estimated_completion_date", "last_councillor_change", "max_demand_date", "exposed_date", "hire_date", "acceptance_date", "completion_date") and type(item) is int else _dates(item)
            for key, item in value.items()}


def _fit_response(result, budget):
    """Preserve page continuation and report every omitted entity field."""
    data = result["data"]
    result["omitted_fields"] = []
    if isinstance(data, dict) and "items" in data and "offset" in data:
        source = data["items"]
        data["items"] = []
        for item in source:
            data["items"].append(item)
            delivered = data["offset"] + len(data["items"])
            data["next_offset"] = delivered if delivered < data["count"] else None
            if len(_json(result)) > budget:
                data["items"].pop()
                delivered -= 1
                data["next_offset"] = delivered if delivered < data["count"] else None
                break
        if source and not data["items"]:
            raise ValueError("max_chars cannot fit one result. Request fewer fields or increase max_chars")
    elif isinstance(data, dict):
        source = dict(data)
        result["data"] = {}
        result["omitted_fields"] = list(source)
        if len(_json(result)) > budget:
            raise ValueError("max_chars is too small for query metadata")
        for key, value in source.items():
            result["data"][key] = value
            result["omitted_fields"].remove(key)
            if len(_json(result)) > budget:
                del result["data"][key]
                result["omitted_fields"].append(key)
    if len(_json(result)) > budget:
        raise ValueError("max_chars is too small for the requested response")
    return result


class DirectWorldReader:
    """Keep addresses and clocks inside the local execution environment."""

    def __init__(self, pid=None, *, character_id=None, notification_policy_path=None, notification_cooldown=0.0):
        self._lock = threading.RLock()
        self.memory = MemoryWorld(pid)
        try:
            self.labels = GameLabels(self.memory.memory.executable)
        except Exception:
            self.memory.close()
            self.memory = None
            raise
        self.character_id = character_id
        self.runtime = {}
        self._baseline = {}
        self._responses = OrderedDict()
        self._catalog = None
        self._event_catalog = None
        self._interaction_catalog = None
        self._planning_catalog = None
        self._definition_catalog = None
        self._notifications = None
        self.notification_policy_path = notification_policy_path
        self.notification_cooldown = notification_cooldown

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        with self._lock:
            if self.memory is None:
                return
            self.memory.close()
            self.memory = None

    def feed(self, command):
        self.notification_hub().feed(command)

    def set_character(self, character_id):
        if character_id != self.character_id:
            self.character_id = character_id
            self._baseline.clear()
            self._responses.clear()
            if self._notifications is not None:
                self._notifications.reset(character_id)

    def notification_catalog(self):
        if self._catalog is None:
            from ck3_llm.gameplay.notification_catalog import NotificationCatalog
            self._catalog = NotificationCatalog(self.memory.memory.executable)
        return self._catalog

    def event_catalog(self):
        if self._event_catalog is None:
            from ck3_llm.gameplay.event_catalog import EventCatalog
            labels = self._catalog.labels if self._catalog is not None else None
            self._event_catalog = EventCatalog(self.memory.memory.executable, labels=labels)
        return self._event_catalog

    def notification_hub(self):
        if self._notifications is None:
            from ck3_llm.gameplay.game_notifications import NotificationHub
            from ck3_llm.gameplay.notification_policy import DEFAULT_POLICY_PATH, NotificationPolicy
            self._notifications = NotificationHub(
                self, catalog=self.notification_catalog(),
                policy=NotificationPolicy(self.notification_policy_path or DEFAULT_POLICY_PATH),
                persistent_cooldown=self.notification_cooldown)
        return self._notifications

    def session_snapshot(self):
        """Internal player identity and progress. Kept out of model responses."""
        with self._lock:
            if self.memory is None:
                raise ValueError("Direct-memory reader is closed")
            native = self.memory.query({"kind": "session"})
            self._set_runtime(native["runtime"])
            return {**native["data"], **self.runtime}

    def _set_runtime(self, runtime):
        before = self.runtime
        identity = (runtime.get("world"), runtime.get("world_data"))
        previous = (before.get("world"), before.get("world_data"))
        rewound = runtime.get("tick", 0) < before.get("tick", 0)
        if before and (identity != previous or rewound):
            self._baseline.clear()
            self._responses.clear()
        self.runtime = runtime

    def handle(self, request):
        command, budget = _request(request)
        signature = _json(request)
        request_id = request.get("request_id")
        with self._lock:
            if request_id is not None and request_id in self._responses:
                if self.memory is not None and hasattr(self.memory, "clock"):
                    self._set_runtime(self.memory.clock())
            if request_id is not None and request_id in self._responses:
                previous, result = self._responses[request_id]
                if previous != signature:
                    raise ValueError("request_id was already used for a different request")
                return deepcopy(result)
            result = self._handle(request, command, budget)
            if request_id is not None:
                self._responses[request_id] = (signature, deepcopy(result))
                if len(self._responses) > 1024:
                    self._responses.popitem(last=False)
            return result

    def _handle(self, request, command, budget):
        if command == "schema":
            kind = request.get("kind")
            if kind is not None and kind not in QUERY_FIELDS:
                raise ValueError("Unknown query kind")
            return _fit_response({"kind": "schema", "data": QUERY_FIELDS if kind is None else QUERY_FIELDS[kind]}, budget)
        if command == "observation_status":
            result = {"ready": self.memory is not None and self.memory.memory.is_running(),
                      "source": "direct_game_memory", "character_id": self.character_id}
            if len(_json(result)) > budget:
                raise ValueError("max_chars is too small for query metadata")
            return result
        if self.memory is None:
            raise ValueError("Direct-memory reader is closed")
        if command == "query" and request.get("kind") == "action_status":
            raise ValueError("Action status is available through the connected SDK client")
        if command == "query" and request.get("kind") in ("actions", "action"):
            from ck3_llm.gameplay.game_actions import action, actions
            data = actions(request) if request["kind"] == "actions" else action(request.get("id"))
            if request["kind"] == "actions":
                data["items"] = [_select(item, request.get("fields")) for item in data["items"]]
            else:
                data = _select(data, request.get("fields"))
            return _fit_response({"kind": request["kind"], "data": data}, budget)
        if command == "query" and request.get("kind") in ("interactions", "interaction"):
            if self._interaction_catalog is None:
                from ck3_llm.gameplay.interaction_catalog import InteractionCatalog
                labels = self._catalog.labels if self._catalog is not None else None
                self._interaction_catalog = InteractionCatalog(self.memory.memory.executable, labels=labels)
            data = self._interaction_catalog.query(request)
            if request["kind"] == "interactions":
                data["items"] = [_select(item, request.get("fields")) for item in data["items"]]
            else:
                data = _select(data, request.get("fields"))
            return _fit_response({"kind": request["kind"], "data": data}, budget)
        if command == "query" and request.get("kind") in ("decisions", "decision", "activity_types", "activity_type"):
            if self._planning_catalog is None:
                from ck3_llm.gameplay.planning_catalog import PlanningCatalog

                labels = self._catalog.labels if self._catalog is not None else None
                if labels is None and self._event_catalog is not None:
                    labels = self._event_catalog.labels
                self._planning_catalog = PlanningCatalog(self.memory.memory.executable, labels=labels)
            query = dict(request)
            if "character" not in query and self.character_id is not None:
                query["character"] = self.character_id
            data = self._planning_catalog.query(query)
            if (request["kind"] in ("decision", "activity_type")
                    and query.get("character") is not None
                    and hasattr(self.memory, "planning_cooldown")
                    and (not request.get("fields") or any(f == "cooldown" or f.startswith("cooldown.") for f in request["fields"]))):
                cooldown = self.memory.planning_cooldown(request["kind"], data["id"], query["character"])
                data["cooldown"] = {**data["cooldown"], **cooldown}
            if request["kind"] == "decision" and query.get("character") is not None and hasattr(self.memory, "evaluate_decision"):
                wanted = request.get("fields")
                if not wanted or any(f in ("shown", "available", "availability", "valid", "can_afford", "cost")
                                     or f.startswith("cost.") for f in wanted):
                    evaluated = self.memory.evaluate_decision(data["id"], query["character"])
                    if evaluated is not None:
                        data["shown"] = evaluated["shown"]
                        data["available"] = evaluated["available"]
                        data["availability"] = evaluated["source"]
                        data["valid"] = evaluated["valid"]
                        data["can_afford"] = evaluated["can_afford"]
                        data["cost"] = {**data["cost"], **evaluated["cost"], "status": evaluated["source"]}
            if request["kind"] == "activity_type" and query.get("character") is not None and hasattr(self.memory, "evaluate_activity_type"):
                wanted = request.get("fields")
                if not wanted or any(f in ("shown", "available", "availability", "can_start", "can_afford", "cost")
                                     or f.startswith("cost.") for f in wanted):
                    evaluated = self.memory.evaluate_activity_type(data["id"], query["character"])
                    if evaluated is not None:
                        data["shown"] = evaluated["shown"]
                        data["available"] = evaluated["available"]
                        data["availability"] = evaluated["source"]
                        data["can_start"] = evaluated["can_start"]
                        if "can_afford" in evaluated:
                            data["can_afford"] = evaluated["can_afford"]
                        if "cost" in evaluated:
                            data["cost"] = {**data["cost"], **evaluated["cost"], "status": evaluated["source"]}
            if request["kind"] in LIST_KINDS:
                data["items"] = [_select(item, request.get("fields")) for item in data["items"]]
            else:
                data = _select(data, request.get("fields"))
            return _fit_response({"kind": request["kind"], "data": data}, budget)
        if command == "query" and request.get("kind") in (
                "council_task_types", "council_task_type",
                "court_position_types", "court_position_type",
                "court_position_tasks", "court_position_task",
                "scheme_countermeasures", "scheme_countermeasure",
                "laws", "law",
                "men_at_arms_types", "men_at_arms_type"):
            if self._definition_catalog is None:
                from ck3_llm.gameplay.definition_catalog import DefinitionCatalog

                labels = self._catalog.labels if self._catalog is not None else None
                if labels is None and self._event_catalog is not None:
                    labels = self._event_catalog.labels
                self._definition_catalog = DefinitionCatalog(self.memory.memory.executable, labels=labels)
            data = self._definition_catalog.query(request)
            if request["kind"] in ("men_at_arms_types", "men_at_arms_type") and hasattr(self.memory, "men_at_arms_type_indices"):
                indices = self.memory.men_at_arms_type_indices()
                if indices:
                    if request["kind"] == "men_at_arms_types":
                        for item in data["items"]:
                            if item.get("id") in indices:
                                item["native_index"] = indices[item["id"]]
                    elif data.get("id") in indices:
                        data["native_index"] = indices[data["id"]]
            if request["kind"] in LIST_KINDS:
                data["items"] = [_select(item, request.get("fields")) for item in data["items"]]
            else:
                data = _select(data, request.get("fields"))
            return _fit_response({"kind": request["kind"], "data": data}, budget)
        if command == "notifications":
            character = request.get("character", self.character_id)
            if type(character) is not int or not 0 <= character < 0xffffffff:
                raise ValueError("notifications requires a character ID")
            hub = self.notification_hub()
            hub.collect(character)
            return hub.take(budget, metadata={"kind": "notifications", "character_id": character})
        if request.get("kind") == "notification_types":
            data = self.notification_catalog().query(request)
            data["items"] = [_select(item, request.get("fields")) for item in data["items"]]
            return _fit_response({"kind": "notification_types", "data": data}, budget)
        if request.get("kind") == "notification_status":
            return _fit_response({"kind": "notification_status", "data": self.notification_hub().status()}, budget)
        if request.get("kind") == "notification":
            hub = self.notification_hub()
            hub.synchronize(hub.character_id, self.session_snapshot())
            data = _select(hub.lookup(request.get("id")), request.get("fields"))
            return _fit_response({"kind": "notification", "data": data}, budget)
        query = dict(request)
        if command == "query" and query.get("kind") == "declaration_preview":
            from ck3_llm.gameplay.gameplay_preview import declaration_preview
            actor = query.get("character", self.character_id)
            evaluator = self.memory.get_shadow_evaluator()
            if evaluator is None:
                raise ValueError("declaration_preview requires a native ShadowEvaluator")
            data = declaration_preview(evaluator, actor, query.get("target"), query.get("casus_belli"),
                                       query.get("titles", []), query.get("claimant", 0xFFFFFFFF))
            return _fit_response({"kind": "declaration_preview", "data": _select(data, request.get("fields"))}, budget)
        if query.get("kind") == "war_theater" and "character" not in query and self.character_id is not None:
            query["character"] = self.character_id
        if query.get("kind") == "construction_quote" and "character" not in query:
            if self.character_id is None:
                raise ValueError("construction_quote requires a character ID")
            query["character"] = self.character_id
        if command == "query" and query.get("kind") in ("event", "event_options"):
            self.event_catalog()
            if "character" not in query and self.character_id is not None:
                query["character"] = self.character_id
        if query.get("kind") == "events" and "id" not in query:
            recipient = query.get("character", self.character_id)
            if recipient is not None:
                query["id"] = recipient
        if query.get("kind") in ("activities", "activity") and "character" not in query and self.character_id is not None:
            query["character"] = self.character_id
        if query.get("kind") in ("alerts", "player_messages", "incoming_interactions", "incoming_interaction", "activity_invitations", "activity_invitation") and "character" not in query:
            if self.character_id is None:
                raise ValueError("Specify a notification recipient character")
            query["character"] = self.character_id
        if query.get("kind") in ("armies", "council_tasks", "court_positions", "factions", "schemes", "secrets", "task_contracts"):
            if query.pop("all", False):
                pass
            elif "character" not in query and self.character_id is not None:
                query["character"] = self.character_id
        if query.get("kind") in ("neighbors", "borders") and "id" not in query:
            query["id"] = request.get("character", self.character_id)
            if query["id"] is None:
                raise ValueError("Specify a character or title for realm neighbors")
        if query.get("kind") in ("titles", "realms", "building_types") and "search" in query:
            search = query["search"].casefold()
            query["search_keys"] = {key for key, value in self.labels.values.items() if search in value.casefold()}
            if query["kind"] == "building_types":
                query["search_keys"] = {key.removeprefix("building_type_") for key in query["search_keys"]}
        if command == "observe":
            character = request.get("character", self.character_id)
            if type(character) is not int or not 0 <= character < 0xffffffff:
                raise ValueError("observe requires a character ID")
            if character != self.character_id:
                self.set_character(character)
            query.update(kind="observe", id=character)
        native = self.memory.query(query)
        self._set_runtime(native["runtime"])
        data = self.labels.apply(_dates(native["data"]))
        if query.get("kind") in ("event", "event_options"):
            definition, options = self.event_catalog().describe(data)
            if query["kind"] == "event":
                data["definition"] = definition
            else:
                native_options = {item["index"]: item for item in data.get("native_options") or []}
                result = page(list(range(data["option_count"])), query)
                items = []
                for index in result["items"]:
                    option = dict(options[index]) if options else {"index": index, "name": None, "conditional": None,
                                                                   "conditions": [], "condition_results": [],
                                                                   "effect_summary": None, "probabilities": [],
                                                                   "script": None,
                                                                   "availability": "requires_game_evaluation"}
                    native_opt = native_options.get(index)
                    if native_opt is not None:
                        option["available"] = native_opt["available"]
                        option["shown"] = native_opt["shown"]
                        option["show_as_unavailable"] = native_opt["show_as_unavailable"]
                        option["availability"] = native_opt["availability"]
                    if request.get("fields") is None:
                        default_keys = ("index", "name", "conditional", "availability", "effect_summary", "probabilities")
                        extra_keys = ("available", "shown", "show_as_unavailable") if native_opt is not None else ()
                        option = {key: option[key] for key in (*default_keys, *extra_keys) if key in option}
                    detail_fields = ["index", "name", "conditions", "condition_results", "effect_summary", "probabilities", "script", "availability"]
                    if native_opt is not None:
                        detail_fields.extend(["available", "shown", "show_as_unavailable"])
                    option["details_query"] = {"kind": "event_options", "id": data["id"],
                                               "character": data["character_id"], "offset": index, "limit": 1,
                                               "fields": detail_fields}
                    items.append(option)
                data = {**result, "items": items, "event_id": data["id"], "character_id": data["character_id"],
                        "definition_status": definition["status"],
                        "live_option_mapping": "native_shadow_unicorn" if native_options else "pending_validation"}
        fields = request.get("fields")
        if command == "observe":
            personal = data["character"]
            personal["events"] = data["events"]
            personal = _select(personal, fields)
            full = not request.get("changes_only", True) or not self._baseline
            changed = {key: value for key, value in personal.items() if full or self._baseline.get(key) != value}
            result = _fit_response({"kind": "observation", "character_id": self.character_id,
                                    "mode": "full" if full else "delta", "date": format_game_date(self.runtime["date"]),
                                    "data": changed}, budget)
            self._baseline.update(deepcopy(result["data"]))
            return result
        if request["kind"] in LIST_KINDS:
            data["items"] = [_select(item, fields) for item in data["items"]]
        else:
            data = _select(data, fields)
        result = {"kind": request["kind"], "date": format_game_date(self.runtime["date"]), "data": data}
        if self.runtime.get("spans_updates"):
            result["observed_from"] = format_game_date(self.runtime["date_started"])
        return _fit_response(result, budget)


class DirectNotifications:
    """Track retained battle reports and significant military state changes."""

    def __init__(self, reader):
        self.reader = reader
        self.character_id = None
        self.seen_results = set()
        self.units = None
        self.wars = None
        self.combats = {}

    def poll(self, character_id):
        if character_id != self.character_id:
            self.character_id = character_id
            self.seen_results.clear()
            self.units = self.wars = None
            self.combats = {}
        events = []
        seen_results = set(self.seen_results)
        offset = 0
        while offset is not None:
            reports = self.reader.handle({"kind": "combat_results", "character": character_id,
                                          "offset": offset, "limit": 100, "max_chars": 200000})
            for report in reports["data"]["items"]:
                if report["id"] not in seen_results:
                    events.append({"event": "game_notification", "type": "battle_result",
                                   "character_id": character_id, "date": report["end_date"], "data": report})
                    seen_results.add(report["id"])
            offset = reports["data"]["next_offset"]
        character = self.reader.handle({"kind": "character", "id": character_id, "fields": ["military"]})
        combats = {}
        offset = 0
        while offset is not None:
            battles = self.reader.handle({"kind": "combats", "character": character_id,
                                          "offset": offset, "limit": 100, "max_chars": 200000})
            for battle in battles["data"]["items"]:
                combats[battle["id"]] = battle
                previous = self.combats.get(battle["id"])
                change = "battle_started" if previous is None else None
                if previous is not None and previous["phase"] != battle["phase"]:
                    change = "battle_phase_changed"
                if change:
                    events.append({"event": "game_notification", "type": change, "character_id": character_id,
                                   "date": battles["date"], "data": battle})
            offset = battles["data"]["next_offset"]
        military = character["data"]["military"] or {"unit_ids": [], "war_ids": []}
        units = {}
        for unit_id in military["unit_ids"]:
            unit = self.reader.handle({"kind": "unit", "id": unit_id})["data"]
            units[unit_id] = unit
            previous = self.units.get(unit_id) if self.units is not None else None
            if previous is None:
                continue
            change = None
            if unit["retreating"] and not previous["retreating"]:
                change = "retreat_started"
            elif previous["retreating"] and not unit["retreating"]:
                change = "retreat_ended"
            elif previous["path"] and not unit["path"]:
                change = "movement_stopped"
            elif not previous["path"] and unit["path"]:
                change = "movement_started"
            if change:
                events.append({"event": "game_notification", "type": change, "character_id": character_id,
                               "date": character["date"], "data": unit})
        wars = set(military["war_ids"])
        if self.wars is not None:
            for war_id in sorted(wars - self.wars):
                events.append({"event": "game_notification", "type": "war_started", "character_id": character_id,
                               "date": character["date"], "data": {"war_id": war_id}})
            for war_id in sorted(self.wars - wars):
                events.append({"event": "game_notification", "type": "war_ended", "character_id": character_id,
                               "date": character["date"], "data": {"war_id": war_id}})
        self.seen_results = seen_results
        self.units = deepcopy(units)
        self.wars = wars
        self.combats = deepcopy(combats)
        return events


def _player_session(state, character_id, machineid):
    player = next((item for item in state["players"] if item["machineid"] == machineid), None)
    if machineid is not None:
        character_id = player["character_id"] if player is not None else None
    return {**state, "character_id": character_id, "local_player": player, "machineid": machineid,
            "active": True, "sampled_at": time.monotonic()}


class DirectObservationQueue:
    """Integrate direct queries with the existing client polling loop."""

    def __init__(self, pid, *, notification_policy_path=None, notification_cooldown=0.0):
        self.pid = pid
        self.reader = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ck3-memory")
        self.pending = {}
        self.notification_future = None
        self.next_notification_poll = 0
        self.notification_error = None
        self.notification_policy_path = notification_policy_path
        self.notification_cooldown = notification_cooldown
        self.machineid = None
        self.session = None
        self.progress_world = None
        self.next_progress_poll = 0
        self.progress_error = None
        self.commands = queue.SimpleQueue()
        self.closed = False

    def set_player(self, machineid):
        if self.machineid != machineid:
            self.session = {"active": False, "character_id": None, "machineid": machineid,
                            "sampled_at": time.monotonic()}
            self.next_progress_poll = 0
        self.machineid = machineid

    def _poll_progress(self, character_id):
        if self.closed or time.monotonic() < self.next_progress_poll:
            return []
        self.next_progress_poll = time.monotonic() + 0.2
        try:
            # A separate read-only handle keeps small clock/player reads moving
            # while the background worker scans a large world query.
            if self.progress_world is None:
                self.progress_world = MemoryWorld(self.pid)
            native = self.progress_world.query({"kind": "session"})
            self.session = _player_session({**native["data"], **native["runtime"]}, character_id, self.machineid)
            self.progress_error = None
        except (OSError, ValueError) as error:
            self.session = {"active": False, "character_id": None, "machineid": self.machineid,
                            "sampled_at": time.monotonic()}
            message = str(error)
            if message != self.progress_error:
                self.progress_error = message
                return [{"event": "notification_error", "source": "progress", "character_id": character_id,
                         "message": message}]
        return []

    def _reader(self, character_id):
        if self.reader is None:
            self.reader = DirectWorldReader(self.pid, character_id=character_id,
                                            notification_policy_path=self.notification_policy_path,
                                            notification_cooldown=self.notification_cooldown)
        return self.reader

    def _handle(self, request, character_id):
        reader = self._reader(character_id)
        reader.set_character(character_id)
        if request.get("command") in ("observe", "notifications") and "character" not in request:
            request = {**request, "character": character_id}
        return reader.handle(request)

    def submit(self, request, character_id):
        if self.closed:
            raise ValueError("The direct-memory request queue is closed")
        if len(self.pending) >= 64:
            raise ValueError("The direct-memory request queue is full")
        future = self.executor.submit(self._handle, deepcopy(request), character_id)
        self.pending[future] = request.get("request_id")

    def feed(self, command):
        if command.get("type") == "create_important_action":
            self.commands.put(deepcopy(command))

    def _notifications(self, character_id, machineid):
        reader = self._reader(character_id)
        with reader._lock:
            try:
                return self._refresh_notifications(reader, character_id, machineid)
            except (OSError, ValueError):
                reader.set_character(None)
                if reader._notifications is not None:
                    reader._notifications.reset()
                raise

    def _refresh_notifications(self, reader, character_id, machineid):
        session = _player_session(reader.session_snapshot(), character_id, machineid)
        character_id = session["character_id"]
        reader.set_character(character_id)
        hub = reader.notification_hub()
        while not self.commands.empty():
            hub.feed(self.commands.get_nowait())
        if character_id is None:
            hub.reset()
            return session, []
        return session, hub.poll(character_id, runtime=session)

    def poll(self, character_id=None):
        events = self._poll_progress(character_id)
        if self.session is not None:
            if not self.session["active"]:
                character_id = None
            elif self.machineid is not None or character_id is None:
                character_id = self.session["character_id"]
        for future in list(self.pending):
            if not future.done():
                continue
            request_id = self.pending.pop(future)
            try:
                events.append({"event": "interface_response", "request_id": request_id, "result": future.result()})
            except Exception as error:
                events.append({"event": "interface_error", "request_id": request_id, "message": str(error)})
        if self.notification_future is not None and self.notification_future.done():
            try:
                session, updates = self.notification_future.result()
                if session["machineid"] != self.machineid:
                    updates = []
                elif self.session is None or session.get("sampled_at", 0) >= self.session.get("sampled_at", 0):
                    self.session = session
                current = self.session or {}
                source_world = (session.get("world"), session.get("world_data"))
                current_world = (current.get("world"), current.get("world_data"))
                if (not current.get("active") or source_world != current_world
                        or session.get("tick", 0) > current.get("tick", 0)):
                    updates = []
                if self.machineid is not None or character_id is None:
                    character_id = self.session["character_id"] if self.session is not None else None
                events.extend(event for event in updates if event.get("character_id") == character_id)
                self.notification_error = None
            except Exception as error:
                if str(error) != self.notification_error:
                    events.append({"event": "notification_error", "character_id": character_id, "message": str(error)})
                    self.notification_error = str(error)
            self.notification_future = None
        if (not self.closed and character_id is not None and self.notification_future is None
                and time.monotonic() >= self.next_notification_poll):
            self.notification_future = self.executor.submit(self._notifications, character_id, self.machineid)
            self.next_notification_poll = time.monotonic() + 0.25
        return events

    def finish(self):
        if self.closed:
            return []
        self.closed = True
        self.executor.shutdown(wait=True)
        events = self.poll()
        if self.reader is not None:
            self.reader.close()
        if self.progress_world is not None:
            self.progress_world.close()
        return events


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="Target CK3 process. Autodetected when omitted")
    parser.add_argument("--character", type=int, help="Default character for personal observations")
    parser.add_argument("--stdin", action="store_true", help="Serve one JSON query per input line")
    parser.add_argument("--watch", action="store_true", help="Stream character-scoped notifications with filtering and a delivery budget")
    parser.add_argument("--notification-policy", type=Path, help="Notification preference file. Reloaded while watching")
    parser.add_argument("--watch-player", help="Follow the current character controlled by this player when watching")
    parser.add_argument("--request-file", type=Path, help="Read a single query from this file")
    args = parser.parse_args()
    if args.watch and ((args.character is None and not args.watch_player) or args.stdin or args.request_file is not None):
        parser.error("--watch requires --character or --watch-player and its own output stream")
    try:
        with DirectWorldReader(args.pid, character_id=args.character, notification_policy_path=args.notification_policy) as reader:
            if args.watch:
                notifications = reader.notification_hub()
                last_error = None
                while reader.memory.memory.is_running():
                    try:
                        character = args.character
                        if args.watch_player:
                            players = reader.handle({"kind": "players"})["data"]
                            matches = [item for item in players if item["name"] == args.watch_player]
                            if len(matches) != 1 or matches[0]["character"] is None:
                                raise ValueError("The watched player name is ambiguous or has no controlled character")
                            player = matches[0]
                            character = player["character"]["id"]
                        for event in notifications.poll(character):
                            print(_json(event), flush=True)
                        last_error = None
                    except (OSError, ValueError) as error:
                        if str(error) != last_error:
                            print(_json({"event": "notification_error", "message": str(error)}), flush=True)
                            last_error = str(error)
                    time.sleep(0.25)
            elif args.stdin:
                for line in sys.stdin:
                    if not line.strip():
                        continue
                    request_id = None
                    try:
                        request = json.loads(line)
                        request_id = request.get("request_id") if isinstance(request, dict) else None
                        result = reader.handle(request)
                        print(_json({"event": "memory_response", "request_id": request_id, "result": result}), flush=True)
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        print(_json({"event": "memory_error", "request_id": request_id, "message": str(error)}), flush=True)
            else:
                request = {"command": "query", "kind": "players"} if args.request_file is None else json.loads(
                    args.request_file.read_text(encoding="utf-8-sig"))
                print(_json(reader.handle(request)))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(_json({"event": "memory_error", "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
