use std::collections::BTreeSet;

use anyhow::{Result, bail, ensure};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::world::{MilitaryList, QueryKind};

use super::{
    CHARACTER, Catalogs, Layout, Memory, PROVINCE_VTABLE, WorldView, array, date_text,
    decode_pointer, fresh, reference,
};

use crate::notifications::watch::{Military, Motion};

pub(super) const WAR: Layout = Layout {
    object: "战争",
    global: 0x5d32e68,
    id_offset: 8,
    vtable: 0x4743d48,
};
pub(super) const ARMY: Layout = Layout {
    object: "军队",
    global: 0x5d32e58,
    id_offset: 0x10,
    vtable: 0x4746d80,
};
const REGIMENT: Layout = Layout {
    object: "兵团",
    global: 0x5d33b68,
    id_offset: 0x10,
    vtable: 0x4755708,
};
pub(super) const UNIT: Layout = Layout {
    object: "移动单位",
    global: 0x5d33390,
    id_offset: 0x10,
    vtable: 0x4744430,
};
const ARMY_REGIMENT: Layout = Layout {
    object: "出征兵团",
    global: 0x5d34328,
    id_offset: 0x10,
    vtable: 0x4755918,
};
pub(super) const COMBAT_RESULT: Layout = Layout {
    object: "战报",
    global: 0x5d34fb8,
    id_offset: 8,
    vtable: 0x4757750,
};
pub(super) const COMBAT: Layout = Layout {
    object: "交战",
    global: 0x5d32e80,
    id_offset: 8,
    vtable: 0x474def8,
};
pub(super) const SIEGE: Layout = Layout {
    object: "围城",
    global: 0x5d33c88,
    id_offset: 8,
    vtable: 0x474a5b8,
};
pub(super) const MERCENARY: Layout = Layout {
    object: "佣兵团",
    global: 0x5d32f20,
    id_offset: 0x10,
    vtable: 0x47699c8,
};

#[derive(Serialize)]
pub(super) struct Unit {
    pub id: u32,
    #[serde(rename = "type")]
    kind: &'static str,
    pub owner_id: Option<u32>,
    pub province_id: Option<u32>,
    pub army_id: Option<u32>,
    pub path: Vec<u32>,
    movement_progress: f64,
    pub retreating: bool,
    arrival_date: Option<String>,
}

#[derive(Serialize)]
struct ArmyRegiment {
    id: u32,
    #[serde(rename = "type")]
    kind: Option<String>,
    current: u32,
    maximum: u32,
    army_id: Option<u32>,
    vassal_id: Option<u32>,
    knight_id: Option<u32>,
}

#[derive(Serialize)]
pub(super) struct Edge {
    pub province_id: u32,
    pub crossing: &'static str,
    pub through_province_id: Option<u32>,
    pub movement_cost: f64,
}

