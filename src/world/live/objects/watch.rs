use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use crate::notifications::watch::{Pending, Sample, Target};
use crate::world::{EntityList, QueryKind};

use super::{
    CHARACTER, Catalogs, Layout, Memory, WorldView, array, date_text, decode_pointer, fresh,
    pointer, read_vector, reference,
};

pub(super) const INTERACTION: Layout = Layout {
    object: "待回复互动",
    global: 0x5d33c78,
    id_offset: 0x10,
    vtable: 0x4769590,
};
pub(super) const ACTIVITY: Layout = Layout {
    object: "活动",
    global: 0x5d32c90,
    id_offset: 8,
    vtable: 0x473ea08,
};

impl Catalogs {
    pub(crate) fn sample(
        &mut self,
        memory: &dyn Memory,
        base: u64,
        target: Target,
    ) -> Result<Sample> {
        let (clock, (character, state, pending)) = self.read(memory, base, |view| {
            let character = view.subject(target)?;
            let state = view.character(character)?;
            let alive = state["identity"]["alive"].as_bool() == Some(true);
            let mut pending = Vec::new();
            if alive {
                view.pending_events(character, &mut pending)?;
                view.pending_interactions(character, &mut pending)?;
                view.activity_invitations(character, &mut pending)?;
            }
            Ok((character, state, pending))
        })?;
        Ok(Sample {
            generation: self.generation,
            character,
            date: date_text(clock.date),
            state,
            pending,
        })
    }
}

