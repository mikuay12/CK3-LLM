use std::collections::HashSet;

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value, json};

use crate::world::{EntityList, QueryKind};

use super::military::MERCENARY;
use super::{
    CHARACTER, Layout, ReadView, TITLE, WorldView, array, date_text, decode_pointer, fresh,
    reference,
};

const COUNCIL_TASK: Layout = Layout {
    object: "内阁任务",
    global: 0x5d32eb0,
    id_offset: 0x10,
    vtable: 0x453d1b0,
};
const FACTION: Layout = Layout {
    object: "派系",
    global: 0x5d32ea0,
    id_offset: 0x10,
    vtable: 0x4754088,
};
const SCHEME: Layout = Layout {
    object: "阴谋",
    global: 0x5d34c38,
    id_offset: 0x10,
    vtable: 0x4789c90,
};
const COURT_POSITION: Layout = Layout {
    object: "宫廷职位",
    global: 0x5d32d20,
    id_offset: 8,
    vtable: 0x473f400,
};
const HOLY_ORDER: Layout = Layout {
    object: "军事修会",
    global: 0x5d32f18,
    id_offset: 0x10,
    vtable: 0x4754d28,
};
const SECRET: Layout = Layout {
    object: "秘密",
    global: 0x5d34c28,
    id_offset: 0x10,
    vtable: 0x473f5b0,
};
const TASK_CONTRACT: Layout = Layout {
    object: "任务委托",
    global: 0x5d34c30,
    id_offset: 8,
    vtable: 0x4757f98,
};
const CONTRACT: Layout = Layout {
    object: "封臣契约",
    global: 0x5d33b88,
    id_offset: 8,
    vtable: 0x4746150,
};

fn layout(kind: QueryKind) -> Result<Layout> {
    Ok(match kind {
        QueryKind::CouncilTask => COUNCIL_TASK,
        QueryKind::Faction => FACTION,
        QueryKind::Scheme => SCHEME,
        QueryKind::CourtPosition => COURT_POSITION,
        QueryKind::HolyOrder => HOLY_ORDER,
        QueryKind::Secret => SECRET,
        QueryKind::TaskContract => TASK_CONTRACT,
        QueryKind::Contract => CONTRACT,
        QueryKind::MercenaryCompany => MERCENARY,
        _ => bail!("此类别没有对象数据库"),
    })
}