impl WorldView<'_> {
    pub(super) fn military_list(
        &mut self,
        kind: MilitaryList,
        character: u32,
        offset: usize,
        limit: usize,
    ) -> Result<Value> {
        ensure!((1..=100).contains(&limit), "列表每页需要 1 到 100 项");
        let mut armies = BTreeSet::new();
        for unit in self.units_for(character)? {
            if unit.owner_id == Some(character) {
                armies.extend(unit.army_id);
            }
        }
        let ids = match kind {
            MilitaryList::Wars => self.landed_ids(character, 0x318)?,
            MilitaryList::Armies => armies.iter().copied().collect(),
            MilitaryList::Combats => self.combat_ids(character, &armies)?,
            MilitaryList::CombatResults => self.report_ids(character)?,
            MilitaryList::Sieges => self
                .siege_ids(character, &armies)?
                .into_iter()
                .map(|(id, _)| id)
                .collect(),
        };
        let count = ids.len();
        let items = ids
            .into_iter()
            .skip(offset)
            .take(limit)
            .map(|id| match kind {
                MilitaryList::Wars => self.war(id),
                MilitaryList::Armies => self.army(id),
                MilitaryList::Combats => self.combat(id),
                MilitaryList::CombatResults => self.combat_result(id),
                MilitaryList::Sieges => self.siege(id),
            })
            .collect::<Result<Vec<_>>>()?;
        let next = offset.saturating_add(items.len());
        Ok(
            json!({"count":count,"offset":offset,"next_offset":(next < count).then_some(next),"items":items}),
        )
    }

    fn landed_ids(&mut self, character: u32, offset: u64) -> Result<Vec<u32>> {
        let character = self.lookup(CHARACTER, character)?;
        let landed = self.read.pointer(character + 0x1c0)?;
        if landed == 0 {
            Ok(Vec::new())
        } else {
            self.read.ids(landed + offset)
        }
    }

    pub(super) fn units_for(&mut self, character: u32) -> Result<Vec<Unit>> {
        self.landed_ids(character, 0x278)?
            .into_iter()
            .map(|id| self.unit(id))
            .collect()
    }

    fn combat_ids(&mut self, character: u32, armies: &BTreeSet<u32>) -> Result<Vec<u32>> {
        let mut result = Vec::new();
        self.visit_objects(COMBAT, |read, id, object| {
            for side in [object + 0x20, object + 0x368] {
                if read.reference(side + 0x70)? == Some(character)
                    || read.reference(side + 0x74)? == Some(character)
                    || read.ids(side + 0x10)?.iter().any(|id| armies.contains(id))
                {
                    result.push(id);
                    break;
                }
            }
            Ok(())
        })?;
        Ok(result)
    }

    fn report_ids(&mut self, character: u32) -> Result<Vec<u32>> {
        let mut result = Vec::new();
        self.visit_objects(COMBAT_RESULT, |read, id, object| {
            if read.ids(object + 0xb8)?.contains(&character) {
                result.push((read.u32(object + 0x34)?, id));
            }
            Ok(())
        })?;
        result.sort_unstable_by(|left, right| right.cmp(left));
        Ok(result.into_iter().map(|(_, id)| id).collect())
    }

    fn siege_ids(
        &mut self,
        character: u32,
        armies: &BTreeSet<u32>,
    ) -> Result<Vec<(u32, &'static str)>> {
        let mut rows = Vec::new();
        self.visit_objects(SIEGE, |read, id, object| {
            let province = read.pointer(object + 0x200)?;
            if province != 0 {
                rows.push((
                    id,
                    read.reference(object + 0x208)?,
                    read.reference(province + 0x738)?,
                ));
            }
            Ok(())
        })?;
        let mut result = Vec::new();
        for (id, army, title) in rows {
            if army.is_some_and(|id| armies.contains(&id)) {
                result.push((id, "attacker"));
            } else if let Some(title) = title {
                let mut lord = self.title_overview(title)?.holder_id;
                let mut seen = BTreeSet::new();
                while let Some(value) = lord {
                    if value == character {
                        result.push((id, "defender"));
                        break;
                    }
                    ensure!(seen.insert(value), "围城守方领主关系出现循环");
                    let primary = self.primary_title(value)?;
                    lord = self.character_liege(value, primary.as_ref())?;
                }
            }
        }
        Ok(result)
    }

    fn military_sample(
        &mut self,
        character: u32,
        seen_reports: &BTreeSet<u32>,
    ) -> Result<Military> {
        let owned = self.units_for(character)?;
        let armies = owned
            .iter()
            .filter(|unit| unit.owner_id == Some(character))
            .filter_map(|unit| unit.army_id)
            .collect();
        let units = owned
            .into_iter()
            .map(|unit| {
                (
                    unit.id,
                    Motion {
                        moving: !unit.path.is_empty(),
                        retreating: unit.retreating,
                        province: unit.province_id,
                        destination: unit.path.last().copied(),
                    },
                )
            })
            .collect();
        let combats = self
            .combat_ids(character, &armies)?
            .into_iter()
            .map(|id| Ok((id, self.combat(id)?)))
            .collect::<Result<_>>()?;
        let reports = self
            .report_ids(character)?
            .into_iter()
            .filter(|id| !seen_reports.contains(id))
            .map(|id| Ok((id, self.combat_result(id)?)))
            .collect::<Result<_>>()?;
        let sieges = self
            .siege_ids(character, &armies)?
            .into_iter()
            .map(|(id, side)| {
                let mut item = self.siege(id)?;
                item["side"] = side.into();
                Ok((id, item))
            })
            .collect::<Result<_>>()?;
        Ok(Military {
            units,
            combats,
            reports,
            sieges,
        })
    }

    pub(super) fn military_query(&mut self, kind: QueryKind, id: u32) -> Result<Value> {
        match kind {
            QueryKind::War => self.war(id),
            QueryKind::Army => self.army(id),
            QueryKind::Unit => Ok(serde_json::to_value(self.unit(id)?)?),
            QueryKind::UnitMovement => self.unit_movement(id),
            QueryKind::Combat => self.combat(id),
            QueryKind::CombatResult => self.combat_result(id),
            QueryKind::Siege => self.siege(id),
            QueryKind::Regiment => self.regiment(id),
            QueryKind::ArmyRegiment => Ok(serde_json::to_value(self.army_regiment(id)?)?),
            QueryKind::MercenaryCompany => self.mercenary_company(id),
            QueryKind::HoldingBuildings => self.holding_buildings(id),
            QueryKind::Adjacencies => Ok(json!({"id":id, "items":self.adjacencies(id)?})),
            _ => bail!("未知的实时查询类型"),
        }
    }

    pub(super) fn war(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(WAR, id)?;
        let cb = self.read.pointer(object + 0x100)?;
        Ok(json!({
            "id":id, "attacker":self.war_side(object + 0x20)?, "defender":self.war_side(object + 0x80)?,
            "start_date":date_text(self.read.u32(object + 0xe0)?), "casus_belli":self.read.key(cb)?,
            "target_title_ids":self.read.ids(object + 0x270)?, "claimant_id":self.read.reference(object + 0x290)?,
            "resolving":self.read.u8(object + 0x358)? != 0,
        }))
    }

    fn war_side(&mut self, object: u64) -> Result<Value> {
        let mut participants = Vec::new();
        for bytes in self.read.vector(object + 8)? {
            let member = decode_pointer(bytes)?;
            ensure!(member != 0, "战争参与者暂不可用");
            let id = self.read.reference(member + 8)?;
            participants.push(json!({
                "character":id.map(|id| self.character_name(id)).transpose()?,
                "joined":date_text(self.read.u32(member + 0xc)?), "casualties":self.read.u32(member + 0x20)?,
            }));
        }
        Ok(
            json!({"participants":participants, "ticking_war_score":self.read.fixed(object + 0x20)?,
            "controls_all_targets":self.read.u8(object + 0x58)? != 0}),
        )
    }

    pub(super) fn unit(&mut self, id: u32) -> Result<Unit> {
        let object = self.lookup(UNIT, id)?;
        let location = self.read.pointer(object + 0x20)?;
        let province_id = self.province_id(location)?;
        let path = self
            .read
            .vector(object + 0x38)?
            .into_iter()
            .map(|bytes| {
                let node = decode_pointer(bytes)?;
                ensure!(node != 0, "行军路径节点无效");
                self.read.u32(node)
            })
            .collect::<Result<Vec<_>>>()?;
        let arrival_date = if path.is_empty() {
            None
        } else {
            Some(date_text(self.read.u32(object + 0x180)?))
        };
        Ok(Unit {
            id,
            kind: match self.read.u32(object + 0x18)? {
                0 => "army",
                1 => "fleet",
                _ => "unknown",
            },
            owner_id: self.read.reference(object + 0x174)?,
            province_id,
            army_id: self.read.reference(object + 0x178)?,
            path,
            movement_progress: self.read.fixed(object + 0x168)?,
            retreating: self.read.u32(object + 0x170)? != 0,
            arrival_date,
        })
    }

    fn unit_movement(&mut self, id: u32) -> Result<Value> {
        let unit = self.unit(id)?;
        let object = self.lookup(UNIT, id)?;
        let speed = self.read.fixed(object + 0x190)?;
        let threshold = self.read.fixed(self.base + 0x5c7eb08)?;
        let cost = if let (Some(start), Some(next)) = (unit.province_id, unit.path.first()) {
            self.adjacencies(start)?
                .into_iter()
                .find(|edge| edge.province_id == *next)
                .map(|edge| edge.movement_cost)
        } else {
            None
        };
        let mut result = serde_json::to_value(&unit)?;
        result["cached_movement_speed"] = json!((speed > 0.0).then_some(speed));
        result["movement"] = json!({
            "next_province_id":unit.path.first(), "destination_province_id":unit.path.last(),
            "leg_movement_cost":cost, "movement_lock_threshold":(0.0..=1.0).contains(&threshold).then_some(threshold),
            "leg_progress_fraction":null, "movement_locked":null, "estimated_next_arrival_days":null,
            "derived_fields_status":if unit.path.is_empty() { "stationary" } else { "unavailable" },
        });
        if !unit.path.is_empty() {
            match self.shadow().and_then(|mut engine| engine.movement(object)) {
                Ok(Value::Object(native)) => {
                    let movement = result["movement"].as_object_mut().expect("movement object");
                    movement.extend(native);
                    movement.insert(
                        "derived_fields_status".into(),
                        "native_shadow_unicorn".into(),
                    );
                }
                Ok(_) => unreachable!("native movement result is an object"),
                Err(error) => result["movement"]["native_error"] = format!("{error:#}").into(),
            }
        }
        Ok(result)
    }

    pub(super) fn province_id(&mut self, object: u64) -> Result<Option<u32>> {
        if object == 0 {
            return Ok(None);
        }
        ensure!(
            self.read.pointer(object)? == self.base + PROVINCE_VTABLE,
            "部队所在省份暂不可用"
        );
        let id = self.read.u32(object + 0x10)?;
        ensure!(
            self.province_object(id)? == object,
            "部队所在省份在读取期间变化"
        );
        Ok(Some(id))
    }

    fn regiment(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(REGIMENT, id)?;
        let definition = self.read.pointer(object + 0x118)?;
        let mut strength_chunks = Vec::new();
        for index in 0..7 {
            let entry = object + 0x18 + index * 0x24;
            strength_chunks.push(
                json!({"maximum":self.read.u32(entry)?, "current":self.read.u32(entry + 4)?}),
            );
        }
        let mut result = json!({"id":id, "type":self.read.key(definition)?, "current":null, "maximum":null,
            "strength_status":"native_result_not_located", "strength_chunks":strength_chunks,
            "owner_id":self.read.reference(object + 0x12c)?, "owning_title_id":self.read.reference(object + 0x130)?,
            "controlling_title_id":self.read.reference(object + 0x134)?, "war_id":self.read.reference(object + 0x13c)?,
        });
        if let Ok((current, maximum)) = self.shadow().and_then(|mut engine| engine.regiment(object))
        {
            result["current"] = current.into();
            result["maximum"] = maximum.into();
            result["strength_status"] = "native_shadow_unicorn".into();
        }
        Ok(result)
    }

    fn army_regiment(&mut self, id: u32) -> Result<ArmyRegiment> {
        let object = self.lookup(ARMY_REGIMENT, id)?;
        let definition = self.read.pointer(object + 0x18)?;
        Ok(ArmyRegiment {
            id,
            kind: self.read.key(definition)?,
            current: self.read.u32(object + 0x38)?,
            maximum: self.read.u32(object + 0x3c)?,
            army_id: self.read.reference(object + 0x140)?,
            vassal_id: self.read.reference(object + 0x144)?,
            knight_id: self.read.reference(object + 0x148)?,
        })
    }

    pub(super) fn army(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(ARMY, id)?;
        let unit = self
            .read
            .reference(object + 0x124)?
            .map(|id| self.unit(id))
            .transpose()?;
        let regiments = self
            .read
            .ids(object + 0x38)?
            .into_iter()
            .map(|id| self.army_regiment(id))
            .collect::<Result<Vec<_>>>()?;
        let current: u64 = regiments
            .iter()
            .map(|regiment| u64::from(regiment.current))
            .sum();
        let maximum: u64 = regiments
            .iter()
            .map(|regiment| u64::from(regiment.maximum))
            .sum();
        Ok(
            json!({"id":id, "commander_id":self.read.reference(object + 0x120)?, "unit":unit,
                "current":current, "maximum":maximum, "strength_status":"native_army_regiment_sum", "regiments":regiments,
                "supply":self.read.fixed(object + 0x180)?, "raiding":self.read.u8(object + 0x1d4)? != 0,
                "raid_gold":self.read.fixed(object + 0x1e0)?,
            }),
        )
    }

    pub(super) fn army_owner(&mut self, id: u32) -> Result<Option<u32>> {
        let object = self.lookup(ARMY, id)?;
        let Some(unit) = self.read.reference(object + 0x124)? else {
            return Ok(None);
        };
        let unit = self.lookup(UNIT, unit)?;
        self.read.reference(unit + 0x174)
    }

    pub(super) fn mercenary_company(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(MERCENARY, id)?;
        let county = self
            .read
            .reference(object + 0x24)?
            .map(|id| self.title_overview(id))
            .transpose()?;
        let realm = self.realm(county.as_ref().and_then(|county| county.holder_id))?;
        let regiments = self
            .read
            .ids(object + 0x30)?
            .into_iter()
            .map(|id| self.regiment(id))
            .collect::<Result<Vec<_>>>()?;
        let hired_by_id = self.read.reference(object + 0x48)?;
        let hired_until = hired_by_id
            .map(|_| self.read.u32(object + 0x4c).map(date_text))
            .transpose()?;
        let mut result = json!({"id":id, "culture_id":self.read.reference(object + 0x20)?, "home_county":county, "home_realm":realm,
            "title_id":self.read.reference(object + 0x2c)?, "hired_by_id":hired_by_id, "hired_until":hired_until,
            "current":null, "maximum":null, "strength_status":"native_result_not_located", "regiments":regiments,
        });
        if let Ok((current, maximum)) = self
            .shadow()
            .and_then(|mut engine| engine.mercenary(object))
        {
            result["current"] = current.into();
            result["maximum"] = maximum.into();
            result["strength_status"] = "native_shadow_unicorn".into();
        }
        Ok(result)
    }

    pub(super) fn combat(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(COMBAT, id)?;
        let location = self.read.pointer(object + 0x6b8)?;
        let phase = match self.read.u32(object + 0x6b0)? {
            0 => "maneuver",
            1 => "main",
            2 => "pursuit",
            _ => "unknown",
        };
        Ok(
            json!({"id":id, "province_id":self.province_id(location)?, "phase":phase,
                "phase_days":self.read.u32(object + 0x6b4)?, "attacker_advantage":self.read.fixed(object + 0x6c8)?,
                "combat_width":self.read.u32(object + 0x6c4)?, "attacker":self.combat_side(object + 0x20)?,
                "defender":self.combat_side(object + 0x368)?,
            }),
        )
    }

    fn combat_side(&mut self, object: u64) -> Result<Value> {
        ensure!(
            self.read.pointer(object)? == self.base + 0x4756988,
            "交战方对象暂不可用"
        );
        let armies = self.read.ids(object + 0x10)?;
        let mut owners = BTreeSet::new();
        for &army in &armies {
            owners.extend(self.army_owner(army)?);
        }
        Ok(
            json!({"army_ids":armies, "owner_ids":owners, "leader_id":self.read.reference(object + 0x70)?,
                "commander_id":self.read.reference(object + 0x74)?, "fighting_soldiers":self.read.fixed(object + 0x98)?,
                "initial_soldiers":self.read.fixed(object + 0xa8)?, "initial_levies":self.read.fixed(object + 0xb0)?,
                "retreat_disabled":self.read.u8(object + 0xc0)? != 0, "early_retreat_allowed":self.read.u8(object + 0xc1)? != 0,
            }),
        )
    }

    pub(super) fn combat_result(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(COMBAT_RESULT, id)?;
        let province = self.read.pointer(object + 0x18)?;
        let winning_side = match self.read.u32(object + 0x20)? {
            0 => Some("attacker"),
            1 => Some("defender"),
            _ => None,
        };
        Ok(
            json!({"id":id, "province_id":self.province_id(province)?, "winning_side":winning_side,
                "start_date":date_text(self.read.u32(object + 0x2c)?), "end_date":date_text(self.read.u32(object + 0x34)?),
                "wiped":self.read.u8(object + 0x28)? != 0, "attacker":self.combat_result_side(object + 0xe8)?,
                "defender":self.combat_result_side(object + 0x138)?, "interested_character_ids":self.read.ids(object + 0xb8)?,
            }),
        )
    }

    fn combat_result_side(&mut self, object: u64) -> Result<Value> {
        ensure!(
            self.read.pointer(object)? == self.base + 0x4757718,
            "战报交战方对象暂不可用"
        );
        let mut result = json!({"main_participant_id":self.read.reference(object + 0xc)?, "commander_id":self.read.reference(object + 8)?,
            "initial_soldiers":self.read.fixed(object + 0x10)?, "surviving_soldiers":self.read.fixed(object + 0x18)?,
            "casualties":null, "casualties_status":"native_result_not_located", "participant_ids":self.read.ids(object + 0x20)?,
        });
        if let Ok(casualties) = self
            .shadow()
            .and_then(|mut engine| engine.casualties(object))
        {
            result["casualties"] = casualties.into();
            result["casualties_status"] = "native_shadow_unicorn".into();
        }
        Ok(result)
    }

    pub(super) fn siege(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(SIEGE, id)?;
        let province = self.read.pointer(object + 0x200)?;
        let Some(province_id) = self.province_id(province)? else {
            bail!("围城所在省份暂不可用")
        };
        let army = self.read.reference(object + 0x208)?;
        let attacker = army
            .map(|army| self.army_owner(army))
            .transpose()?
            .flatten();
        let title = self
            .read
            .reference(province + 0x738)?
            .map(|title| self.title_overview(title))
            .transpose()?;
        let defender = title.as_ref().and_then(|title| title.holder_id);
        let mut current = 0;
        let mut capacity = None;
        if let Some(id) = self.read.reference(province + 0x70c)? {
            let regiment = self.lookup(REGIMENT, id)?;
            capacity = Some(self.read.u32(regiment + 0x128)?);
            current = self.read.i32(regiment + 0x1c)?;
            if current == 0 && self.read.u32(regiment + 0x30)? == 3 {
                current = self.read.i32(regiment + 0x18)?;
            }
        }
        let mut effects = Map::new();
        for (index, key) in ["breach", "starvation", "disease", "desertion", "stalemate"]
            .into_iter()
            .enumerate()
        {
            effects.insert(
                key.into(),
                self.read.u32(object + 0x3d8 + index as u64 * 4)?.into(),
            );
        }
        let commander = match army {
            Some(army) => {
                let army = self.lookup(ARMY, army)?;
                self.read.u32(army + 0x120)?
            }
            None => u32::MAX,
        };
        let progress = self.read.fixed(object + 0x3d0)?;
        let mut result = json!({"id":id, "province_id":province_id, "owner_army_id":army, "title":title,
            "attacker":attacker.map(|id| self.character_name(id)).transpose()?, "defender":defender.map(|id| self.character_name(id)).transpose()?,
            "defending_realm":self.realm(defender)?, "progress_points":progress,
            "progress":{"status":"unavailable", "percent":null, "points_required":null,
                    "garrison_current":current, "garrison_base":self.read.i32(province + 0x730)?, "garrison_capacity":capacity},
            "phase_days_remaining":self.read.i32(object + 0x43c)?, "start_date":date_text(self.read.u32(object + 0x444)?),
            "effects":effects, "projection":null,
        });
        if let Ok(mut engine) = self.shadow()
            && let Ok((points, percent)) = engine.siege(object)
        {
            result["progress"]["points_required"] = points.into();
            result["progress"]["percent"] = percent.into();
            result["progress"]["status"] = "native_shadow_unicorn".into();
            if let Ok(projection) =
                engine.siege_projection(object, commander, points, progress, self.clock.date)
            {
                result["projection"] = projection;
            }
        }
        Ok(result)
    }

    pub(super) fn adjacencies(&mut self, id: u32) -> Result<Vec<Edge>> {
        let province = self.province_object(id)?;
        let definition = self.read.pointer(province + 8)?;
        ensure!(
            definition != 0 && self.read.u32(definition + 0x24)? == id,
            "省份地图定义暂不可用"
        );
        let count = self.read.u32(definition + 0x5c)?;
        let capacity = self.read.u32(definition + 0x58)?;
        ensure!(count <= capacity && count <= 1024, "相邻省份列表无效");
        self.read
            .vector::<48>(definition + 0x50)?
            .into_iter()
            .map(|record| {
                let kind = u32::from_le_bytes(array(&record[..4]));
                let crossing = ["normal", "strait", "river", "large_river", "impassable"]
                    .get(kind as usize)
                    .copied();
                let Some(crossing) = crossing else {
                    bail!("省份之间的通行类型无效")
                };
                let through = u32::from_le_bytes(array(&record[8..12]));
                Ok(Edge {
                    province_id: u32::from_le_bytes(array(&record[4..8])),
                    crossing,
                    through_province_id: (through != 0).then_some(through),
                    movement_cost: i64::from_le_bytes(array(&record[16..24])) as f64 / 100_000.0,
                })
            })
            .collect()
    }

    fn holding_buildings(&mut self, id: u32) -> Result<Value> {
        let province = self.province_object(id)?;
        let holding = province + 0x620;
        let rows = self.read.vector::<16>(holding + 0x10)?;
        let mut slots = Vec::with_capacity(rows.len());
        for (index, row) in rows.iter().enumerate() {
            let definition = decode_pointer(array(&row[..8]))?;
            slots.push(json!({"index":index, "building_key":self.read.key(definition)?, "disabled":row[8] != 0, "ruined":row[9] != 0}));
        }
        let record = fresh::<0x78>(self.read.memory, holding + 0x68)?;
        let building = decode_pointer(array(&record[..8]))?;
        let construction = if building != 0 {
            let mut paid = Map::new();
            for (name, offset) in [
                ("gold", 0x20),
                ("prestige", 0x28),
                ("piety", 0x30),
                ("treasury", 0x50),
            ] {
                paid.insert(
                    name.into(),
                    json!(
                        i64::from_le_bytes(array(&record[offset..offset + 8])) as f64 / 100_000.0
                    ),
                );
            }
            let timing = self
                .shadow()
                .and_then(|mut engine| {
                    engine.construction(holding + 0x68, province, self.clock.date)
                })
                .ok();
            json!({"building_key":self.read.key(building)?, "slot_index":i32::from_le_bytes(array(&record[8..12])),
                "start_date":date_text(u32::from_le_bytes(array(&record[12..16]))),
                "character_id":reference(u32::from_le_bytes(array(&record[0x70..0x74]))), "paid_cost":paid,
                "paid_cost_basis":"native_construction_record", "base_days_remaining":i64::from_le_bytes(array(&record[0x18..0x20])) as f64 / 100_000.0,
                "timing":timing,
            })
        } else {
            Value::Null
        };
        ensure!(
            fresh::<0x78>(self.read.memory, holding + 0x68)? == record,
            "施工记录在读取期间变化"
        );
        let holding_type = self.read.pointer(holding)?;
        let special = self.read.pointer(holding + 0x28)?;
        let duchy = self.read.pointer(holding + 0x38)?;
        let great = self.read.pointer(holding + 0x48)?;
        Ok(
            json!({"province_id":id, "holding_type":self.read.key(holding_type)?, "slot_count":slots.len(), "slots":slots,
                "special_building_key":self.read.key(special)?, "duchy_building_key":self.read.key(duchy)?,
                "great_building_key":self.read.key(great)?, "construction":construction,
            }),
        )
    }
}

impl Catalogs {
    pub(crate) fn military(
        &mut self,
        memory: &dyn Memory,
        base: u64,
        character: u32,
        generation: u64,
        seen_reports: &BTreeSet<u32>,
    ) -> Result<Military> {
        let (_, data) = self.consistent(memory, base, true, |view| {
            view.military_sample(character, seen_reports)
        })?;
        ensure!(self.generation == generation, "战役在读取军事状态期间变化");
        Ok(data)
    }
}
