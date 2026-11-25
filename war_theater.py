"""Compact, low-token wartime strategic theater briefing for LLM decision making."""

import math

from army_navigation import compass_direction, shortest_routes


def _safe_map_province(world, province_id):
    if province_id is None:
        return {}
    try:
        return world.map_data().province(province_id)
    except (OSError, ValueError, AttributeError):
        return {}


def _safe_province_brief(world, province_id, ref_char_id=None, *, besieged_provinces=()):
    try:
        prov = world.province(province_id, character_id=ref_char_id)
    except (OSError, ValueError):
        return None
    map_info = _safe_map_province(world, province_id)
    title = prov.get("title") or {}
    name = title.get("name") or map_info.get("map_name") or title.get("key") or f"prov_{province_id}"
    return {
        "province_id": province_id,
        "name": name,
        "title_key": title.get("key"),
        "fort_level": prov.get("fort_level", 0),
        "terrain": prov.get("terrain") or map_info.get("terrain"),
        "supply_limit": prov.get("supply_limit"),
        "occupier_id": prov.get("occupying_character_id"),
        "besieged": province_id in besieged_provinces,
        "position": map_info.get("position"),
    }


def _collect_side_armies(world, participant_ids, ref_char_id, max_armies):
    armies = []
    seen_units = set()
    for char_id in participant_ids:
        try:
            char_data = world.character(char_id, {"military"})
        except (OSError, ValueError):
            continue
        unit_ids = ((char_data.get("military") or {}).get("unit_ids")) or []
        for unit_id in unit_ids:
            if unit_id in seen_units:
                continue
            seen_units.add(unit_id)
            try:
                um = world.unit_movement(unit_id)
            except (OSError, ValueError):
                continue
            army_id = um.get("army_id")
            soldiers = None
            supply = None
            if army_id is not None:
                try:
                    army_data = world.army(army_id)
                    soldiers = army_data.get("current")
                    supply = army_data.get("supply")
                except (OSError, ValueError):
                    pass
            prov_id = um.get("province_id")
            brief = _safe_province_brief(world, prov_id, ref_char_id) if prov_id is not None else None
            mv = um.get("movement") or {}
            armies.append({
                "unit_id": unit_id,
                "army_id": army_id,
                "owner_id": um.get("owner_id"),
                "province_id": prov_id,
                "province_name": brief["name"] if brief else None,
                "terrain": brief["terrain"] if brief else None,
                "supply_limit": brief["supply_limit"] if brief else None,
                "soldiers": soldiers,
                "supply": round(supply, 1) if isinstance(supply, (int, float)) else None,
                "speed": um.get("cached_movement_speed"),
                "destination_province_id": mv.get("destination_province_id"),
                "eta_days": mv.get("estimated_next_arrival_days"),
                "locked": mv.get("movement_locked"),
                "direction": (mv.get("direction") or {}).get("cardinal"),
            })
    armies.sort(key=lambda item: (item["soldiers"] is None, -(item["soldiers"] or 0), item["unit_id"]))
    return armies[:max_armies]


def _leader_capital_province(world, leader_id):
    if leader_id is None:
        return None
    try:
        domain = world.character(leader_id, {"domain"}).get("domain") or {}
        cap_title_id = domain.get("capital_title_id")
        if cap_title_id is None:
            return None
        title = world.title(cap_title_id)
        return title.get("province_id")
    except (OSError, ValueError):
        return None