impl WorldView<'_> {
    pub(super) fn relations(
        &mut self,
        kind: QueryKind,
        id: u32,
        other: Option<u32>,
        offset: usize,
        limit: usize,
    ) -> Result<Value> {
        ensure!((1..=100).contains(&limit), "关系查询每页需要 1 到 100 项");
        let character = self.lookup(CHARACTER, id)?;
        if let Some(related) = other {
            self.lookup(CHARACTER, related)?;
        }
        let mut matches = Vec::new();
        if kind == QueryKind::Relations {
            for bytes in self.read.vector::<8>(self.clock.data + 0xa530)? {
                let relation = decode_pointer(bytes)?;
                let first = self.read.u32(relation + 8)?;
                let second = self.read.u32(relation + 0xc)?;
                let related = if first == id {
                    second
                } else if second == id {
                    first
                } else {
                    continue;
                };
                if other.is_none_or(|other| other == related) {
                    matches.push((relation, related));
                }
            }
        } else {
            let alive = self.read.pointer(character + 0x1b0)?;
            if alive == 0 {
                return Ok(page(0, offset, Vec::new()));
            }
            for bytes in self.read.vector::<8>(alive + 0x38)? {
                let relation = decode_pointer(bytes)?;
                if self.read.u8(relation + 0x4a)? != 0 {
                    continue;
                }
                let owner = self.read.pointer(relation + 0x38)?;
                if owner == 0 {
                    continue;
                }
                let owner = self.read.u32(owner + 0x18)?;
                if other.is_none_or(|other| other == owner) {
                    ensure!(
                        self.read.pointer(relation + 0x40)? == character,
                        "好感记录指向的人物已变化"
                    );
                    matches.push((relation, owner));
                }
            }
        }
        let mut items = Vec::new();
        for &(relation, related) in matches.iter().skip(offset).take(limit) {
            if kind == QueryKind::Relations {
                items.push(json!({"character":self.character_name(related)?, "war_id":self.read.reference(relation + 0x20)?,
                    "alliance_count":self.read.u32(relation + 0x94)?, "broken_alliance":self.read.u8(relation + 0x1a9)? != 0}));
            } else {
                let mut modifiers = Vec::new();
                for bytes in self.read.vector::<8>(relation + 8)? {
                    let modifier = decode_pointer(bytes)?;
                    modifiers.push(json!({"key":self.key_at(modifier + 8)?, "value":self.read.i32(modifier + 0x24)?,
                        "start_date":actual_date(self.read.u32(modifier + 0x10)?),
                        "expiration_date":actual_date(self.read.u32(modifier + 0x18)?)}));
                }
                let scripted = self
                    .read
                    .vector::<40>(relation + 0x20)?
                    .into_iter()
                    .map(|row| self.read.key(decode_pointer(array(&row[..8]))?))
                    .collect::<Result<Vec<_>>>()?;
                items.push(
                    json!({"owner":self.character_name(related)?, "target_id":id,
                    "modifiers":modifiers, "scripted_relations":scripted}),
                );
            }
        }
        Ok(page(matches.len(), offset, items))
    }

    pub(super) fn ruler(&mut self, key: &str) -> Result<u32> {
        if let Ok(id) = key.parse() {
            return Ok(id);
        }
        let title = self.title_id(key)?;
        self.title_holder(Some(title), None)?
            .context("此头衔没有在位领主")
    }

    pub(super) fn realm_detail(&mut self, key: &str) -> Result<Value> {
        let id = self.ruler(key)?;
        let primary = self.primary_title(id)?.context("此角色没有领地")?;
        let ruler = self.character(id)?;
        let ids: Vec<u32> = serde_json::from_value(ruler["domain"]["contract_ids"].clone())?;
        let vassals = ids
            .into_iter()
            .map(|id| self.entity_detail(QueryKind::Contract, id))
            .collect::<Result<Vec<_>>>()?;
        Ok(
            json!({"primary_title":primary, "ruler":ruler, "vassals":vassals, "top_realm":self.realm(Some(id))?}),
        )
    }

    pub(super) fn entities(&mut self, request: EntityList<'_>) -> Result<Value> {
        ensure!((1..=100).contains(&request.limit), "每页条数须为 1 到 100");
        if let Some(character) = request.character {
            self.lookup(CHARACTER, character)?;
        }
        match request.kind {
            QueryKind::Titles | QueryKind::Realms => return self.titles_and_realms(request),
            QueryKind::Alerts
            | QueryKind::PlayerMessages
            | QueryKind::MessageFeed
            | QueryKind::PlayerSuccessions => {
                return self.messages(request);
            }
            QueryKind::Activity => {
                #[cfg(all(windows, target_arch = "x86_64"))]
                return self.activities(request);
                #[cfg(not(all(windows, target_arch = "x86_64")))]
                bail!("活动查询需要 Windows x64");
            }
            _ => {}
        }
        if request.kind == QueryKind::Contract
            && let Some(character) = request.character
        {
            let object = self.lookup(CHARACTER, character)?;
            let landed = self.read.pointer(object + 0x1c0)?;
            let ids = if landed == 0 {
                Vec::new()
            } else {
                self.read.ids(landed + 0x218)?
            };
            let items = ids
                .iter()
                .skip(request.offset)
                .take(request.limit)
                .map(|&id| self.entity_detail(request.kind, id))
                .collect::<Result<Vec<_>>>()?;
            return Ok(page(ids.len(), request.offset, items));
        }
        let layout = layout(request.kind)?;
        let mut ids = Vec::new();
        let search = request.search.to_lowercase();
        self.visit_objects(layout, |read, id, object| {
            if !request.include_inactive
                && (layout == SCHEME && read.u32(object + 0x28)? != 0
                    || layout == SECRET && read.u8(object + 0xa9)? != 0)
            {
                return Ok(());
            }
            if let Some(character) = request.character
                && !member(read, layout, object, character)?
            {
                return Ok(());
            }
            if !search.is_empty() {
                let offset = match layout {
                    COUNCIL_TASK => 0x18,
                    TASK_CONTRACT => 0x40,
                    COURT_POSITION => 0x110,
                    HOLY_ORDER => 0x30,
                    CONTRACT => 0xc8,
                    _ => 0x20,
                };
                let definition = read.pointer(object + offset)?;
                if !read
                    .key(definition)?
                    .is_some_and(|key| key.to_lowercase().contains(&search))
                {
                    return Ok(());
                }
            }
            ids.push(id);
            Ok(())
        })?;
        let items = ids
            .iter()
            .skip(request.offset)
            .take(request.limit)
            .map(|&id| self.entity_detail(request.kind, id))
            .collect::<Result<Vec<_>>>()?;
        Ok(page(ids.len(), request.offset, items))
    }

    pub(super) fn entity_detail(&mut self, kind: QueryKind, id: u32) -> Result<Value> {
        if kind == QueryKind::MercenaryCompany {
            return self.mercenary_company(id);
        }
        let object = self.lookup(layout(kind)?, id)?;
        Ok(match kind {
            QueryKind::CouncilTask => {
                let owner = self.read.reference(object + 0x40)?;
                let court = self.read.reference(object + 0x44)?;
                json!({"id":id, "type":self.key_at(object + 0x18)?,
                    "owner_id":owner, "owner":self.person(owner)?, "court_owner_id":court, "court_owner":self.person(court)?,
                    "target":self.scope_value(object + 0x48)?, "progress":self.read.fixed(object + 0x20)?,
                    "last_councillor_change":date_text(self.read.u32(object + 0x28)?), "frozen":self.read.u8(object + 0x39)? != 0,
                    "last_appointed_councillor_id":self.read.reference(object + 0x30)?,
                    "next_forced_councillor_id":self.read.reference(object + 0x34)?})
            }
            QueryKind::Faction => {
                let target = self.read.reference(object + 0x40)?;
                let leader = self.read.reference(object + 0x44)?;
                json!({"id":id, "type":self.key_at(object + 0x20)?, "target_id":target, "target":self.person(target)?,
                    "leader_id":leader, "leader":self.person(leader)?, "discontent":self.read.fixed(object + 0x28)?,
                    "power":self.read.fixed(object + 0x30)?, "power_threshold":self.read.fixed(object + 0x38)?,
                    "member_ids":struct_ids::<0x20>(&mut self.read, object + 0x48)?,
                    "title_member_ids":struct_ids::<0x18>(&mut self.read, object + 0x60)?,
                    "created_date":date_text(self.read.u32(object + 0x7c)?), "max_demand_date":date_text(self.read.u32(object + 0x84)?),
                    "war_id":self.read.reference(object + 0x8c)?})
            }
            QueryKind::Scheme => {
                let owner = self.read.reference(object + 0x2c)?;
                let status = ["continue", "invalidated"]
                    .get(self.read.u32(object + 0x28)? as usize)
                    .unwrap_or(&"invalid");
                let target = ["character", "title", "culture", "faith", "nothing"]
                    .get(self.read.u32(object + 0x30)? as usize)
                    .unwrap_or(&"unknown");
                let exposed = self.read.u8(object + 0x27c)? != 0;
                let exposed_date = if exposed {
                    Some(date_text(self.read.u32(object + 0x268)?))
                } else {
                    None
                };
                json!({"id":id, "type":self.key_at(object + 0x20)?, "owner_id":owner, "owner":self.person(owner)?,
                    "status":status, "target_type":target, "target_id":self.read.reference(object + 0x34)?,
                    "contract_id":self.read.reference(object + 0x3c)?, "artifact_id":self.read.reference(object + 0x40)?,
                    "progress":self.read.i32(object + 0x78)?, "secrecy":self.read.i32(object + 0x280)?,
                    "opportunities":self.read.i32(object + 0x298)?, "start_date":date_text(self.read.u32(object + 0x258)?),
                    "exposed":exposed, "exposed_date":exposed_date})
            }
            QueryKind::CourtPosition => {
                let employer = self.read.reference(object + 0x120)?;
                let employee = self.read.reference(object + 0x124)?;
                json!({"id":id, "type":self.key_at(object + 0x110)?, "task_type":self.key_at(object + 0x118)?,
                    "employer_id":employer, "employer":self.person(employer)?, "employee_id":employee, "employee":self.person(employee)?,
                    "hire_date":date_text(self.read.u32(object + 0x128)?)})
            }
            QueryKind::HolyOrder => {
                let founder = self.read.reference(object + 0x40)?;
                let hired = self.read.reference(object + 0x80)?;
                json!({"id":id, "type":self.key_at(object + 0x30)?, "tenet":self.key_at(object + 0x38)?,
                    "faith_id":self.read.reference(object + 0x20)?, "rite_id":self.read.reference(object + 0x24)?,
                    "title_id":self.read.reference(object + 0x28)?, "founder_id":founder, "founder":self.person(founder)?,
                    "hired_by_id":hired, "hired_by":self.person(hired)?, "worldliness":self.read.fixed(object + 0x48)?,
                    "leased_title_ids":self.read.ids(object + 0x50)?, "regiment_ids":self.read.ids(object + 0x68)?,
                    "army_regiment_ids":self.read.ids(object + 0x88)?})
            }
            QueryKind::Secret => {
                let owner = self.read.reference(object + 0x40)?;
                json!({"id":id, "type":self.key_at(object + 0x20)?, "relation_type":self.key_at(object + 0x28)?,
                    "target":self.scope_value(object + 0x30)?, "owner_id":owner, "owner":self.person(owner)?,
                    "exposed":self.read.u8(object + 0xa8)? != 0, "delete":self.read.u8(object + 0xa9)? != 0,
                    "participant_ids":self.read.ids(object + 0x60)?, "spent_by_ids":self.read.ids(object + 0x78)?,
                    "cannot_expose_ids":self.read.ids(object + 0x90)?})
            }
            QueryKind::TaskContract => {
                let owner = self.read.reference(object + 0x50)?;
                let employer = self.read.reference(object + 0x38)?;
                let location = self.read.pointer(object + 0x28)?;
                let destination = self.read.pointer(object + 0x30)?;
                let status = [
                    "not_available",
                    "available",
                    "accepted",
                    "completed",
                    "invalidated",
                ]
                .get(self.read.u32(object + 0x54)? as usize)
                .unwrap_or(&"unknown");
                json!({"id":id, "type":self.key_at(object + 0x40)?, "status":status,
                    "tier":self.read.u32(object + 0x4c)?, "name":self.read.string(object + 0x58)?,
                    "owner_id":owner, "owner":self.person(owner)?, "employer_id":employer, "employer":self.person(employer)?,
                    "target_id":self.read.reference(object + 0x48)?, "location_province_id":self.province_id(location).ok().flatten(),
                    "destination_province_id":self.province_id(destination).ok().flatten(),
                    "acceptance_date":actual_date(self.read.u32(object + 0x78)?),
                    "completion_date":actual_date(self.read.u32(object + 0x80)?),
                    "completed_contract_reward_desc":self.read.string(object + 0x88)?})
            }
            QueryKind::Contract => {
                let vassal = contract_character(&mut self.read, object + 0x20)?;
                let liege = contract_character(&mut self.read, object + 0x28)?;
                json!({"id":id, "type":self.key_at(object + 0xc8)?,
                    "vassal_id":vassal, "vassal":self.person(vassal)?,
                    "liege_id":liege, "liege":self.person(liege)?})
            }
            _ => bail!("此对象类别不支持单项查询"),
        })
    }

    fn titles_and_realms(&mut self, request: EntityList<'_>) -> Result<Value> {
        let independent = request.kind == QueryKind::Realms;
        let mut ids = Vec::new();
        self.visit_objects(TITLE, |read, id, object| {
            let holder = read.reference(object + 0x128)?;
            if (request.character.is_none() || request.character == holder)
                && (!independent || holder.is_some() && read.reference(object + 0xe8)?.is_none())
            {
                ids.push(id);
            }
            Ok(())
        })?;
        let search = request.search.to_lowercase();
        let mut seen = HashSet::new();
        let mut items = Vec::new();
        let mut count = 0;
        for id in ids {
            let title = self.title_overview(id)?;
            let title = if independent {
                let Some(ruler) = title.holder_id else {
                    continue;
                };
                if !seen.insert(ruler) {
                    continue;
                }
                let Some(primary) = self.primary_title(ruler)? else {
                    continue;
                };
                if self.character_liege(ruler, Some(&primary))?.is_some() {
                    continue;
                }
                primary
            } else {
                title
            };
            if !search.is_empty()
                && !title
                    .key
                    .as_ref()
                    .is_some_and(|key| key.to_lowercase().contains(&search))
            {
                continue;
            }
            if count >= request.offset && items.len() < request.limit {
                let mut row = serde_json::to_value(title)?;
                let holder = row["holder_id"].as_u64().map(|id| id as u32);
                row["holder"] = json!(self.person(holder)?);
                items.push(row);
            }
            count += 1;
        }
        Ok(page(count, request.offset, items))
    }

    pub(super) fn planning_cooldown(
        &mut self,
        character: u64,
        kind: QueryKind,
        key: &str,
    ) -> Result<Value> {
        let alive = self.read.pointer(character + 0x1b0)?;
        let landed = self.read.pointer(character + 0x1c0)?;
        if alive == 0 || kind == QueryKind::Decision && landed == 0 {
            return Ok(json!({"status":"unavailable", "remaining_days":null,
                "reason":if alive == 0 {"character_dead"} else {"character_not_landed"}}));
        }
        let map = if kind == QueryKind::Decision {
            landed + 0x430
        } else {
            alive + 0x500
        };
        let memory = self.read.memory;
        let header = fresh::<17>(memory, map + 8)?;
        let count = u32::from_le_bytes(array(&header[8..12]));
        let mut expires = None;
        if count != 0 {
            let buckets = decode_pointer(array(&header[..8]))?;
            let capacity = u32::from_le_bytes(array(&header[12..16]));
            let total = capacity
                .checked_add(u32::from(header[16]) + 1)
                .context("冷却时间表容量无效")?;
            ensure!(
                buckets != 0 && count <= total && total <= 4096,
                "冷却时间表无效"
            );
            let mut rows = vec![0; total as usize * 24];
            memory.read(buckets, &mut rows)?;
            for row in rows.as_chunks::<24>().0.iter().filter(|row| row[4] != 0) {
                let definition = decode_pointer(array(&row[8..16]))?;
                if self.read.key(definition)?.as_deref() == Some(key) {
                    expires = Some(u32::from_le_bytes(array(&row[16..20])));
                    break;
                }
            }
            let mut after = vec![0; rows.len()];
            memory.read(buckets, &mut after)?;
            ensure!(rows == after, "冷却时间表在查询期间变化");
        }
        ensure!(
            fresh::<17>(memory, map + 8)? == header,
            "冷却时间表在查询期间变化"
        );
        let remaining = expires.map_or(0, |date| date.saturating_sub(self.clock.date));
        Ok(
            json!({"status":"native_memory", "remaining_hours":remaining, "remaining_days": f64::from(remaining) / 24.0,
            "on_cooldown":remaining != 0, "expiration_date":expires.map(date_text)}),
        )
    }

    pub(super) fn key_at(&mut self, address: u64) -> Result<Option<String>> {
        let definition = self.read.pointer(address)?;
        self.read.key(definition)
    }

    pub(super) fn person(&mut self, id: Option<u32>) -> Result<Option<Value>> {
        id.map(|id| self.character_name(id)).transpose()
    }

    pub(super) fn scope_context(&mut self, address: u64) -> Result<Value> {
        let root = self.scope_value(address)?;
        let targets = self.scope_targets(address + 0x18)?;
        Ok(json!({"root":root, "targets":targets}))
    }

    pub(super) fn scope_targets(&mut self, address: u64) -> Result<Map<String, Value>> {
        let mut named = Map::new();
        let count = self.read.u32(address + 12)?;
        ensure!(count <= 1024, "作用域目标数量无效");
        if count == 0 {
            return Ok(named);
        }
        let entries = self.read.pointer(address)?;
        let registry = self.base + 0x5dd6570;
        let names = self.read.pointer(registry + 0x30)?;
        let name_count = self.read.u32(registry + 0x3c)?;
        ensure!(
            entries != 0 && names != 0 && count <= self.read.u32(address + 8)?,
            "作用域目标暂不可用"
        );
        for index in 0..u64::from(count) {
            let entry = entries + index * 24;
            let id = self.read.u32(entry)?;
            ensure!(id < name_count, "作用域目标名称已失效");
            let key = self.read.string(names + u64::from(id) * 32)?;
            named.insert(key, self.scope_value(entry + 8)?);
        }
        Ok(named)
    }

    pub(super) fn scope_value(&mut self, address: u64) -> Result<Value> {
        let (token, identity) = scope(&mut self.read, self.base, address)?;
        Ok(match token {
            0 => Value::Null,
            0x500 => json!({"type":"boolean", "value":identity != 0}),
            0x2ea6 => {
                let id = u32::try_from(identity).ok().and_then(reference);
                json!({"type":"char", "id":id, "character":self.person(id).ok().flatten()})
            }
            0x2ea7 => {
                let id = u32::try_from(identity).ok().and_then(reference);
                json!({"type":"lt", "id":id, "title":id.and_then(|id| self.title_overview(id).ok())})
            }
            0x4ff | 0x3594 | 0x2b3f | 0x3688 | 0x3c0c | 0x315f => {
                let kind = match token {
                    0x4ff => "prov",
                    0x3594 => "artifact",
                    0x2b3f => "war",
                    0x3688 => "court_position",
                    0x3c0c => "court_position_type",
                    _ => "council_task",
                };
                json!({"type":kind, "id":u32::try_from(identity).ok().and_then(reference)})
            }
            _ => json!({"type":"unknown", "token":token, "resolved":false}),
        })
    }
}