impl WorldView<'_> {
    pub(super) fn pending_detail(
        &mut self,
        kind: QueryKind,
        id: u32,
        character: Option<u32>,
    ) -> Result<Value> {
        if kind == QueryKind::Activity {
            return self.activity(id, character, false);
        }
        let character = match character {
            Some(character) => character,
            None => self.subject(Target::LocalPlayer)?,
        };
        self.lookup(CHARACTER, character)?;
        match kind {
            QueryKind::Event => self.event(id, character),
            QueryKind::IncomingInteraction => self.interaction(id, character),
            QueryKind::ActivityInvitation => self.activity(id, Some(character), true),
            _ => anyhow::bail!("无效的待办查询"),
        }
    }

    fn event(&mut self, id: u32, character: u32) -> Result<Value> {
        let memory = self.read.memory;
        let queue = self.clock.data + 0x34480 + 0x1f18;
        let entries = read_vector::<8>(memory, fresh(memory, queue)?)?;
        for entry in &entries {
            let event = decode_pointer(*entry)?;
            let header = fresh::<16>(memory, event + 0x1b0)?;
            if u32::from_le_bytes(array(&header[8..12])) != character
                || u32::from_le_bytes(array(&header[12..])) != id
            {
                continue;
            }
            let definition = decode_pointer(array(&header[..8]))?;
            ensure!(definition != 0, "待处理事件定义暂不可用");
            let count = self.read.u32(definition + 0x1ac)?;
            ensure!(count <= 64, "事件选项数无效");
            let evaluated = self
                .shadow()
                .and_then(|mut engine| engine.event_options(event));
            let mut result = json!({"id":id, "character_id":character, "event":self.read.string(definition + 0x10)?,
                "expiration_date":date_text(self.read.u32(event + 0x1a8)?),
                "title_override":self.read.string(event + 0x188).ok().filter(|value| !value.is_empty()),
                "scope":self.scope_context(event).ok(),
                "primary":self.read.u8(event + 0x1c0)? != 0, "death":self.read.u8(event + 0x1c1)? != 0,
                "options":(0..count).map(|index| json!({"index":index, "available":null,
                    "availability":"requires_game_evaluation"})).collect::<Vec<_>>()});
            match evaluated {
                Ok(options) if options.len() == count as usize => {
                    result["options"] = json!(options)
                }
                Ok(_) => anyhow::bail!("事件选项数量在查询期间变化"),
                Err(error) => result["native_error"] = format!("{error:#}").into(),
            }
            ensure!(
                fresh::<16>(memory, event + 0x1b0)? == header
                    && read_vector::<8>(memory, fresh(memory, queue)?)? == entries,
                "事件在查询期间变化"
            );
            return Ok(result);
        }
        anyhow::bail!("当前人物没有待处理事件 {id}")
    }

    fn interaction(&mut self, id: u32, character: u32) -> Result<Value> {
        let memory = self.read.memory;
        let object = self.lookup(INTERACTION, id)?;
        let (_, role) =
            interaction_owner(memory, object, character)?.context("此互动不属于当前角色")?;
        let ownership = fresh::<24>(memory, object + 0x2f0)?;
        let stage = fresh::<7>(memory, object + 0x5c0)?;
        let mut characters = Vec::new();
        for &bytes in ownership.as_chunks::<4>().0.iter().take(5) {
            let id = reference(u32::from_le_bytes(bytes));
            characters.push(id.map(|id| self.character_name(id)).transpose()?);
        }
        let definition = self.read.pointer(object + 0x18)?;
        let key = self.read.key(definition)?.context("互动定义暂不可用")?;
        let replies = self.shadow().and_then(|mut engine| engine.replies(object));
        let response_options = [("accept", "接受"), ("reject", "拒绝"), ("block", "阻止"), ("acknowledge", "确认")]
            .into_iter()
            .map(|(name,label)| json!({"id":name, "label":label,
                "available":replies.as_ref().ok().map(|value| &value[name]),
                "availability":if replies.is_ok() {"native_shadow_unicorn"} else {"requires_game_evaluation"},
                "action":{"action":"reply_character_interaction", "parameters":{"character_interaction":id, "accept":name}}}))
            .collect::<Vec<_>>();
        let mut result = json!({"id":id, "character_id":character, "role":if role == 1 {"intermediary"} else {"recipient"},
            "interaction":{"key":key}, "actor":characters[0], "recipient":characters[1],
            "secondary_actor":characters[2], "secondary_recipient":characters[3], "intermediary":characters[4],
            "days_in_queue":self.read.i32(object + 0x5b8)?, "ai_reply_delay":self.read.i32(object + 0x5bc)?,
            "intermediary_status":u32::from_le_bytes(array(&stage[..4])),
            "ai_intermediary_accepts":stage[4] != 0, "ai_accepts":stage[5] != 0, "is_notification":stage[6] != 0,
            "selected_options":self.read.vector::<1>(object + 0x318)?.into_iter().map(|flag| flag[0] != 0).collect::<Vec<_>>(),
            "scope":self.scope_context(object + 0x20).ok(), "original_scope":self.scope_context(object + 0x188).ok(),
            "display":self.interaction_text(id, role == 1).ok(),
            "response_options":response_options});
        if let Err(error) = replies {
            result["native_error"] = format!("{error:#}").into();
        }
        ensure!(
            fresh::<24>(memory, object + 0x2f0)? == ownership
                && fresh::<7>(memory, object + 0x5c0)? == stage
                && interaction_owner(memory, object, character)?
                    .is_some_and(|(_, current)| current == role),
            "互动在查询期间变化"
        );
        Ok(result)
    }

    fn activity(&mut self, id: u32, character: Option<u32>, invited: bool) -> Result<Value> {
        let memory = self.read.memory;
        let mut invitation_queue = None;
        if invited {
            let character = self.lookup(CHARACTER, character.context("查询邀请需要角色编号")?)?;
            let alive = pointer(memory, character + 0x1b0)?;
            ensure!(alive != 0, "活动邀请需要在世角色");
            let queue = alive + 0x550;
            let entries = read_vector::<4>(memory, fresh(memory, queue)?)?;
            ensure!(
                entries.contains(&id.to_le_bytes()),
                "当前角色没有这项活动邀请"
            );
            invitation_queue = Some((character, alive, queue, entries));
        }
        let object = self.lookup(ACTIVITY, id)?;
        let header = fresh::<0x82>(memory, object + 0x3a0)?;
        let definition = decode_pointer(array(&header[..8]))?;
        let key = self.read.key(definition)?.context("活动定义暂不可用")?;
        let host = reference(u32::from_le_bytes(array(&header[8..12])));
        let phases = read_vector::<0x38>(memory, array(&header[0x28..0x38]))?;
        let rows = phases
            .iter()
            .enumerate()
            .map(|(index, row)| {
                let phase = decode_pointer(array(&row[..8]))?;
                Ok(json!({"index":index, "key":self.read.key(phase)?,
                "province_id":reference(u32::from_le_bytes(array(&row[8..12])))}))
            })
            .collect::<Result<Vec<_>>>()?;
        let attendees = self.read.ids(object + 0x528)?;
        let completed = header[0x81] != 0;
        let mut result = json!({"id":id, "character_id":character,
            "activity":{"key":key}, "host":host.map(|id| self.character_name(id)).transpose()?,
            "active":header[0x80] != 0, "completed":completed,
            "current_phase":i32::from_le_bytes(array(&header[0x40..0x44])),
            "creation_date":date_text(u32::from_le_bytes(array(&header[0x4c..0x50]))),
            "active_start_date":date_text(u32::from_le_bytes(array(&header[0x54..0x58]))),
            "phases":rows, "attending_character_ids":attendees});
        if let Some(character) = character {
            let join = self
                .shadow()
                .and_then(|mut engine| engine.activity_join(object, character));
            let mut options = vec![json!({"id":"join", "label":"参加",
                "available":join.as_ref().ok().map(|value| &value["can_join"]),
                "availability":if join.is_ok() {"native_shadow_unicorn"} else {"requires_game_evaluation"},
                "action":{"action":"join_activity_command", "parameters":{"activity":id, "character":character, "travel":"yes"}}})];
            if invited {
                result["invitation_state"] = "invited".into();
                options.push(json!({"id":"decline", "label":"拒绝", "available":!completed,
                    "action":{"action":"decline_activity_invite_command", "parameters":{"activity":id, "character":character}}}));
            }
            result["response_options"] = json!(options);
        }
        ensure!(
            fresh::<0x82>(memory, object + 0x3a0)? == header
                && read_vector::<0x38>(memory, array(&header[0x28..0x38]))? == phases
                && read_vector::<4>(memory, fresh(memory, object + 0x528)?)?
                    .into_iter()
                    .map(u32::from_le_bytes)
                    .eq(attendees),
            "活动在查询期间变化"
        );
        if let Some((object, alive, queue, entries)) = invitation_queue {
            ensure!(
                pointer(memory, object + 0x1b0)? == alive
                    && read_vector::<4>(memory, fresh(memory, queue)?)? == entries,
                "活动邀请在查询期间变化"
            );
        }
        Ok(result)
    }

    pub(super) fn activities(&mut self, request: EntityList<'_>) -> Result<Value> {
        let mut ids = Vec::new();
        self.visit_objects(ACTIVITY, |read, id, object| {
            let completed = read.u8(object + 0x421)? != 0;
            if request.include_inactive || !completed {
                ids.push(id);
            }
            Ok(())
        })?;
        let items = ids
            .iter()
            .skip(request.offset)
            .take(request.limit)
            .map(|&id| self.activity(id, request.character, false))
            .collect::<Result<Vec<_>>>()?;
        Ok(super::governance::page(ids.len(), request.offset, items))
    }

    fn subject(&mut self, target: Target) -> Result<u32> {
        let players = match target {
            Target::Character(id) => return Ok(id),
            Target::Machine(id) => {
                return self
                    .players()?
                    .iter()
                    .find(|player| player.machineid == id)
                    .and_then(|player| player.character_id)
                    .context("等待虚拟玩家进入战役");
            }
            Target::LocalPlayer => self.players()?,
        };
        let mut characters = players.iter().filter_map(|player| player.character_id);
        let character = characters.next().context("请输入要跟踪的角色 ID")?;
        ensure!(
            characters.next().is_none(),
            "当前有多个玩家，请输入要跟踪的角色 ID"
        );
        Ok(character)
    }

    fn pending_events(&mut self, character: u32, out: &mut Vec<Pending>) -> Result<()> {
        let memory = self.read.memory;
        let queue = self.clock.data + 0x34480 + 0x1f18;
        let entries = read_vector::<8>(memory, fresh(memory, queue)?)?;
        for &entry in &entries {
            let object = decode_pointer(entry)?;
            let header = fresh::<16>(memory, object + 0x1b0)?;
            let owner = u32::from_le_bytes(array(&header[8..12]));
            if owner == character {
                let definition = decode_pointer(array(&header[..8]))?;
                ensure!(definition != 0, "待处理事件定义暂不可用");
                let id = u32::from_le_bytes(array(&header[12..]));
                let event = self.read.string(definition + 0x10)?;
                ensure!(
                    fresh::<16>(memory, object + 0x1b0)? == header,
                    "待处理事件在读取期间变化"
                );
                out.push(Pending {
                    group: "pending_events",
                    kind: "pending_event",
                    id,
                    stage: 0,
                    text: format!("待处理事件 {event}（事件编号 {id}）"),
                });
            }
        }
        ensure!(
            read_vector::<8>(memory, fresh(memory, queue)?)? == entries,
            "待处理事件列表在读取期间变化"
        );
        Ok(())
    }

    fn pending_interactions(&mut self, character: u32, out: &mut Vec<Pending>) -> Result<()> {
        let mut matches = Vec::new();
        self.visit_objects(INTERACTION, |read, id, object| {
            if interaction_owner(read.memory, object, character)?.is_some() {
                matches.push(id);
            }
            Ok(())
        })?;
        for id in matches {
            let object = self.lookup(INTERACTION, id)?;
            let Some((actor, stage)) = interaction_owner(self.read.memory, object, character)?
            else {
                continue;
            };
            let definition = self.read.pointer(object + 0x18)?;
            let key = self.read.key(definition)?.context("人物互动定义暂不可用")?;
            ensure!(
                interaction_owner(self.read.memory, object, character)? == Some((actor, stage)),
                "人物互动的接收者在读取期间变化"
            );
            let actor =
                reference(actor).map_or_else(|| "未知人物".into(), |id| format!("人物 {id}"));
            let action = if stage == 1 {
                "请你转达"
            } else {
                "请你回复"
            };
            out.push(Pending {
                group: "pending_interactions",
                kind: "pending_interaction",
                id,
                stage,
                text: format!("{actor}{action} {key}（请求编号 {id}）"),
            });
        }
        Ok(())
    }

    fn activity_invitations(&mut self, character: u32, out: &mut Vec<Pending>) -> Result<()> {
        let object = self.lookup(CHARACTER, character)?;
        let memory = self.read.memory;
        let alive = pointer(memory, object + 0x1b0)?;
        ensure!(alive != 0, "角色活动邀请在读取期间变化");
        let queue = alive + 0x550;
        let entries = read_vector::<4>(memory, fresh(memory, queue)?)?;
        for entry in &entries {
            let id = u32::from_le_bytes(*entry);
            let activity = self.lookup(ACTIVITY, id)?;
            let header = fresh::<12>(memory, activity + 0x3a0)?;
            let definition = decode_pointer(array(&header[..8]))?;
            let host = reference(u32::from_le_bytes(array(&header[8..])));
            let key = self.read.key(definition)?.context("活动定义暂不可用")?;
            let host = host.map_or_else(|| "未知主办人".into(), |id| format!("人物 {id}"));
            ensure!(
                fresh::<12>(memory, activity + 0x3a0)? == header,
                "活动邀请在读取期间变化"
            );
            out.push(Pending {
                group: "activity_invitations",
                kind: "activity_invitation",
                id,
                stage: 0,
                text: format!("{host}邀请参加 {key}（活动编号 {id}）"),
            });
        }
        ensure!(
            pointer(memory, object + 0x1b0)? == alive
                && read_vector::<4>(memory, fresh(memory, queue)?)? == entries,
            "活动邀请列表在读取期间变化"
        );
        Ok(())
    }
}

fn interaction_owner(
    memory: &dyn Memory,
    object: u64,
    character: u32,
) -> Result<Option<(u32, u8)>> {
    let bytes = fresh::<20>(memory, object + 0x2f0)?;
    let actor = u32::from_le_bytes(array(&bytes[..4]));
    let recipient = u32::from_le_bytes(array(&bytes[4..8]));
    let intermediary = u32::from_le_bytes(array(&bytes[16..]));
    let stage = u32::from_le_bytes(fresh(memory, object + 0x5c0)?);
    Ok(if intermediary == character && stage == 1 {
        Some((actor, 1))
    } else if recipient == character {
        Some((actor, 0))
    } else {
        None
    })
}