def _collect_both_realms_provinces(world, friendly_leader_ids, enemy_leader_ids):
    """Single-pass collection of (counties, provinces) for both sides using title hierarchy."""
    f_set = set(friendly_leader_ids)
    e_set = set(enemy_leader_ids)
    both = f_set | e_set
    f_counties, e_counties = set(), set()
    f_provs, e_provs = [], []
    f_seen, e_seen = set(), set()
    if not both:
        return (0, []), (0, [])
    try:
        rows = world.rows("title")
    except (OSError, ValueError):
        return (0, []), (0, [])

    # Build lightweight in-memory title graph without touching unrelated character objects.
    titles = {}
    for title_id, ptr in rows:
        try:
            summary = world.title_summary(ptr)
        except (OSError, ValueError):
            continue
        holder = summary.get("holder_id")
        if holder is None:
            continue
        titles[title_id] = (
            holder,
            summary.get("de_facto_liege_id"),
            summary.get("tier"),
            summary.get("province_id"),
        )

    # Resolve each title's owning side by walking de_facto_liege_id in memory.
    side_cache = {}
    has_liege_graph = any(liege_tid is not None for _, liege_tid, _, _ in titles.values())
    has_top_ruler = not has_liege_graph and callable(getattr(world, "top_ruler", None))

    def _title_side(tid):
        if tid in side_cache:
            return side_cache[tid]
        visited = []
        seen = set()
        cur = tid
        matched = None
        while cur is not None and cur not in seen and cur in titles:
            if cur in side_cache:
                matched = side_cache[cur]
                break
            seen.add(cur)
            visited.append(cur)
            holder, liege_tid, _, _ = titles[cur]
            if holder in f_set:
                matched = "f"
                break
            if holder in e_set:
                matched = "e"
                break
            cur = liege_tid
        # Fallback for mock worlds where titles omit de_facto_liege_id and only implement top_ruler().
        if matched is None and visited and has_top_ruler:
            holder, liege_tid, _, _ = titles[visited[-1]]
            if liege_tid is None:
                try:
                    top = world.top_ruler(holder)
                    if top in f_set:
                        matched = "f"
                    elif top in e_set:
                        matched = "e"
                except (OSError, ValueError):
                    pass
        for v in visited:
            side_cache[v] = matched
        return matched

    for title_id, (holder, _, tier, prov_id) in titles.items():
        side = _title_side(title_id)
        if side is None:
            continue
        if prov_id is None and tier == "barony":
            try:
                prov_id = world.title(title_id).get("province_id")
            except (OSError, ValueError):
                prov_id = None
        if side == "f":
            if tier == "county":
                f_counties.add(title_id)
            if prov_id is not None and prov_id not in f_seen:
                f_seen.add(prov_id)
                f_provs.append((prov_id, title_id))
        else:
            if tier == "county":
                e_counties.add(title_id)
            if prov_id is not None and prov_id not in e_seen:
                e_seen.add(prov_id)
                e_provs.append((prov_id, title_id))
    return (len(f_counties), f_provs), (len(e_counties), e_provs)


def _collect_realm_provinces(world, top_ruler_ids):
    """Collect (county_count, fortified_provinces_list) owned by any top ruler in top_ruler_ids."""
    (count, provs), _ = _collect_both_realms_provinces(world, top_ruler_ids, ())
    return count, provs


def _expand_target_provinces(world, target_title_ids, enemy_provinces):
    """Resolve war target_title_ids to concrete province IDs."""
    target_set = set(target_title_ids or ())
    if not target_set:
        return []
    result = []
    seen = set()
    for tid in target_set:
        try:
            title = world.title(tid)
        except (OSError, ValueError):
            continue
        prov_id = title.get("province_id")
        if prov_id is not None and prov_id not in seen:
            seen.add(prov_id)
            result.append(prov_id)
        de_jure = title.get("de_jure_vassal_ids") or []
        for child_id in de_jure:
            try:
                child = world.title(child_id)
            except (OSError, ValueError):
                continue
            cprov = child.get("province_id")
            if cprov is not None and cprov not in seen:
                seen.add(cprov)
                result.append(cprov)
            for grand_id in (child.get("de_jure_vassal_ids") or []):
                try:
                    grand = world.title(grand_id)
                except (OSError, ValueError):
                    continue
                gprov = grand.get("province_id")
                if gprov is not None and gprov not in seen:
                    seen.add(gprov)
                    result.append(gprov)
    if not result:
        for prov_id, tid in enemy_provinces:
            if tid in target_set and prov_id not in seen:
                seen.add(prov_id)
                result.append(prov_id)
    return result


def _route_summary(routes_map, origin_pos, target_id, target_pos, speed=None):
    direction = (compass_direction(origin_pos, target_pos) or {}).get("cardinal")
    found = routes_map.get(target_id)
    if not found:
        return {"hops": None, "movement_cost": None, "direction": direction, "crossings": {}, "estimated_days": None}
    crossings = {}
    for seg in found["segments"]:
        kind = seg.get("crossing")
        if kind and kind != "normal":
            crossings[kind] = crossings.get(kind, 0) + 1
    est_days = math.ceil(found["movement_cost"] / speed) if isinstance(speed, (int, float)) and speed > 0 else None
    return {
        "hops": len(found["province_ids"]) - 1,
        "movement_cost": round(found["movement_cost"], 2),
        "direction": direction,
        "crossings": crossings,
        "estimated_days": est_days,
    }


