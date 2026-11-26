use anyhow::{Context, Result, ensure};

use crate::notifications::watch::{Pending, Sample, Target};

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
