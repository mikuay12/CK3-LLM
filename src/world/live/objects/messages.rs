use std::collections::HashMap;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::world::{EntityList, QueryKind};

use super::governance::page;
use super::{
    Catalogs, Layout, Memory, WorldView, array, date_text, decode_pointer, fresh, read_vector,
    reference,
};

const ALERT: Layout = Layout {
    object: "局势提示",
    global: 0x5d34ee0,
    id_offset: 0x10,
    vtable: 0x4756ff0,
};

impl Catalogs {
    #[cfg(all(windows, target_arch = "x86_64"))]
    pub fn message_sample(
        &mut self,
        memory: &dyn Memory,
        base: u64,
        character: u32,
        generation: u64,
    ) -> Result<Vec<Value>> {
        let (_, items) = self.read(memory, base, |view| {
            ensure!(view.catalogs.generation == generation, "战役已变化");
            let mut items = Vec::new();
            if let Ok(first) = view.player_messages(character, 0, 1) {
                let count = first["count"].as_u64().unwrap_or_default() as usize;
                let offset = count.saturating_sub(32);
                if let Ok(rows) = view.player_messages(character, offset, 32)
                    && let Some(messages) = rows["items"].as_array()
                {
                    items.extend(messages.iter().cloned());
                }
            }
            if let Ok(mut alerts) = view.messages(EntityList {
                kind: QueryKind::Alerts,
                character: Some(character),
                search: "",
                offset: 0,
                limit: 32,
                include_inactive: false,
            }) && let Some(rows) = alerts["items"].as_array_mut()
            {
                for mut alert in rows.drain(..) {
                    alert["source"] = "native_important_action".into();
                    items.push(alert);
                }
            }
            Ok(items)
        })?;
        Ok(items)
    }
}

