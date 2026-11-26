use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use super::Shadow;
use super::unicorn::{R13, RAX, RBP, RBX, RCX, RDX};

const SCALE: f64 = 100_000.0;
const ABSENT: u32 = u32::MAX;
const SOURCE: &str = "native_shadow_unicorn";

#[derive(Deserialize)]
pub(in crate::world::live::objects) struct Interaction<'a> {
    #[serde(rename = "character_interaction")]
    name: &'a str,
    pub actor: u32,
    pub recipient: u32,
    pub secondary_actor: u32,
    pub secondary_recipient: u32,
    intermediary: u32,
    puppet_or_actor: u32,
    #[serde(borrow)]
    scope: Root<'a>,
    target: Option<Scope<'a>>,
    options: Vec<bool>,
}

#[derive(Deserialize)]
struct Root<'a> {
    #[serde(borrow)]
    root: Scope<'a>,
}

#[derive(Deserialize)]
struct Scope<'a> {
    #[serde(rename = "type")]
    kind: Option<&'a str>,
    #[serde(default)]
    identity: u64,
}

pub(crate) struct Acceptance {
    pub ai_accept: f64,
    pub ai_intermediary_accept: f64,
}

impl Shadow<'_> {
    fn u32(&mut self, address: u64) -> Result<u32> {
        self.read(address).map(u32::from_le_bytes)
    }

    fn u64(&mut self, address: u64) -> Result<u64> {
        self.read(address).map(u64::from_le_bytes)
    }

    fn fixed(&mut self, address: u64) -> Result<f64> {
        Ok(i64::from_le_bytes(self.read(address)?) as f64 / SCALE)
    }

    fn result(&mut self, rva: u64, output: u64, arguments: &[u64]) -> Result<f64> {
        self.write64(output, 0)?;
        self.call(rva, arguments)?;
        self.fixed(output)
    }

    fn boolean(&mut self, rva: u64, arguments: &[u64]) -> Result<bool> {
        Ok(self.call(rva, arguments)? as u8 != 0)
    }

    fn definition(&mut self, database: u64, name: &str) -> Result<u64> {
        ensure!(
            !name.is_empty() && !name.contains('\0'),
            "游戏定义的脚本键无效"
        );
        let key = self.alloc(name.len() as u64 + 1)?;
        self.write(key, name.as_bytes())?;
        self.write32(self.base() + 0x5cd44ac, 0)?;
        let object = self.call(0x3f93e90, &[self.base() + database, key])?;
        ensure!(object != 0, "游戏内未加载定义 {name}");
        Ok(object)
    }

    fn scope_type(&mut self, kind: &str) -> Result<u16> {
        let token = match kind {
            "character" | "char" => return Ok(4),
            "boolean" => 0x500,
            "prov" | "province" => 0x4ff,
            "lt" | "landed_title" => 0x2ea7,
            "artifact" => 0x3594,
            "war" => 0x2b3f,
            "court_position" => 0x3688,
            "court_position_type" => 0x3c0c,
            "council_task" => 0x315f,
            _ => anyhow::bail!("原生求值暂不支持 {kind} 作用域"),
        };
        let header = self.call(0x37a8320, &[])?;
        let table = self.u64(header)?;
        let count = self.u32(header + 0xc)?;
        ensure!(
            table != 0 && (1..=512).contains(&count),
            "游戏作用域目录暂不可用"
        );
        for index in 0..count {
            if self.u32(table + u64::from(index) * 0x50 + 4)? == token {
                return Ok(index as u16);
            }
        }
        anyhow::bail!("游戏未加载 {kind} 作用域")
    }

    fn top_scope(&mut self, address: u64, kind: &str, identity: u64) -> Result<()> {
        let kind = self.scope_type(kind)?;
        self.write(address, &kind.to_le_bytes())?;
        self.write64(address + 8, identity)?;
        self.write32(address + 0x10, u32::try_from(identity)?)?;
        self.call(0x88bc90, &[address + 0x18])?;
        let base = self.base();
        self.write64(address + 0x110, base + 0x54f22b0)?;
        self.write64(address + 0x118, base + 0x449d368)?;
        self.write64(address + 0x120, base + 0x449d3d8)?;
        self.write64(address + 0x138, base + 0x54f22b8)?;
        self.write32(address + 0x160, ABSENT)
    }

    pub fn acceptance(&mut self, interaction: &Interaction<'_>) -> Result<Acceptance> {
        let data = self.interaction_data(interaction)?;
        let output = self.alloc(8)?;
        self.call(0x3088d40, &[data, 0])
            .context("初始化互动作用域")?;
        Ok(Acceptance {
            ai_accept: self.result(0x308c740, output, &[data, output])?,
            ai_intermediary_accept: self.result(0x308c640, output, &[data, output])?,
        })
    }

    fn interaction_data(&mut self, interaction: &Interaction<'_>) -> Result<u64> {
        let definition = self.definition(0x554cf70, interaction.name)?;
        let data = self.alloc(0x338)?;
        self.write64(data, definition)?;
        let (kind, identity) = interaction
            .scope
            .root
            .kind
            .map_or(("character", u64::from(interaction.actor)), |kind| {
                (kind, interaction.scope.root.identity)
            });
        self.top_scope(data + 8, kind, identity)?;
        self.top_scope(data + 0x170, "character", u64::from(interaction.actor))?;
        let characters = [
            interaction.actor,
            interaction.recipient,
            interaction.secondary_actor,
            interaction.secondary_recipient,
            interaction.intermediary,
            interaction.puppet_or_actor,
        ];
        for (index, id) in characters.into_iter().enumerate() {
            self.write32(data + 0x2d8 + index as u64 * 4, id)?;
        }
        if let Some(target) = &interaction.target {
            let kind = self.scope_type(target.kind.context("目标缺少作用域类型")?)?;
            self.write(data + 0x2f0, &kind.to_le_bytes())?;
            self.write64(data + 0x2f8, target.identity)?;
        }
        let count = interaction.options.len().max(32);
        let address = self.alloc(count as u64)?;
        let options: Vec<_> = interaction.options.iter().copied().map(u8::from).collect();
        self.write(address, &options)?;
        self.write64(data + 0x300, address)?;
        self.write32(data + 0x308, u32::try_from(count)?)?;
        self.write32(
            data + 0x30c,
            u32::try_from(if options.is_empty() {
                count
            } else {
                options.len()
            })?,
        )?;
        Ok(data)
    }

    pub fn declaration(
        &mut self,
        actor: u32,
        recipient: u32,
        cb: &str,
        titles: &[u32],
        claimant: u32,
    ) -> Result<Value> {
        ensure!(
            cb.len() <= 255 && titles.len() <= 100,
            "宣战预览参数超出游戏限制"
        );
        let data = self.interaction_data(&Interaction {
            name: "declare_war_interaction",
            actor,
            recipient,
            secondary_actor: ABSENT,
            secondary_recipient: ABSENT,
            intermediary: ABSENT,
            puppet_or_actor: actor,
            scope: Root {
                root: Scope {
                    kind: None,
                    identity: 0,
                },
            },
            target: Some(Scope {
                kind: Some("boolean"),
                identity: 1,
            }),
            options: vec![false],
        })?;
        let casus_belli = self.definition(0x55da828, cb)?;
        let special = self.alloc(0x30)?;
        self.write64(special, self.base() + 0x4540228)?;
        self.write64(special + 8, casus_belli)?;
        if !titles.is_empty() {
            let entries = self.alloc((titles.len() * 4) as u64)?;
            for (index, &title) in titles.iter().enumerate() {
                self.write32(entries + index as u64 * 4, title)?;
            }
            self.write64(special + 0x10, entries)?;
            self.write32(special + 0x18, titles.len() as u32)?;
            self.write32(special + 0x1c, titles.len() as u32)?;
        }
        self.write32(special + 0x28, claimant)?;
        self.write64(data + 0x330, special)?;
        self.call(0x3088d40, &[data, 0])?;
        let valid = self.boolean(0x308aa50, &[data])?;
        let reason = self.alloc(32)?;
        self.write64(reason + 0x18, 15)?;
        let can_send = self.boolean(0x308c320, &[data, reason])?;
        let length = self.u64(reason + 0x10)?;
        let capacity = self.u64(reason + 0x18)?;
        let text = if length != 0 {
            ensure!(
                length <= capacity && length <= 65_536,
                "宣战预览返回无效文本"
            );
            let address = if capacity < 16 {
                reason
            } else {
                self.u64(reason)?
            };
            let mut bytes = vec![0; length as usize];
            self.state.read(address, &mut bytes)?;
            Some(
                String::from_utf8_lossy(&bytes)
                    .chars()
                    .filter(|ch| !ch.is_control() || ch.is_whitespace())
                    .collect::<String>(),
            )
        } else {
            None
        };
        Ok(
            json!({"actor":actor, "recipient":recipient, "casus_belli":cb, "titles":titles, "claimant":claimant,
            "base_valid":valid, "can_send":can_send, "reason":text, "source":SOURCE}),
        )
    }

    pub fn building_quote(
        &mut self,
        character: u32,
        id: u32,
        province: u64,
        building: u64,
        slot: Option<i32>,
        treasury: bool,
    ) -> Result<Value> {
        let output = self.alloc(8)?;
        let flags = self.u64(building + 0x6f510)?;
        let base_days = i64::from(self.u32(building + 0x58)? as i32) * 100_000;
        let gold = self.u64(building + 0x6f518)?;
        let prestige = self.u64(building + 0x6f520)?;
        let piety = self.u64(building + 0x6f528)?;
        let gold = self.result(
            0x2c33390,
            output,
            &[
                output,
                province,
                gold,
                flags,
                if treasury { 3 } else { 0 },
                0,
            ],
        )?;
        let prestige = self.result(0x2c33580, output, &[output, province, prestige, flags, 0])?;
        let piety = self.result(0x2c33760, output, &[output, province, piety, flags, 0])?;
        let days = self.result(
            0x2c33bd0,
            output,
            &[output, province, base_days as u64, flags, 0],
        )?;
        let (can_build, ignore_cost) = if let Some(slot) = slot {
            let slot = u64::from(slot as u32);
            (
                Some(self.boolean(
                    0x2c87590,
                    &[character.into(), id.into(), building, slot, 1, 0],
                )?),
                Some(self.boolean(
                    0x2c87590,
                    &[character.into(), id.into(), building, slot, 0, 0],
                )?),
            )
        } else {
            (None, None)
        };
        Ok(
            json!({"cost":{"gold_or_treasury":gold,"prestige":prestige,"piety":piety},
            "timing":{"construction_days":days,"source":SOURCE},
            "availability":{"can_construct":can_build,"can_construct_ignoring_cost":ignore_cost,"status":SOURCE}}),
        )
    }

    pub fn movement(&mut self, unit: u64) -> Result<Value> {
        let output = self.alloc(8)?;
        let cost = self.result(0x24b2c00, output, &[unit, output])?;
        let cost_raw = self.u64(output)?;
        let progress = self.result(0x24b3680, output, &[unit, output])?;
        let threshold = self.fixed(self.base() + 0x5c7eb08)?;
        let speed = self.fixed(unit + 0x190)?;
        let days = if cost > 0.0 && speed > 0.0 {
            let frame = self.alloc(0x300)?;
            self.write64(frame + 0x2b8, cost_raw)?;
            Some(self.branch(
                0x24b520a,
                0x24b533e,
                &[(RBP, frame), (RBX, unit), (R13, 0)],
                RDX,
            )?)
        } else {
            None
        };
        Ok(
            json!({"leg_movement_cost":cost, "leg_progress_fraction":progress,
            "movement_locked":progress > threshold, "estimated_next_arrival_days":days}),
        )
    }

    pub fn regiment(&mut self, regiment: u64) -> Result<(u32, u32)> {
        let maximum = self.u32(regiment + 0x128)?;
        let current = self.branch(0xcce339, 0xcce37f, &[(RAX, regiment)], RAX)? as u32;
        Ok((current, maximum))
    }

    pub fn mercenary(&mut self, company: u64) -> Result<(u32, u32)> {
        let current = self.call(0x26274c0, &[company])? as u32;
        let maximum = self.call(0x26275d0, &[company])? as u32;
        Ok((current, maximum))
    }

    pub fn siege(&mut self, siege: u64) -> Result<(f64, f64)> {
        let output = self.alloc(8)?;
        let points = self.result(0x2526600, output, &[siege, output])?;
        let ratio = self.result(0x25252a0, output, &[siege, output])?;
        Ok((points, ratio * 100.0))
    }

    pub fn siege_projection(
        &mut self,
        siege: u64,
        commander: u32,
        points: f64,
        progress: f64,
        date: u32,
    ) -> Result<Value> {
        let output = self.alloc(8)?;
        let rate = self.result(0x2527a50, output, &[siege, output, commander.into(), 0, 0])?;
        let days = (rate > 0.0).then(|| ((points - progress).max(0.0) / rate).ceil() as u64);
        let completion = days.and_then(|days| {
            u32::try_from(u64::from(date).checked_add(days.checked_mul(24)?)?).ok()
        });
        Ok(
            json!({"status":SOURCE, "points_per_day":rate, "estimated_days_remaining":days,
            "estimated_completion_date":completion.map(super::super::date_text)}),
        )
    }

    pub fn construction(&mut self, construction: u64, province: u64, date: u32) -> Result<Value> {
        let output = self.alloc(8)?;
        self.call(0x24763a0, &[construction, output, province])?;
        let end = self.u32(output)?;
        let percent = self.result(0x24761c0, output, &[construction, output, province])?;
        Ok(
            json!({"source":SOURCE, "end_date":super::super::date_text(end),
            "days_remaining":end.saturating_sub(date).div_ceil(24), "progress_percent":percent}),
        )
    }

    pub fn casualties(&mut self, side: u64) -> Result<f64> {
        let entries = self.u64(side + 0x38)?;
        let capacity = self.u32(side + 0x40)?;
        let count = self.u32(side + 0x44)?;
        ensure!(
            count <= capacity && count <= 4096 && (count == 0 || entries != 0),
            "战报兵团数组无效"
        );
        let mut casualties = 0_f64;
        for index in 0..count {
            let entry = entries + u64::from(index) * 0x50;
            let lost = self.branch(0x2677801, 0x2677812, &[(RCX, entry)], RAX)? as i64;
            casualties += lost as f64 / SCALE;
        }
        Ok(casualties)
    }

    pub fn supply_limit(&mut self, province: u64, character: u64) -> Result<i32> {
        Ok(self.call(0x2482f30, &[province, character, character, 0, 0])? as i32)
    }

    fn cost(
        &mut self,
        cost: u64,
        scope: u64,
        character: u64,
    ) -> Result<(bool, Map<String, Value>)> {
        let affordable = self.boolean(0x311b560, &[cost, scope, character, 0])?;
        let output = self.alloc(0x60)?;
        self.call(0x311d090, &[cost, scope, output])?;
        let mut values = Map::new();
        for (index, name) in [
            "gold",
            "prestige",
            "piety",
            "renown",
            "influence",
            "herd",
            "provisions",
            "treasury",
            "merit",
            "barter_goods",
        ]
        .into_iter()
        .enumerate()
        {
            let amount = self.fixed(output + index as u64 * 8)?;
            if index < 3 || name == "treasury" || amount != 0.0 {
                values.insert(name.into(), amount.into());
            }
        }
        Ok((affordable, values))
    }

    pub fn decision(&mut self, id: u32, character: u64, name: &str) -> Result<Value> {
        let definition = self.definition(0x559ab80, name)?;
        let shown = self.boolean(0x31135b0, &[definition, character])?;
        let scope = self.alloc(0x180)?;
        self.top_scope(scope, "character", id.into())?;
        let valid = self.boolean(0x31136c0, &[definition, character, scope, 0, 0])?;
        let cost = self.call(0x1474800, &[definition])?;
        let (affordable, cost) = self.cost(cost, scope, character)?;
        Ok(
            json!({"shown":shown, "valid":valid, "can_afford":affordable,
            "available":shown && valid && affordable, "cost":cost, "source":SOURCE}),
        )
    }

    pub fn activity_type(&mut self, id: u32, character: u64, name: &str) -> Result<Value> {
        let definition = self.definition(0x55d51b0, name)?;
        let scope = self.alloc(0x180)?;
        self.top_scope(scope, "character", id.into())?;
        let shown = self.trigger(definition + 0x40, scope, true)?;
        let can_start = self.boolean(0x2bcbeb0, &[definition, scope, 0, 1])?;
        let (affordable, cost) = self.cost(definition + 0x1368, scope, character)?;
        Ok(
            json!({"shown":shown, "can_start":can_start, "can_afford":affordable,
            "available":shown && can_start && affordable, "cost":cost, "source":SOURCE}),
        )
    }

    fn trigger(&mut self, trigger: u64, scope: u64, empty: bool) -> Result<bool> {
        if self.u32(trigger + 0x4c)? == 0 {
            Ok(empty)
        } else {
            self.boolean(0x37407c0, &[trigger, scope])
        }
    }

    pub fn event_options(&mut self, event: u64) -> Result<Vec<Value>> {
        let definition = self.u64(event + 0x1b0)?;
        ensure!(definition != 0, "事件尚未加载脚本定义");
        let options = self.u64(definition + 0x1a0)?;
        let count = self.u32(definition + 0x1ac)?;
        ensure!(
            count <= 64 && (count == 0 || options != 0),
            "事件选项数组无效"
        );
        (0..count)
            .map(|index| {
                let option = self.u64(options + u64::from(index) * 8)?;
                ensure!(option != 0, "事件选项尚未初始化");
                let available = self.trigger(option + 0x80, event, true)?;
                let unavailable = self.trigger(option + 0x150, event, false)?;
                Ok(
                    json!({"index":index, "available":available, "shown":available || unavailable,
                "show_as_unavailable":unavailable, "availability":SOURCE}),
                )
            })
            .collect()
    }

    pub fn activity_join(&mut self, activity: u64, character: u32) -> Result<Value> {
        let can_join = self.boolean(0x2bcb140, &[activity, character.into(), 0, 0])?;
        Ok(json!({"source":SOURCE, "can_join":can_join}))
    }

    pub fn replies(&mut self, pending: u64) -> Result<Value> {
        let data = pending + 0x18;
        let definition = self.u64(data)?;
        let accept =
            self.boolean(0x308aa50, &[data])? && self.boolean(0x308ae50, &[data, 0, 0, 0])?;
        let distinct = self.u32(data + 0x2d8)? != self.u32(data + 0x2dc)?;
        let (reject, block) = if accept && distinct && definition != 0 {
            let condition = self.u64(definition + 0x2290)?;
            let auto = if condition == 0 {
                self.read::<1>(definition + 0x2718)?[0] != 0
            } else {
                self.boolean(0x37407c0, &[condition, data + 8])?
            };
            (
                !auto,
                !auto && self.boolean(0x37407c0, &[definition + 0x1098, data + 8])?,
            )
        } else {
            (false, false)
        };
        Ok(
            json!({"accept":accept, "reject":reject, "block":block, "acknowledge":true, "source":SOURCE}),
        )
    }
}
