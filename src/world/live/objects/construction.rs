use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use super::governance::page;
use super::{CHARACTER, WorldView, array, decode_pointer, fresh};

impl WorldView<'_> {
    fn building_definitions(&mut self) -> Result<Vec<[u8; 8]>> {
        let database = self.read.pointer(self.base + 0x5c7c4c8)?;
        ensure!(database != 0, "建筑目录暂不可用");
        self.read.vector(database + 0x50)
    }

    fn building_object(&mut self, key: &str) -> Result<(usize, u64)> {
        let objects = self.building_definitions()?;
        if let Ok(index) = key.parse::<usize>() {
            let object = decode_pointer(*objects.get(index).context("建筑编号无效")?)?;
            ensure!(object != 0, "建筑定义暂不可用");
            return Ok((index, object));
        }
        for (index, &bytes) in objects.iter().enumerate() {
            let object = decode_pointer(bytes)?;
            if self.read.key(object)?.as_deref() == Some(key) {
                return Ok((index, object));
            }
        }
        anyhow::bail!("游戏中找不到建筑 {key}")
    }

    pub(super) fn building_type(&mut self, key: &str) -> Result<Value> {
        let (id, object) = self.building_object(key)?;
        self.building_info(id, object)
    }

    fn building_info(&mut self, id: usize, object: u64) -> Result<Value> {
        Ok(
            json!({"id":id, "key":self.read.key(object)?, "base_construction_days":self.read.i32(object + 0x58)?,
            "previous_building_key":self.key_at(object + 0x6f500)?,
            "base_cost":{"gold":self.read.fixed(object + 0x6f518)?, "prestige":self.read.fixed(object + 0x6f520)?,
                "piety":self.read.fixed(object + 0x6f528)?},
            "availability":"requires_game_evaluation"}),
        )
    }

    pub(super) fn building_types(
        &mut self,
        search: &str,
        offset: usize,
        limit: usize,
    ) -> Result<Value> {
        ensure!((1..=100).contains(&limit), "建筑目录每页需要 1 到 100 项");
        let search = search.to_lowercase();
        let mut count = 0;
        let mut items = Vec::new();
        for (id, bytes) in self.building_definitions()?.into_iter().enumerate() {
            let object = decode_pointer(bytes)?;
            if !self
                .read
                .key(object)?
                .is_some_and(|key| key.to_lowercase().contains(&search))
            {
                continue;
            }
            if count >= offset && items.len() < limit {
                items.push(self.building_info(id, object)?);
            }
            count += 1;
        }
        Ok(page(count, offset, items))
    }

    pub(super) fn construction_quote(
        &mut self,
        id: u32,
        building: &str,
        character: Option<u32>,
        slot: Option<i32>,
        treasury: bool,
    ) -> Result<Value> {
        let character = self.query_character(character)?;
        self.lookup(CHARACTER, character)?;
        let province = self.province_object(id)?;
        let (building_id, object) = self.building_object(building)?;
        let cached = self
            .quote_cache(province, object, character, slot)
            .ok()
            .flatten();
        let actual_slot = cached
            .as_ref()
            .and_then(|value| value["slot_index"].as_i64())
            .map(|slot| slot as i32)
            .or(slot);
        let native = self.shadow().and_then(|mut engine| {
            engine.building_quote(character, id, province, object, actual_slot, treasury)
        });
        let mut quote = native.unwrap_or_else(|error| json!({"native_error":format!("{error:#}"),
            "availability":{"status":"requires_game_evaluation"}, "cost":{}, "timing":{"status":"unavailable"}}));
        if let Some(cache) = cached {
            for name in ["gold_or_treasury", "prestige", "piety"] {
                if !cache["cost"][name].is_null() {
                    quote["cost"][name] = cache["cost"][name].clone();
                }
            }
            if !cache["construction_days"].is_null() {
                quote["timing"] = json!({"construction_days":cache["construction_days"], "source":"native_gui_cache",
                    "status":"cached","freshness":"untracked"});
            }
            quote["quote_cache"] = cache;
        }
        quote["character_id"] = character.into();
        quote["province_id"] = id.into();
        quote["slot_index"] = json!(actual_slot);
        quote["building_type"] = self.building_info(building_id, object)?;
        Ok(quote)
    }

    fn quote_cache(
        &mut self,
        province: u64,
        building: u64,
        character: u32,
        slot: Option<i32>,
    ) -> Result<Option<Value>> {
        let Some(interface) = self.interface()? else {
            return Ok(None);
        };
        let view = self.read.pointer(interface + 0xd0)?;
        if view == 0
            || self.read.pointer(view)? != self.base + 0x4553820
            || self.read.pointer(view + 0xd8)? != province
            || self.read.pointer(view + 0x1c0)? != province
        {
            return Ok(None);
        }
        let title = self.read.reference(province + 0x738)?;
        if self.title_holder(title, None)? != Some(character) {
            return Ok(None);
        }
        let memory = self.read.memory;
        let header = fresh::<16>(memory, view + 0xe8)?;
        let entries = self.read.vector::<8>(view + 0xe8)?;
        let mut matches = Vec::new();
        for entry in entries {
            let object = decode_pointer(entry)?;
            if object != 0
                && self.read.pointer(object)? == view
                && self.read.pointer(object + 8)? == building
            {
                let index = self.read.i32(object + 16)?;
                if slot.is_none_or(|slot| slot == index) {
                    matches.push((object, index));
                }
            }
        }
        let (item, index, kind, offsets) = match matches.as_slice() {
            [(item, index)] => (*item, *index, "candidate_list", [0x18, 0xf8, 0x1d8, 0x2b8]),
            [] => {
                let item = view + 0x1c8;
                let current = self.read.pointer(item + 0x458)?;
                let next = self.read.pointer(item + 0x460)?;
                let selected = self.read.pointer(item + 0x468)?;
                let index = self.read.i32(item + 0x470)?;
                if self.read.pointer(item + 0x450)? != view
                    || next != building
                    || selected != current && selected != next
                    || slot.is_some_and(|slot| slot != index)
                {
                    return Ok(None);
                }
                let gold = self.quoted_value(item + 0x478, false)?;
                let treasury = self.quoted_value(item + 0x558, false)?;
                if gold.is_some() && treasury.is_some() {
                    return Ok(None);
                }
                let currency = if treasury.is_some() { 0x558 } else { 0x478 };
                (
                    item,
                    index,
                    "upgrade_track",
                    [currency, 0x638, 0x718, 0x7f8],
                )
            }
            _ => return Ok(None),
        };
        let cost = json!({"gold_or_treasury":self.quoted_value(item + offsets[0],false)?,
            "prestige":self.quoted_value(item + offsets[1],false)?,"piety":self.quoted_value(item + offsets[2],false)?});
        let days = self.quoted_value(item + offsets[3], true)?;
        ensure!(
            fresh::<16>(memory, view + 0xe8)? == header,
            "建设报价缓存已变化"
        );
        Ok(Some(
            json!({"slot_index":index,"cost":cost,"construction_days":days,
            "cost_units":{"gold_or_treasury":self.read.string(item + offsets[0] + 0x38).ok(),
                "prestige":self.read.string(item + offsets[1] + 0x38).ok(),
                "piety":self.read.string(item + offsets[2] + 0x38).ok()},
            "cache_kind":kind,"source":"native_gui_cache","freshness":"untracked"}),
        ))
    }

    fn quoted_value(&mut self, address: u64, duration: bool) -> Result<Option<f64>> {
        let bytes = fresh::<23>(self.read.memory, address + 0x78)?;
        let kind = u32::from_le_bytes(array(&bytes[16..20]));
        let state = bytes[22];
        Ok(
            (kind == 0 && state & 4 == 0 && (!duration || state & 0x20 != 0))
                .then(|| i64::from_le_bytes(array(&bytes[..8])) as f64 / 100_000.0),
        )
    }
}
