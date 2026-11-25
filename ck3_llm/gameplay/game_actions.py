"""Read-only descriptions of model-submitted gameplay commands."""

from copy import deepcopy

from ck3_llm.protocol.jomini_codec import (
    ACTIVITY_INTENT, ACTIVITY_PLAN, BOOL, BOOLEAN_PARAMETERS, COMMAND_SCHEMAS,
    I32, I32_ENUM, RALLY_POINTS, SCOPE_VALUE, SIMPLE_INTERACTIONS, STRING,
    TOKEN_VALUE, TRAVEL_PLAN, U32, U32_LIST,
)
from ck3_llm.world.memory_world import page


ACTION_LABELS = {
    "add_perk": "学习生活方式技能", "set_character_focus": "切换生活重心",
    "execute_decision": "执行决议", "select_event_option": "选择事件选项",
    "reply_character_interaction": "回应收到的人物互动请求",
    "start_activity_command": "主动举办活动",
    "set_activity_intent_command": "设置活动意图",
    "join_activity_command": "参加活动", "decline_activity_invite_command": "拒绝活动邀请",
    "raise_troops": "集结军队", "move_unit": "移动单位", "stop": "停止单位行军",
    "merge_units": "合并多个单位", "split_half_army": "将半数军队拆分",
    "disband_army": "解散军队", "stop_gathering_army": "停止集结",
    "select_commander": "任命指挥官", "detach_commander": "撤下指挥官",
    "start_assault": "强攻围城", "stop_assault": "停止强攻",
    "hire_mercenary": "雇佣佣兵", "extend_mercenary": "续约已雇佣佣兵", "hire_holy_order": "雇佣骑士团",
    "change_council_task": "指派或变更内阁任务", "fire_council_position": "罢免内阁成员",
    "swap_council_position": "互换内阁职位", "leave_council_position": "辞去领主内阁职务",
    "end_scheme": "终止阴谋", "expose_secret": "揭露秘密",
    "set_countermeasure": "设置阴谋反制措施", "toggle_scheme_agents_auto_assign": "切换自动指派阴谋同谋",
    "create_title": "创建头衔", "destroy_title": "销毁头衔", "usurp_title": "篡夺头衔",
    "set_primary_title": "设为主头衔", "set_realm_capital": "迁都至伯爵领首府",
    "join_faction": "加入派系", "leave_faction": "退出派系",
    "destroy_faction": "解散派系", "faction_press_demand": "派系发出最后通牒",
    "give_court_position": "任命宫廷职位", "revoke_court_position": "撤销宫廷职位",
    "cancel_travel_plan_command": "取消旅行规划", "dismiss_important_action": "忽略局势提示",
    "accept_task_contract": "接受任务委托",
    "add_law": "颁布领地或王权法律", "add_title_law": "为头衔添加法律", "clear_title_laws": "清除头衔法律",
    "nominate_successor_for_title": "提名头衔继承人", "designate_diarch": "指定摄政官或共治者",
    "set_diarchy_mandate": "设置摄政职权重心",
    "create_maa_regiment": "新建常备军兵团", "add_maa_to_regiment": "扩充或缩编常备军规模",
    "destroy_maa_regiment": "裁撤常备军兵团", "set_maa_reinforce": "切换常备军补员开关",
    "reorganize_army": "重组与合并常备军兵团", "split_army_for_siege": "分兵留守围城",
    "attach_to_unit": "将部队附庸至友军单位", "detatch_from_unit": "脱离友军部队跟随",
    "toggle_raiding": "切换军队劫掠状态", "raid": "命令军队开始劫掠",
    "knight_status": "设置角色骑士征召状态",
    "add_rally_point": "新增集结点", "move_rally_point": "移动集结点位置", "remove_rally_point": "移除集结点",
    "mass_prisoner_action": "批量处置囚犯（赎回/释放/处决）",
    "equip_artifact": "装备宝物", "unequip_artifact": "卸下宝物",
    "start_court_position_task": "指派宫廷职位任务", "stop_court_position_task": "停止宫廷职位任务",
    "select_culture_fascination": "选择文化革新聚焦项目", "add_dynasty_perk": "解锁宗族传承",
    "create_faction": "创建派系",
    "pause_game_command": "设置本人暂停状态", "set_speed": "设置游戏速度", "declare_war": "宣战",
    "add_construction": "建造或升级地产建筑", "cancel_construction": "取消地产施工",
    "send_interaction": "发送普通或头衔/宝物目标人物互动",
}
PARAMETER_TYPES = {U32: "unsigned_integer", I32: "integer", BOOL: "boolean", STRING: "string",
                   TOKEN_VALUE: "enum", I32_ENUM: "enum", U32_LIST: "unsigned_integer_array",
                   RALLY_POINTS: "rally_point_array",
                   BOOLEAN_PARAMETERS: "boolean_event_target_parameters",
                   SCOPE_VALUE: "scope_value_object",
                   ACTIVITY_INTENT: "activity_intent_object",
                   TRAVEL_PLAN: "travel_plan_object",
                   ACTIVITY_PLAN: "activity_plan_object"}
