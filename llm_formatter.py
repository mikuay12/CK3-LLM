"""Compact Chinese text briefing formatters for VM -> LLM payloads.

Keeps LLM -> VM tool calls strictly JSON while shrinking VM -> LLM notifications
and high-frequency query responses by 50-85% with zero loss of actionable IDs.
"""

import json


def _j(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ref(obj, fallback="未知"):
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("key") or fallback
        oid = obj.get("id")
        return f"{name}({oid})" if oid is not None else str(name)
    return str(obj) if obj is not None else fallback


def _fmt_num(value, digits=1):
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def format_notification_line(event):
    """Render one game_notification / player_chat / action_feedback / error into 1-3 compact lines."""
    ev_type = event.get("event")
    if ev_type == "player_chat":
        return f"【房主玩家发来实时对话 ({event.get('time', '')})】\"{event.get('text', '')}\"\n  （请直接在回复 content 中回应玩家，并可同步调用工具执行对应操作）"
    if ev_type == "action_feedback":
        return f"[指令回执] request_id={event.get('request_id')} action={event.get('action')} status={event.get('status')}"
    if ev_type in ("command_error", "notification_error"):
        src = event.get("source") or event.get("request_id") or ""
        return f"[错误:{ev_type}] {src} {event.get('message', '')}".strip()

    nid = event.get("notification_id") or event.get("type") or "notification"
    date = event.get("date") or ""
    summary = event.get("summary") or nid
    data = event.get("data")
    eid = event.get("id")
    prefix = f"[{date} {summary}] ({nid}" + (f", id={eid}" if eid else "") + ")"

    if not isinstance(data, dict):
        if event.get("message"):
            return f"{prefix}\n  {event['message']}"
        if event.get("details_query"):
            return f"{prefix} (详情过大已折叠 -> query={_j(event['details_query'])})"
        return prefix

    # High-frequency structured notifications rendered into compact briefings
    if nid in ("military.siege_started", "military.siege_breach"):
        title = data.get("title") or {}
        realm = (data.get("defending_realm") or {}).get("primary_title") or {}
        prog = data.get("progress") or {}
        proj = data.get("projection") or {}
        eff = data.get("effects") or {}
        loc = f"{title.get('name', '?')}(prov={data.get('province_id')}, {realm.get('name', '?')})"
        eta = proj.get("estimated_days_remaining", proj.get("eta_days"))
        eta_str = f"预计{eta}天" if eta is not None else "ETA未知"
        pct = _fmt_num(prog.get("percent", proj.get("progress_percent")))
        return (
            f"{prefix} siege={data.get('id')} side={data.get('side')} 地点:{loc}\n"
            f"  攻方:{_ref(data.get('attacker'))} 守方:{_ref(data.get('defender'))} | "
            f"进度:{pct}% ({eta_str}) | 驻军:{prog.get('garrison_current', '?')}/{prog.get('garrison_capacity', '?')} | "
            f"缺口:{eff.get('breach', 0)}"
        )

    if nid == "military.siege_ended":
        occ = data.get("occupation") or {}
        occ_realm = (occ.get("occupying_realm") or {}).get("primary_title") or {}
        occ_str = f"占领者={occ.get('occupying_character_id')}({occ_realm.get('name', '')})" if occ.get("occupying_character_id") else "未沦陷/撤围"
        return f"{prefix} siege={data.get('siege_id')} prov={data.get('province_id')} side={data.get('side')} -> {occ_str}"

    if nid in ("military.battle_started", "military.battle_phase_changed"):
        att = data.get("attacker") or {}
        dfn = data.get("defender") or {}
        return (
            f"{prefix} battle={data.get('id')} prov={data.get('province_id')} 阶段={data.get('phase')}\n"
            f"  攻方:{att.get('leader_id')}(兵力{_fmt_num(att.get('fighting_soldiers', att.get('initial_soldiers')), 0)}) "
            f"vs 守方:{dfn.get('leader_id')}(兵力{_fmt_num(dfn.get('fighting_soldiers', dfn.get('initial_soldiers')), 0)})"
        )

    if nid == "military.battle_result":
        att = data.get("attacker") or {}
        dfn = data.get("defender") or {}
        return (
            f"{prefix} battle={data.get('id')} prov={data.get('province_id')} 结果={data.get('outcome') or data.get('winning_side')}\n"
            f"  攻方伤亡:{_fmt_num(att.get('casualties'), 0)}(幸存{_fmt_num(att.get('surviving_soldiers'), 0)}) | "
            f"守方伤亡:{_fmt_num(dfn.get('casualties'), 0)}(幸存{_fmt_num(dfn.get('surviving_soldiers'), 0)})"
        )

    if nid in ("military.war_started", "military.war_ended"):
        return f"{prefix} war_id={data.get('war_id')}"

    if nid in ("military.movement_stopped", "military.movement_started"):
        return f"{prefix} unit={data.get('unit', data.get('unit_id', data.get('id')))} prov={data.get('location', data.get('province_id'))} path={data.get('path', [])}"

    if nid == "state.pending_event":
        base = (
            f"{prefix} event_id={data.get('id')} key={data.get('event')} "
            f"过期={data.get('expiration_date')}"
        )
        preview = data.get("inline_preview")
        if isinstance(preview, dict):
            lines = [f"{base} 标题:{preview.get('title', '')}"]
            if preview.get("desc"):
                lines.append(f"  正文: {preview['desc']}")
            for opt in preview.get("options") or []:
                avail = "可选" if opt.get("available") is True else ("不可选" if opt.get("available") is False else "待核对")
                oline = f"  [{opt.get('index')}] ({avail}) {opt.get('label', '')}"
                if opt.get("effect_summary"):
                    oline += f" | 后果: {opt['effect_summary']}"
                if opt.get("probabilities"):
                    probs = " / ".join(
                        (f"{_fmt_num(p['percent'])}%" if p.get("percent") is not None else f"基础{_fmt_num(p.get('base_percent'))}%(动态)")
                        + f" {p.get('label') or p.get('effect', '')}".rstrip()
                        for p in opt["probabilities"]
                    )
                    oline += f" | 概率: {probs}"
                lines.append(oline)
            lines.append(f"  -> 可直接调用 select_event_option(data={data.get('id')}, option=索引)")
            return "\n".join(lines)
        return base + f" -> 可查 query kind=event / event_options id={data.get('id')}"

    if nid == "state.pending_interaction":
        inter = data.get("interaction") or {}
        ikey = inter.get("key") if isinstance(inter, dict) else inter
        lines = [f"{prefix} req_id={data.get('id')} 来自:{_ref(data.get('actor'))} 类型={ikey}"]
        if event.get("message"):
            lines.append(f"  {event['message']}")
        preview = data.get("inline_preview")
        if isinstance(preview, dict):
            if preview.get("text"):
                lines.append(f"  正文: {preview['text']}")
            if preview.get("response_options"):
                btns = ", ".join(
                    f"{k}={'可选' if (v.get('available') if isinstance(v, dict) else v) is True else '不可选' if (v.get('available') if isinstance(v, dict) else v) is False else '?'}"
                    for k, v in (preview["response_options"].items() if isinstance(preview["response_options"], dict) else [])
                )
                if btns:
                    lines.append(f"  按钮状态: {btns}")
        lines.append(f"  -> reply_character_interaction(character_interaction={data.get('id')}, accept='accept'|'reject')")
        return "\n".join(lines)

    if nid == "state.activity_invitation":
        preview = data.get("inline_preview")
        act_type = data.get("activity_type") or (data.get("activity") or {}).get("key")
        title = ((preview.get("display") or {}).get("title") if isinstance(preview, dict) else None)
        type_text = f"{title}({act_type})" if title and act_type and title != act_type else (title or act_type)
        lines = [f"{prefix} activity_id={data.get('id')} 主办:{_ref(data.get('host'))} 类型={type_text}"]
        if isinstance(preview, dict) and preview.get("response_options"):
            lines.append(f"  按钮状态: {_j(preview['response_options'])}")
        lines.append(f"  -> join_activity_command / decline_activity_invite_command (activity={data.get('id')})")
        return "\n".join(lines)

    if "before" in data and "after" in data:
        before, after = data["before"], data["after"]
        if isinstance(before, list) and isinstance(after, list) and len(before) + len(after) > 12:
            added = [x for x in after if x not in before]
            removed = [x for x in before if x not in after]
            return f"{prefix} 列表变动: 新增{added[:8]} 移除{removed[:8]} (现共{len(after)}项)"
        return f"{prefix} {_j(before)} -> {_j(after)}"

    compact = _j(data)
    if len(compact) > 400 and eid:
        return f"{prefix} (详情过大已折叠 -> query={_j({'kind': 'notification', 'id': eid})})"
    return f"{prefix} {compact}"


def format_updates_text(events, *, omitted_count=0, pending_count=0):
    """Format a batch of environment updates as a compact Chinese briefing."""
    header = f"【环境更新】共 {len(events)} 条"
    if omitted_count:
        header += f"（溢出省略 {omitted_count} 条）"
    if pending_count:
        header += f"（队列剩余 {pending_count} 条）"
    if not events:
        return header + "\n（无新通知）"
    lines = [header]
    for event in events:
        lines.append("- " + format_notification_line(event).replace("\n", "\n  "))
    return "\n".join(lines)


def _format_event_options(data):
    lines = [f"【事件选项】event_id={data.get('event_id')} (共{data.get('count')}项, 求值={data.get('live_option_mapping')})"]
    for opt in data.get("items") or []:
        name = opt.get("name")
        label = name.get("text") if isinstance(name, dict) else str(name)
        if not label and isinstance(name, dict):
            # Fallback to first variant text
            for v in name.get("variants") or []:
                if isinstance(v, dict) and v.get("text"):
                    label = v["text"]
                    break
        avail = opt.get("available")
        status = "可选" if avail is True else ("不可选" if avail is False else "待核对")
        line = f"  [{opt.get('index')}] ({status}) {label or '(无标题)'}"
        if opt.get("effect_summary"):
            line += f"\n      后果: {opt['effect_summary']}"
        if opt.get("probabilities"):
            def _fmt_prob(p):
                if p.get("percent") is not None:
                    pct_s = f"{_fmt_num(p['percent'])}%"
                elif p.get("base_percent") is not None:
                    pct_s = f"基础{_fmt_num(p['base_percent'])}%(动态)"
                else:
                    pct_s = "动态概率"
                return f"{pct_s} {p.get('label') or p.get('effect', '')}".strip()
            probs = " | ".join(_fmt_prob(p) for p in opt["probabilities"])
            line += f"\n      概率: {probs}"
        elif opt.get("script"):
            snippet = " ".join(opt["script"].split())
            line += f"\n      脚本: {snippet[:260]}" + ("..." if len(snippet) > 260 else "")
        lines.append(line)
    return "\n".join(lines)


def _format_event(data):
    defn = data.get("definition") or {}
    title = (defn.get("title") or {}).get("text") or data.get("event")
    lines = [f"【事件详情】id={data.get('id')} key={data.get('event')} 标题:{title} 过期:{data.get('expiration_date')} 选项数:{data.get('option_count')}"]
    descs = defn.get("descriptions") or []
    texts = []
    for d in descs:
        if isinstance(d, dict):
            if d.get("text"):
                texts.append(d["text"])
            for v in d.get("variants") or []:
                if isinstance(v, dict) and v.get("text"):
                    texts.append(v["text"])
    if texts:
        body = " ".join(" ".join(t.split()) for t in texts[:2])
        lines.append("正文: " + body[:500] + ("..." if len(body) > 500 else ""))
    lines.append(f"提示: 调用 query kind=event_options id={data.get('id')} 查看选项与后果")
    return "\n".join(lines)


def _format_observation(result):
    data = result.get("data") or {}
    lines = [f"【角色概况】date={result.get('date')} mode={result.get('mode')} char={result.get('character_id')}"]
    ident = data.get("identity")
    if isinstance(ident, dict):
        lines.append(
            f"身份: {ident.get('name')}({result.get('character_id')}) {ident.get('age')}岁 "
            f"文化:{_ref(ident.get('culture'))} 信仰:{_ref(ident.get('faith'))}"
        )
    res = data.get("resources")
    if isinstance(res, dict):
        lines.append(
            f"资源: 金={_fmt_num(res.get('gold'))}(+{_fmt_num(res.get('income'))}/月) "
            f"威望={_fmt_num(res.get('prestige'), 0)} 虔诚={_fmt_num(res.get('piety'), 0)} "
            f"压力={res.get('stress', 0)} 健康={_fmt_num(res.get('health'))}"
        )
    dom = data.get("domain")
    if isinstance(dom, dict):
        lines.append(
            f"领地: 主头衔={_ref(dom.get('primary_title'))} 领主={dom.get('liege_id')} "
            f"直辖={len(dom.get('title_ids') or [])} 上限={dom.get('domain_limit')} "
            f"封臣={dom.get('vassals_towards_limit')}/{dom.get('vassal_limit')}"
        )
    mil = data.get("military")
    if isinstance(mil, dict):
        lines.append(
            f"军事: 兵力={mil.get('current_strength')}/{mil.get('strength')} "
            f"战争={mil.get('war_ids', [])} 单位={mil.get('unit_ids', [])}"
        )
    if "events" in data and data["events"]:
        lines.append(f"待处理事件: {[e.get('id') for e in data['events'][:10]]}")
    # Include any other delta keys compactly
    handled = {"identity", "resources", "domain", "military", "events"}
    extra = {k: v for k, v in data.items() if k not in handled}
    if extra:
        lines.append("其他变化: " + _j(extra))
    return "\n".join(lines)


def format_tool_result_text(event, *, max_chars=6000):
    """Render a game_query / game_action tool response into compact Chinese text when helpful."""
    if not isinstance(event, dict) or event.get("event") != "interface_response":
        return _j(event)
    result = event.get("result")
    if not isinstance(result, dict):
        return _j(event)
    kind = result.get("kind")
    data = result.get("data")
    text = None
    if kind == "observation":
        text = _format_observation(result)
    elif kind == "event" and isinstance(data, dict):
        text = _format_event(data)
    elif kind == "event_options" and isinstance(data, dict):
        text = _format_event_options(data)
    if text is None:
        return _j(event)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n...(已按预算截断)"
    return text