def war_theater(world, war_id, character_id=None, *, max_targets=8, max_armies=8, max_nodes=2000):
    """Build a compact, single-call wartime strategic & tactical overview."""
    war = world.war(war_id)
    att_parts = [p["character"]["id"] for p in war["attacker"]["participants"] if p.get("character")]
    def_parts = [p["character"]["id"] for p in war["defender"]["participants"] if p.get("character")]

    if character_id is not None and character_id in def_parts:
        side = "defender"
    else:
        side = "attacker"
    enemy_side = "defender" if side == "attacker" else "attacker"

    friendly_ids = att_parts if side == "attacker" else def_parts
    enemy_ids = def_parts if side == "attacker" else att_parts
    friendly_leader = friendly_ids[0] if friendly_ids else None
    enemy_leader = enemy_ids[0] if enemy_ids else None
    ref_char = character_id if character_id is not None else friendly_leader

    # Active sieges & combats filtered to war participants (pre-filter via lightweight siege_context!)
    all_parts = set(friendly_ids) | set(enemy_ids)
    active_sieges = []
    besieged_provinces = set()
    has_siege_ctx = callable(getattr(world, "siege_context", None))
    try:
        refs = getattr(world, "references", None)
        for sid, _ in world.rows("siege"):
            before_refs = set(refs) if isinstance(refs, set) else None
            try:
                if has_siege_ctx:
                    ctx = world.siege_context(sid)
                    att_id = (ctx.get("attacker") or {}).get("id")
                    def_id = (ctx.get("defender") or {}).get("id")
                    if att_id not in all_parts and def_id not in all_parts:
                        if before_refs is not None:
                            refs.intersection_update(before_refs)
                        continue
                siege = world.siege(sid)
            except (OSError, ValueError):
                if before_refs is not None:
                    refs.intersection_update(before_refs)
                continue
            att_id = (siege.get("attacker") or {}).get("id")
            def_id = (siege.get("defender") or {}).get("id")
            if att_id not in all_parts and def_id not in all_parts:
                if before_refs is not None:
                    refs.intersection_update(before_refs)
                continue
            pid = siege.get("province_id")
            if pid is not None:
                besieged_provinces.add(pid)
            proj = siege.get("projection") or {}
            prog = siege.get("progress") or {}
            active_sieges.append({
                "siege_id": sid,
                "province_id": pid,
                "attacker_id": att_id,
                "defender_id": def_id,
                "percent": prog.get("percent"),
                "days_left": proj.get("estimated_days_remaining"),
            })
    except (OSError, ValueError):
        pass

    active_combats = []
    try:
        for cid, _ in world.rows("combat"):
            try:
                combat = world.combat(cid)
            except (OSError, ValueError):
                continue
            c_parts = set(combat["attacker"].get("owner_ids") or ()) | set(combat["defender"].get("owner_ids") or ())
            if not (c_parts & all_parts):
                continue
            active_combats.append({
                "combat_id": cid,
                "province_id": combat.get("province_id"),
                "phase": combat.get("phase"),
                "attacker_soldiers": combat["attacker"].get("fighting_soldiers"),
                "defender_soldiers": combat["defender"].get("fighting_soldiers"),
                "attacker_advantage": combat.get("attacker_advantage"),
            })
    except (OSError, ValueError):
        pass

    # Active armies for both sides
    friendly_armies = _collect_side_armies(world, friendly_ids, ref_char, max_armies)
    enemy_armies = _collect_side_armies(world, enemy_ids, ref_char, max_armies)

    friendly_main = friendly_armies[0] if friendly_armies else None
    enemy_main = enemy_armies[0] if enemy_armies else None

    # Realm scales & province inventories (single pass over titles)
    (friendly_counties, friendly_provs), (enemy_counties, enemy_provs) = _collect_both_realms_provinces(
        world,
        [friendly_leader] if friendly_leader else friendly_ids,
        [enemy_leader] if enemy_leader else enemy_ids,
    )

    friendly_prov_set = {pid for pid, _ in friendly_provs}
    enemy_prov_set = {pid for pid, _ in enemy_provs}

    friendly_cap_id = _leader_capital_province(world, friendly_leader)
    enemy_cap_id = _leader_capital_province(world, enemy_leader)
    target_prov_ids = _expand_target_provinces(world, war.get("target_title_ids"), enemy_provs)
    target_prov_set = set(target_prov_ids)

    # Frontline detection (enemy provinces adjacent to any friendly province)
    frontline_enemy = set()
    for pid in friendly_prov_set:
        try:
            for edge in world.adjacencies(pid, include_straits=True):
                if edge["province_id"] in enemy_prov_set:
                    frontline_enemy.add(edge["province_id"])
        except (OSError, ValueError):
            continue

    # Evaluate enemy fortified / occupied / priority candidates (skip supply_limit during bulk scan)
    enemy_fortified = 0
    enemy_occupied = 0
    enemy_candidates = []
    for pid, _ in enemy_provs:
        brief = _safe_province_brief(world, pid, None, besieged_provinces=besieged_provinces)
        if not brief:
            continue
        is_fort = (brief["fort_level"] or 0) > 0 or pid == enemy_cap_id or pid in target_prov_set
        if is_fort:
            enemy_fortified += 1
        occ = brief["occupier_id"] in set(friendly_ids)
        if occ:
            enemy_occupied += 1
        if is_fort and not occ:
            enemy_candidates.append(brief)

    friendly_fortified = 0
    friendly_occupied = 0
    friendly_threats = []
    for pid, _ in friendly_provs:
        brief = _safe_province_brief(world, pid, None, besieged_provinces=besieged_provinces)
        if not brief:
            continue
        is_fort = (brief["fort_level"] or 0) > 0 or pid == friendly_cap_id
        if is_fort:
            friendly_fortified += 1
        occ = brief["occupier_id"] in set(enemy_ids)
        if occ:
            friendly_occupied += 1
        if occ or brief["besieged"]:
            friendly_threats.append({
                "province_id": pid,
                "name": brief["name"],
                "is_capital": pid == friendly_cap_id,
                "fort_level": brief["fort_level"],
                "terrain": brief["terrain"],
                "occupied": occ,
                "besieged": brief["besieged"],
            })
    friendly_threats.sort(key=lambda item: (not item["is_capital"], not item["besieged"], item["province_id"]))
    friendly_threats = friendly_threats[:max_targets]

    # Single Dijkstra pass from friendly main army (or friendly capital) to key tactical targets
    origin_id = (friendly_main["province_id"] if friendly_main else None) or friendly_cap_id
    origin_pos = _safe_map_province(world, origin_id).get("position") if origin_id is not None else None
    main_speed = friendly_main.get("speed") if friendly_main else None

    route_targets = set()
    if enemy_main and enemy_main.get("province_id") is not None:
        route_targets.add(enemy_main["province_id"])
    if enemy_cap_id is not None:
        route_targets.add(enemy_cap_id)
    route_targets.update(target_prov_ids[:max_targets])
    route_targets.update(c["province_id"] for c in enemy_candidates[: max_targets * 3])

    routes_from_friendly = {}
    if origin_id is not None and route_targets:
        try:
            routes_from_friendly = shortest_routes(
                world.navigation_node, origin_id, route_targets, include_straits=True, max_nodes=max_nodes
            )
        except (OSError, ValueError):
            routes_from_friendly = {}

    # Distance from enemy main army to friendly capital (threat warning!)
    routes_from_enemy = {}
    if enemy_main and enemy_main.get("province_id") is not None and friendly_cap_id is not None:
        try:
            routes_from_enemy = shortest_routes(
                world.navigation_node, enemy_main["province_id"], [friendly_cap_id],
                include_straits=True, max_nodes=max_nodes,
            )
        except (OSError, ValueError):
            routes_from_enemy = {}

    # Build capitals summary
    def _cap_entry(cap_id, is_enemy):
        if cap_id is None:
            return None
        brief = _safe_province_brief(world, cap_id, ref_char, besieged_provinces=besieged_provinces)
        if not brief:
            return None
        if is_enemy:
            rinfo = _route_summary(routes_from_friendly, origin_pos, cap_id, brief["position"], speed=main_speed)
            occ = brief["occupier_id"] in set(friendly_ids)
        else:
            e_origin = enemy_main["province_id"] if enemy_main else None
            e_pos = _safe_map_province(world, e_origin).get("position") if e_origin is not None else None
            rinfo = _route_summary(routes_from_enemy, e_pos, cap_id, brief["position"],
                                   speed=enemy_main.get("speed") if enemy_main else None)
            occ = brief["occupier_id"] in set(enemy_ids)
        return {
            "province_id": cap_id,
            "name": brief["name"],
            "fort_level": brief["fort_level"],
            "terrain": brief["terrain"],
            "supply_limit": brief["supply_limit"],
            "occupied": occ,
            "besieged": brief["besieged"],
            "hops_from_opposing_main": rinfo["hops"],
            "direction": rinfo["direction"],
            "estimated_days": rinfo["estimated_days"],
        }

    # Build war_targets summary
    war_targets = []
    for pid in target_prov_ids[:max_targets]:
        brief = _safe_province_brief(world, pid, ref_char, besieged_provinces=besieged_provinces)
        if not brief:
            continue
        rinfo = _route_summary(routes_from_friendly, origin_pos, pid, brief["position"], speed=main_speed)
        war_targets.append({
            "province_id": pid,
            "name": brief["name"],
            "occupied_by_friendly": brief["occupier_id"] in set(friendly_ids),
            "fort_level": brief["fort_level"],
            "terrain": brief["terrain"],
            "supply_limit": brief["supply_limit"],
            "hops": rinfo["hops"],
            "direction": rinfo["direction"],
            "crossings": rinfo["crossings"],
        })

    # Rank priority siege targets
    priority_targets = []
    for brief in enemy_candidates:
        pid = brief["province_id"]
        rinfo = _route_summary(routes_from_friendly, origin_pos, pid, brief["position"], speed=main_speed)
        tags = []
        if pid in target_prov_set:
            tags.append("war_target")
        if pid == enemy_cap_id:
            tags.append("enemy_capital")
        if pid in frontline_enemy:
            tags.append("frontline")
        priority_targets.append({
            "province_id": pid,
            "name": brief["name"],
            "tags": tags,
            "fort_level": brief["fort_level"],
            "terrain": brief["terrain"],
            "supply_limit": brief["supply_limit"],
            "hops": rinfo["hops"],
            "direction": rinfo["direction"],
            "crossings": rinfo["crossings"],
            "_rank": (
                0 if "war_target" in tags else 1,
                0 if "enemy_capital" in tags else 1,
                0 if "frontline" in tags else 1,
                brief["fort_level"] or 0,
                rinfo["hops"] if rinfo["hops"] is not None else 9999,
            ),
        })
    priority_targets.sort(key=lambda item: item["_rank"])
    priority_targets = [{k: v for k, v in item.items() if k != "_rank"} for item in priority_targets[:max_targets]]
    if ref_char is not None:
        for item in priority_targets:
            if item.get("supply_limit") is None:
                enriched = _safe_province_brief(world, item["province_id"], ref_char, besieged_provinces=besieged_provinces)
                if enriched and enriched.get("supply_limit") is not None:
                    item["supply_limit"] = enriched["supply_limit"]

    # Key distance summary
    distances = {"origin_province_id": origin_id}
    if enemy_main and enemy_main.get("province_id") is not None:
        e_pos = _safe_map_province(world, enemy_main["province_id"]).get("position")
        distances["to_enemy_main"] = {
            "province_id": enemy_main["province_id"],
            **_route_summary(routes_from_friendly, origin_pos, enemy_main["province_id"], e_pos, speed=main_speed),
        }
    if enemy_cap_id is not None:
        c_pos = _safe_map_province(world, enemy_cap_id).get("position")
        distances["to_enemy_capital"] = {
            "province_id": enemy_cap_id,
            **_route_summary(routes_from_friendly, origin_pos, enemy_cap_id, c_pos, speed=main_speed),
        }
    nearest_target = next((t for t in war_targets if not t["occupied_by_friendly"]), None)
    if nearest_target is not None:
        distances["to_nearest_war_target"] = {
            "province_id": nearest_target["province_id"],
            "name": nearest_target["name"],
            "hops": nearest_target["hops"],
            "direction": nearest_target["direction"],
            "crossings": nearest_target["crossings"],
        }

    return {
        "war_id": war_id,
        "casus_belli": war.get("casus_belli"),
        "side": side,
        "ticking_war_score": war[side].get("ticking_war_score"),
        "controls_all_targets": war[side].get("controls_all_targets"),
        "resolving": war.get("resolving"),
        "leaders": {
            "friendly": war[side]["participants"][0]["character"] if war[side]["participants"] else None,
            "enemy": war[enemy_side]["participants"][0]["character"] if war[enemy_side]["participants"] else None,
        },
        "realm_scale": {
            "friendly": {"counties": friendly_counties, "fortified": friendly_fortified, "occupied": friendly_occupied},
            "enemy": {"counties": enemy_counties, "fortified": enemy_fortified, "occupied": enemy_occupied},
        },
        "capitals": {
            "friendly": _cap_entry(friendly_cap_id, is_enemy=False),
            "enemy": _cap_entry(enemy_cap_id, is_enemy=True),
        },
        "war_targets": war_targets,
        "priority_targets": priority_targets,
        "friendly_threats": friendly_threats,
        "armies": {"friendly": friendly_armies, "enemy": enemy_armies},
        "active_combats": active_combats,
        "active_sieges": active_sieges,
        "distances": distances,
    }