PARAMETER_LABELS = {
    "character": "角色编号", "actor": "行动角色编号", "army": "军队编号", "liege": "领主角色编号",
    "valid_for": "原生命令的控制者类别", "unit": "移动单位 unit 的编号，与 army 编号独立",
    "units": "移动单位 unit 编号数组", "siege": "围城实例编号",
    "mercenary_company": "佣兵团编号", "holy_order": "骑士团编号", "focus_type": "生活重心的脚本名称",
    "perk": "技能的脚本名称", "decision": "决议的脚本名称", "puppeteer": "操控者编号",
    "active_council_task": "内阁任务实例编号，可用 council_tasks 查询",
    "task_type": "内阁任务类型脚本名称", "position": "内阁职位索引",
    "scheme": "阴谋实例编号，可用 schemes 查询", "secret": "秘密实例编号",
    "scheme_countermeasure_type": "反制措施脚本名称",
    "landed_title": "头衔编号，可用 titles 查询", "faction": "派系实例编号，可用 factions 查询",
    "employer": "宫廷雇主角色编号", "employee": "受任角色编号",
    "travel_plan": "旅行规划编号", "important_action_item": "局势提示实例编号，可用 alerts 查询",
    "task_contract": "任务委托实例编号，可用 task_contracts 查询",
    "law": "法律脚本名称或头衔法律索引", "elector": "选举人角色编号", "candidate": "被提名继承人角色编号",
    "force": "是否强制提名", "designated_diarch": "指定的摄政或共治角色编号",
    "mandate": "摄政职权脚本名称", "regiment": "常备军兵种编号或兵团实例编号",
    "count": "常备军士兵人数变化量（按兵种单编 stack 递增，如步兵/骑兵每编 100、攻城器械每编 10；非编队份数）", "reinforce": "是否开启补员",
    "merge_army": "合并目标军队编号", "select_unit": "操作后选中的单位索引",
    "soldiers_to_stay": "留守围城的最低兵力数量", "to": "跟随的目标友军 unit 编号",
    "knight_status": "骑士状态（force/default/not/none）",
    "interaction": "批量囚犯操作类型（ransom_interaction/release_from_prison_interaction/execute_prisoner_interaction）",
    "prisoners": "囚犯角色编号列表", "artifact": "宝物实例编号", "replace": "替换的已装备宝物编号",
    "culture": "文化编号", "culture_innovation": "文化革新编号",
    "trait": "可选传承关联特质脚本名称", "faction_type": "派系类型索引",
    "cost": "支付费用标记", "war": "战争相关标记", "cooldown": "检查或使用冷却标记",
    "rally_points": "由 province 省份编号和 color 颜色编号组成的数组",
    "rally_point": "选中的集结点索引", "raise_army_type": "集结类型",
    "supply_limit": "补给限制标记", "unit_order_type": "set 替换行军命令，append 追加行军命令",
    "type": "路线的陆地或水域类型", "gathering_army": "集结状态处理方式",
    "parameters": "决议的命名布尔事件目标", "flags": "原生选项标记",
    "player": "本机玩家编号", "value": "本人暂停状态", "speed": "游戏速度档位",
}