fn member(read: &mut ReadView<'_>, layout: Layout, object: u64, character: u32) -> Result<bool> {
    let offsets: &[u64] = match layout {
        COUNCIL_TASK | FACTION => &[0x40, 0x44],
        SCHEME => &[0x2c],
        COURT_POSITION => &[0x120, 0x124],
        HOLY_ORDER => &[0x40, 0x80],
        SECRET => &[0x40],
        TASK_CONTRACT => &[0x38, 0x48, 0x50],
        _ => &[],
    };
    for &offset in offsets {
        if read.u32(object + offset)? == character {
            return Ok(true);
        }
    }
    Ok(match layout {
        FACTION => struct_ids::<0x20>(read, object + 0x48)?.contains(&character),
        SCHEME => read.u32(object + 0x30)? == 0 && read.u32(object + 0x34)? == character,
        SECRET => {
            read.ids(object + 0x60)?.contains(&character)
                || (u16::from_le_bytes(read.array(object + 0x30)?) == 4
                    && u64::from_le_bytes(read.array(object + 0x38)?) == u64::from(character))
        }
        // Mercenary companies are available by location as well as current hire.
        MERCENARY => true,
        _ => false,
    })
}

fn contract_character(read: &mut ReadView<'_>, address: u64) -> Result<Option<u32>> {
    let character = read.pointer(address)?;
    if character == 0 {
        return Ok(None);
    }
    read.reference(character + 0x18)
}