impl WorldView<'_> {
    pub(super) fn messages(&mut self, request: EntityList<'_>) -> Result<Value> {
        let character = request.character.or_else(|| request.search.parse().ok());
        match request.kind {
            QueryKind::Alerts => {
                let character = character.context("查询局势提示需要角色编号")?;
                let mut alerts = Vec::new();
                for id in self.read.ids(self.clock.data + 0x36510)? {
                    let object = self.lookup(ALERT, id)?;
                    if self.read.u32(object + 0x24)? == character
                        && self.read.u8(object + 0x190)? != 0
                    {
                        alerts.push((id, object));
                    }
                }
                let items = alerts.iter().skip(request.offset).take(request.limit).map(|&(id, object)| {
                    Ok(json!({"id":id, "type":self.key_at(object + 0x18)?,
                        "character_id":character, "visible":true, "scope":self.scope_context(object + 0x28)?}))
                }).collect::<Result<Vec<_>>>()?;
                Ok(page(alerts.len(), request.offset, items))
            }
            QueryKind::PlayerMessages => {
                let character = character.context("查询人物消息需要角色编号")?;
                self.player_messages(character, request.offset, request.limit)
            }
            QueryKind::MessageFeed => {
                ensure!(
                    character.is_none(),
                    "GUI 消息的接收角色未知，请按角色查询人物消息"
                );
                self.gui_messages(request.offset, request.limit)
            }
            QueryKind::PlayerSuccessions => self.successions(request),
            _ => anyhow::bail!("无效的消息查询"),
        }
    }

    fn player_messages(&mut self, character: u32, offset: usize, limit: usize) -> Result<Value> {
        let queue = self.clock.data + 0x22340;
        let memory = self.read.memory;
        let players = read_vector::<8>(memory, fresh(memory, queue)?)?;
        let mut count = 0;
        let mut items = Vec::new();
        let mut copies = HashMap::<String, usize>::new();
        for item in &players {
            let player = decode_pointer(*item)?;
            ensure!(
                self.read.pointer(player)? == self.base + 0x473cdd8,
                "人物消息列表暂不可用"
            );
            if self.read.u32(player + 0xb0)? != character {
                continue;
            }
            let header = fresh::<16>(memory, player + 0x248)?;
            let array = decode_pointer(array(&header[..8]))?;
            let capacity = u32::from_le_bytes(super::array(&header[8..12]));
            let entries = u32::from_le_bytes(super::array(&header[12..]));
            ensure!(
                entries <= capacity && entries <= 10_000 && (entries == 0 || array != 0),
                "人物消息数量无效"
            );
            let parent_offset = count;
            count += entries as usize;
            for index in (offset.saturating_sub(parent_offset)..entries as usize)
                .take(limit.saturating_sub(items.len()))
            {
                let address = array + index as u64 * 0x11a0;
                let mut message = json!({"type":self.key_at(address)?, "character_id":character,
                    "filter":self.key_at(address + 8)?, "left":self.scope_value(address + 0x18)?,
                    "right":self.scope_value(address + 0x28)?, "targets":self.scope_targets(address + 0x38)?,
                    "title":self.read.string(address + 0x1128)?, "description":self.read.string(address + 0x1148)?,
                    "created_date":date_text(self.read.u32(address + 0x1170)?),
                    "arrival_date":date_text(self.read.u32(address + 0x1178)?), "source":"native_player_message_queue"});
                let signature = format!("{:x}", Sha256::digest(serde_json::to_vec(&message)?));
                let occurrence = copies.entry(signature.clone()).or_default();
                *occurrence += 1;
                message["id"] = format!("{}:{}", &signature[..24], occurrence).into();
                items.push(message);
            }
            ensure!(
                fresh::<16>(memory, player + 0x248)? == header
                    && u32::from_le_bytes(fresh(memory, player + 0xb0)?) == character,
                "人物消息在查询期间变化"
            );
        }
        ensure!(
            read_vector::<8>(memory, fresh(memory, queue)?)? == players,
            "人物消息列表在查询期间变化"
        );
        Ok(page(count, offset, items))
    }

    fn gui_messages(&mut self, offset: usize, limit: usize) -> Result<Value> {
        let mut result = json!({"source":"native_gui_message_feed", "scope":"local_game_gui", "recipient_character_id":null,
            "status":"unavailable", "count":0, "items":[]});
        let Some(interface) = self.interface()? else {
            return Ok(result);
        };
        let handler = self.read.pointer(interface + 0x478)?;
        if handler == 0 || self.read.pointer(handler)? != self.base + 0x44fb590 {
            return Ok(result);
        }
        let memory = self.read.memory;
        let header = fresh::<16>(memory, handler + 0xe8)?;
        let entries = read_vector::<8>(memory, header)?;
        ensure!(entries.len() <= 10_000, "GUI 消息数量无效");
        let items = entries.iter().enumerate().skip(offset).take(limit).map(|(index, bytes)| {
            let item = decode_pointer(*bytes)?;
            ensure!(item != 0 && self.read.pointer(item)? == handler, "GUI 消息列表暂不可用");
            Ok(json!({"index":index, "type":self.key_at(item + 8)?, "filter":self.key_at(item + 0x10)?,
                "title":self.read.string(item + 0x11d0)?, "description":self.read.string(item + 0x11f0)?,
                "effects_text":self.read.string(item + 0x1210)?, "created_date":date_text(self.read.u32(item + 0x1178)?),
                "arrival_date":date_text(self.read.u32(item + 0x1180)?)}))
        }).collect::<Result<Vec<_>>>()?;
        ensure!(
            read_vector::<8>(memory, fresh(memory, handler + 0xe8)?)? == entries,
            "GUI 消息列表在查询期间变化"
        );
        result.as_object_mut().expect("message result").extend(
            page(entries.len(), offset, items)
                .as_object()
                .expect("page")
                .clone(),
        );
        result["status"] = "cached".into();
        Ok(result)
    }

    fn successions(&mut self, request: EntityList<'_>) -> Result<Value> {
        let machine = if request.search.is_empty() {
            None
        } else {
            Some(request.search.parse::<i32>()?)
        };
        let memory = self.read.memory;
        let queue = self.clock.data + 0x22340;
        let players = read_vector::<8>(memory, fresh(memory, queue)?)?;
        let mut rows = Vec::new();
        for &entry in &players {
            let player = decode_pointer(entry)?;
            let bytes = fresh::<0x21a>(memory, player + 0xb0)?;
            ensure!(
                u32::from_le_bytes(array(&bytes[0x2c..0x30])) == 0x506c496e,
                "战役玩家身份暂不可用"
            );
            let id = i32::from_le_bytes(array(&bytes[0x28..0x2c]));
            if machine.is_some_and(|machine| machine != id) {
                continue;
            }
            rows.push(json!({"name":self.read.string(player + 0xb8)?,
                "character_id":reference(u32::from_le_bytes(array(&bytes[..4]))), "machineid":id,
                "has_open_succession":bytes[0x1b0] != 0, "has_succession_data":bytes[0x218] != 0}));
            ensure!(
                fresh::<0x21a>(memory, player + 0xb0)? == bytes,
                "玩家继承状态在查询期间变化"
            );
        }
        ensure!(
            read_vector::<8>(memory, fresh(memory, queue)?)? == players,
            "战役玩家列表在查询期间变化"
        );
        let count = rows.len();
        Ok(page(
            count,
            request.offset,
            rows.into_iter()
                .skip(request.offset)
                .take(request.limit)
                .collect(),
        ))
    }

    pub(super) fn interface(&mut self) -> Result<Option<u64>> {
        let mut address = self.base + 0x5ccd808;
        let mut interface = 0;
        for (vtable, next) in [
            (0x44abf28, 0x28),
            (0x44e6408, 0x10),
            (0x44cc6f0, 0x88),
            (0x44cab38, 0xd0),
        ] {
            interface = self.read.pointer(address)?;
            if interface == 0 || self.read.pointer(interface)? != self.base + vtable {
                return Ok(None);
            }
            address = interface + next;
        }
        Ok(Some(interface))
    }

    #[cfg(all(windows, target_arch = "x86_64"))]
    pub(super) fn interaction_text(&mut self, id: u32, intermediary: bool) -> Result<Value> {
        let unavailable = json!({"title":null, "description":null, "status":"unavailable"});
        let Some(interface) = self.interface()? else {
            return Ok(unavailable);
        };
        let window = self.read.pointer(interface + 0xf8)?;
        if window == 0 || self.read.pointer(window)? != self.base + 0x455ce98 {
            return Ok(unavailable);
        }
        let memory = self.read.memory;
        let header = fresh::<16>(memory, window + 0x3f8)?;
        let entries = read_vector::<0xc8>(memory, header)?;
        for (index, bytes) in entries.iter().enumerate() {
            if u32::from_le_bytes(array(&bytes[..4])) != id {
                continue;
            }
            let entries = decode_pointer(array(&header[..8]))?;
            let strings = entries + index as u64 * 0xc8 + 8;
            let stage = if intermediary { 0 } else { 0x40 };
            let result = json!({"title":null, "opening":self.read.string(strings + stage)?,
                "description":self.read.string(strings + stage + 0x20)?,
                "signed":self.read.string(strings + 0x80)?, "signature":self.read.string(strings + 0xa0)?,
                "status":"partially_cached", "source":"native_interaction_text_cache"});
            ensure!(
                fresh::<16>(memory, window + 0x3f8)? == header
                    && fresh::<0xc8>(memory, strings - 8)? == *bytes,
                "互动正文缓存已变化"
            );
            return Ok(result);
        }
        Ok(unavailable)
    }
}