def action(name):
    if not isinstance(name, str) or name not in ACTION_LABELS:
        raise ValueError("Unknown gameplay action")
    if name == "send_interaction":
        return {"id": name, "label": ACTION_LABELS[name], "entry_point": name,
                "parameters": {
                    "interaction": {"type": "string", "required": True, "examples": list(SIMPLE_INTERACTIONS),
                                    "description": "受支持的人物互动脚本名称，可用 interactions/interaction 查询目录、分类、目标类型与选项标记"},
                    "recipient": {"type": "unsigned_integer", "required": True,
                                  "description": "接收互动的角色，婚姻提议中为对方婚姻安排者"},
                    "character": {"type": "unsigned_integer", "required": False, "default_source": "controlled_character"},
                    "secondary_actor": {"type": "unsigned_integer", "required": False, "default": 0xffffffff,
                                        "required_for": ["arrange_marriage_interaction"], "description": "本方拟结婚角色"},
                    "secondary_recipient": {"type": "unsigned_integer", "required": False, "default": 0xffffffff,
                                            "required_for": ["arrange_marriage_interaction"], "description": "对方拟结婚角色"},
                    "target": {"type": "scope_value_object", "required": False,
                               "description": "互动目标，传 {type: landed_title|artifact|war, identity: 编号}；召集盟友参战时必须指定 war 目标"},
                    "special_fields": {"type": "object", "required": False, "default": None,
                                       "description": "特化互动所需的原生附加载荷字段，详见 interaction.special_parameters（如授衔 titles/government、剥夺 title、修改契约 types/level、宫廷职位 court_position、内阁任务 active_council_task）"},
                    "option_flags": {"type": "named_boolean_options", "required": False, "default": {},
                                     "description": "interaction.send_options 提供的标记与布尔值，按安装脚本的顺序编码"}},
                "availability": "shadow_unicorn_ai_acceptance"}
    if name == "start_activity_command":
        return {"id": name, "label": ACTION_LABELS[name], "entry_point": "start_activity_command",
                "parameters": {
                    "type": {"type": "string", "required": True,
                             "description": "activity_types 或 activity_type 查询返回的活动脚本名称"},
                    "start_date": {"type": "integer", "required": True,
                                   "description": "原生游戏小时日期整数，可由当前世界日期及延后天数确定"},
                    "phases": {"type": "array", "required": True,
                               "description": "活动阶段数组，每项含 phase 阶段名称、province 省份编号及可选 guest_subsets"},
                    "host": {"type": "unsigned_integer", "required": False, "default_source": "controlled_character",
                             "description": "主持角色编号，默认当前控制角色"},
                    "intent": {"type": "activity_intent_object", "required": False, "default": {},
                               "description": "主持者意图对象，含可选 type 意图名称与 target 目标角色编号"},
                    "guest_delay_days": {"type": "integer", "required": False, "default": 0},
                    "invite_duration": {"type": "integer", "required": False, "default": 0},
                    "options": {"type": "object", "required": False, "default": {},
                                "description": "activity_type.options 提供的类别到选项映射"},
                    "special_guests": {"type": "object", "required": False, "default": {},
                                       "description": "特殊宾客槽位到角色编号映射"},
                    "invitations": {"type": "array", "required": False, "default": [],
                                    "description": "按邀请组划分的角色编号二维数组（如 [[char_id1, char_id2]]）；多数活动要求至少邀请 1 名宾客，否则房主校验会静默拒绝"},
                    "travel_plan": {"type": "travel_plan_object", "required": False,
                                    "description": "可选原生 CTravelPlan 旅行规划对象"},
                    "variables": {"type": "object", "required": False, "default": {}},
                    "tenets": {"type": "string_array", "required": False, "default": []},
                    "doctrines": {"type": "string_array", "required": False, "default": []}},
                "availability": "requires_game_validation"}
    if name == "set_activity_intent_command":
        return {"id": name, "label": ACTION_LABELS[name], "entry_point": "set_activity_intent_command",
                "parameters": {
                    "intent": {"type": "string", "required": True,
                               "description": "活动意图脚本名称，见 activity_type.host_intents 或 guest_intents"},
                    "target": {"type": "unsigned_integer", "required": False, "default": 0xFFFFFFFF,
                               "description": "可选意图目标角色编号"},
                    "character": {"type": "unsigned_integer", "required": False, "default_source": "controlled_character"}},
                "availability": "requires_game_validation"}
    if name == "declare_war":
        return {"id": name, "label": ACTION_LABELS[name], "entry_point": "declare_war",
                "parameters": {
                    "target": {"type": "unsigned_integer", "required": True, "description": "目标角色编号"},
                    "casus_belli": {"type": "string", "required": True, "description": "宣战理由脚本名称"},
                    "titles": {"type": "unsigned_integer_array", "required": False, "default": [], "description": "目标头衔编号"},
                    "character": {"type": "unsigned_integer", "required": False, "default_source": "controlled_character"},
                    "claimant": {"type": "unsigned_integer", "required": False, "default": 0xffffffff},
                    "uses_hook": {"type": "boolean", "required": False, "default": False},
                    "has_hook": {"type": "boolean", "required": False, "default": False}},
                "availability": "requires_game_validation"}
    schema = COMMAND_SCHEMAS[name]
    parameters = {}
    for field, _, tag, default in schema["fields"]:
        parameter = {"type": PARAMETER_TYPES[tag],
                     "required": default is None and field not in schema.get("optional_fields", ()),
                     "description": PARAMETER_LABELS.get(field, field)}
        if default is not None:
            parameter["default"] = deepcopy(default)
        if tag in (TOKEN_VALUE, I32_ENUM):
            parameter["choices"] = list(schema["enums"][field])
        parameters[field] = parameter
    if name == "move_unit":
        parameters["target"]["description"] = "模型指定的目标省份编号"
    elif name == "execute_decision":
        parameters["decision"]["description"] = "decisions 或 decision 查询返回的决议脚本 id，模型决定是否执行"
        parameters["parameters"]["description"] = "模型选择的具名布尔事件目标参数，可查询决议 widget、条件和效果脚本。动态参数界面待接入"
    elif name == "select_event_option":
        parameters["data"]["description"] = "event 查询返回的待处理事件实例编号"
        parameters["option"]["description"] = "event_options 查询返回的原生选项索引"
    elif name == "reply_character_interaction":
        parameters["character_interaction"]["description"] = "incoming_interactions 查询返回的待回复实例 id"
        parameters["accept"]["description"] = "由模型选择 accept 接受、reject 拒绝、block 阻止或 acknowledge 确认。原生可用状态通过请求详情核对"
    elif name in ("join_activity_command", "decline_activity_invite_command"):
        for field in ("activity", "character"):
            parameters[field].update(required=True)
            parameters[field].pop("default", None)
        parameters["activity"]["description"] = "activity_invitations 查询返回的原生活动 id"
        if name == "join_activity_command":
            parameters["travel"]["description"] = "yes 使用游戏的活动旅行流程，no 提交无需旅行的参加方式。合法性由游戏核对"
            parameters["check_start_date"]["description"] = "使用游戏原生的开始日期校验，默认 true"
            parameters["travel_plan_draft_created"]["description"] = "可选自定义 CTravelPlan 旅行草案，含 departure_date、departure_location、waypoints、destinations 等"
    elif name in ("add_construction", "cancel_construction"):
        parameters["province"]["description"] = "地产所在省份编号，可用 holding_buildings 查询"
        if name == "add_construction":
            parameters["index"]["description"] = "模型指定的建筑槽位索引，可用 holding_buildings 查询"
            parameters["type"]["description"] = "building_types 或 building_type 查询返回的建筑类型 id"
    elif name == "change_council_task":
        parameters["target"]["description"] = "可选作用域目标，如 {type: province/landed_title/char, identity: 编号}"
    elif name == "give_court_position":
        parameters["court_position"]["description"] = "宫廷职位脚本名称"
    elif name == "revoke_court_position":
        parameters["court_position"]["description"] = "已任命宫廷职位实例编号"
    elif "target" in parameters:
        parameters["target"]["description"] = "目标对象编号"
    attributes = 8 if name in ("pause_game_command", "set_speed") else 14
    return {"id": name, "label": ACTION_LABELS[name], "entry_point": "send_game_command",
            "command_type": name, "parameters": parameters, "header": {"attribs": attributes},
            "communication_defaults": "SDK supplies sender, receiver, identity and recipient",
            "availability": "requires_game_validation"}


def actions(request):
    search = request.get("search", "").casefold()
    items = [{"id": name, "label": label, "details_query": {"kind": "action", "id": name}}
             for name, label in ACTION_LABELS.items() if search in (name + label).casefold()]
    return page(items, request)