fn struct_ids<const WIDTH: usize>(read: &mut ReadView<'_>, address: u64) -> Result<Vec<u32>> {
    Ok(read
        .vector::<WIDTH>(address)?
        .into_iter()
        .filter_map(|row| reference(u32::from_le_bytes(array(&row[8..12]))))
        .collect())
}

fn actual_date(raw: u32) -> Option<String> {
    (raw >= 5002 * 8760).then(|| date_text(raw))
}

fn scope(read: &mut ReadView<'_>, base: u64, address: u64) -> Result<(u32, u64)> {
    let raw = read.array::<16>(address)?;
    let index = u32::from(u16::from_le_bytes(array(&raw[..2])));
    if index == 0 {
        return Ok((0, 0));
    }
    let registry = base + 0x5506b90;
    let table = read.pointer(registry)?;
    let count = read.u32(registry + 12)?;
    ensure!(
        table != 0 && count <= 4096 && index < count,
        "作用域类型无效"
    );
    let token = read.u32(table + u64::from(index) * 80 + 4)?;
    Ok((token, u64::from_le_bytes(array(&raw[8..]))))
}

pub(super) fn page(count: usize, offset: usize, items: Vec<Value>) -> Value {
    let next = offset.saturating_add(items.len());
    json!({"count":count, "offset":offset, "next_offset":(next < count).then_some(next), "items":items})
}
