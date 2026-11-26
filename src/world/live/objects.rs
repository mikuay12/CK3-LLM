use std::collections::{HashMap, HashSet};

use anyhow::{Context, Result, anyhow, ensure};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::session::CK3_BUILD;
use crate::world::Query;

#[cfg(test)]
mod tests;

// CK3 1.20.1.2 x64 layouts, verified against the executable hash before use.
const WORLD: u64 = 0x5c7dbe8;
const SYNCHRONIZATION: u64 = 0x5c7f410;
const PLAYER_MANAGER: u64 = 0x5ccd808;
const TRAIT_DATABASE: u64 = 0x5c7c4b0;
const PROVINCE_MANAGER_VTABLE: u64 = 0x4787058;
const PROVINCE_VTABLE: u64 = 0x4742e90;
const COUNTY_DATA_VTABLE: u64 = 0x4746bc0;

pub(super) const REQUIRED_IMAGE_BYTES: u64 = 0x5d33310 + 8;
pub(super) const MAX_READ_BYTES: usize = 1024 * 1024;
const MAX_SLOTS: u32 = 0x0100_0000;
const SLOT_BYTES: u64 = 16;
const PAGE_BYTES: usize = 256;
const MAX_CACHED_PAGES: usize = 8192;
const NO_REFERENCE: u32 = u32::MAX;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Layout {
    object: &'static str,
    global: u64,
    id_offset: u64,
    vtable: u64,
}

const CHARACTER: Layout = Layout {
    object: "人物",
    global: 0x5c7c4f0,
    id_offset: 0x18,
    vtable: 0x47748a0,
};
const TITLE: Layout = Layout {
    object: "头衔",
    global: 0x5d32b08,
    id_offset: 0x10,
    vtable: 0x4723198,
};
const CULTURE: Layout = Layout {
    object: "文化",
    global: 0x5d33300,
    id_offset: 0x10,
    vtable: 0x474c960,
};
const RITE: Layout = Layout {
    object: "仪派",
    global: 0x5d33308,
    id_offset: 8,
    vtable: 0x4748f20,
};
const FAITH: Layout = Layout {
    object: "信仰",
    global: 0x5d33310,
    id_offset: 8,
    vtable: 0x4740c00,
};

pub(super) trait Memory {
    fn read(&self, address: u64, buffer: &mut [u8]) -> Result<()>;
}

struct ReadView<'a> {
    memory: &'a dyn Memory,
    pages: HashMap<u64, [u8; PAGE_BYTES]>,
}

impl<'a> ReadView<'a> {
    fn new(memory: &'a dyn Memory) -> Self {
        Self {
            memory,
            pages: HashMap::new(),
        }
    }

    fn bytes(&mut self, mut address: u64, mut out: &mut [u8]) -> Result<()> {
        while !out.is_empty() {
            let page = address & !(PAGE_BYTES as u64 - 1);
            let offset = (address - page) as usize;
            if self.pages.len() >= MAX_CACHED_PAGES {
                self.pages.clear();
            }
            let bytes = match self.pages.entry(page) {
                std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                std::collections::hash_map::Entry::Vacant(entry) => {
                    let mut bytes = [0; PAGE_BYTES];
                    self.memory.read(page, &mut bytes)?;
                    entry.insert(bytes)
                }
            };
            let count = out.len().min(PAGE_BYTES - offset);
            out[..count].copy_from_slice(&bytes[offset..offset + count]);
            address += count as u64;
            out = &mut out[count..];
        }
        Ok(())
    }

    fn array<const N: usize>(&mut self, address: u64) -> Result<[u8; N]> {
        let mut bytes = [0; N];
        self.bytes(address, &mut bytes)?;
        Ok(bytes)
    }

    fn pointer(&mut self, address: u64) -> Result<u64> {
        decode_pointer(self.array(address)?)
    }

    fn u32(&mut self, address: u64) -> Result<u32> {
        Ok(u32::from_le_bytes(self.array(address)?))
    }

    fn i32(&mut self, address: u64) -> Result<i32> {
        Ok(i32::from_le_bytes(self.array(address)?))
    }

    fn i16(&mut self, address: u64) -> Result<i16> {
        Ok(i16::from_le_bytes(self.array(address)?))
    }

    fn u8(&mut self, address: u64) -> Result<u8> {
        Ok(self.array::<1>(address)?[0])
    }

    fn reference(&mut self, address: u64) -> Result<Option<u32>> {
        Ok(reference(self.u32(address)?))
    }

    fn fixed(&mut self, address: u64) -> Result<f64> {
        Ok(i64::from_le_bytes(self.array(address)?) as f64 / 100_000.0)
    }

    fn string(&mut self, address: u64) -> Result<String> {
        let length = u64::from_le_bytes(self.array(address + 0x10)?);
        let capacity = u64::from_le_bytes(self.array(address + 0x18)?);
        ensure!(length <= capacity && length <= 65_536, "CK3 字符串长度无效");
        if length == 0 {
            return Ok(String::new());
        }
        let data = if capacity < 16 {
            address
        } else {
            self.pointer(address)?
        };
        let mut bytes = vec![0; length as usize];
        self.bytes(data, &mut bytes)?;
        String::from_utf8(bytes).context("CK3 字符串编码无效")
    }

    fn vector<const WIDTH: usize>(&mut self, address: u64) -> Result<Vec<[u8; WIDTH]>> {
        let data = self.pointer(address)?;
        let capacity = self.u32(address + 8)?;
        let count = self.u32(address + 12)?;
        ensure!(
            count <= capacity && count as u64 * WIDTH as u64 <= MAX_READ_BYTES as u64,
            "CK3 对象列表长度无效"
        );
        if count == 0 {
            return Ok(Vec::new());
        }
        let mut bytes = vec![0; count as usize * WIDTH];
        self.memory.read(data, &mut bytes)?;
        Ok(bytes.as_chunks::<WIDTH>().0.to_vec())
    }

    fn ids(&mut self, address: u64) -> Result<Vec<u32>> {
        Ok(self
            .vector(address)?
            .into_iter()
            .map(u32::from_le_bytes)
            .collect())
    }

    fn key(&mut self, object: u64) -> Result<Option<String>> {
        if object == 0 || self.u32(object + 0x38)? != 0x4744624f {
            return Ok(None);
        }
        Ok(Some(self.string(object + 0x18)?))
    }

    fn keys(&mut self, address: u64) -> Result<Vec<Option<String>>> {
        self.vector(address)?
            .into_iter()
            .map(|bytes| self.key(decode_pointer(bytes)?))
            .collect()
    }
}

fn array<const N: usize>(bytes: &[u8]) -> [u8; N] {
    bytes.try_into().expect("fixed-width record")
}

fn decode_pointer(bytes: [u8; 8]) -> Result<u64> {
    let value = u64::from_le_bytes(bytes);
    ensure!(value < i64::MAX as u64, "CK3 对象地址无效");
    Ok(value)
}

fn fresh<const N: usize>(memory: &dyn Memory, address: u64) -> Result<[u8; N]> {
    let mut bytes = [0; N];
    memory.read(address, &mut bytes)?;
    Ok(bytes)
}

fn pointer(memory: &dyn Memory, address: u64) -> Result<u64> {
    decode_pointer(fresh(memory, address)?)
}

fn reference(id: u32) -> Option<u32> {
    (id != NO_REFERENCE).then_some(id)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Clock {
    world: u64,
    data: u64,
    synchronization: u64,
    date: u32,
    tick: u32,
}

impl Clock {
    fn read(memory: &dyn Memory, base: u64) -> Result<Self> {
        let world = pointer(memory, base + WORLD)?;
        let synchronization = pointer(memory, base + SYNCHRONIZATION)?;
        ensure!(
            world != 0 && synchronization != 0,
            "请进入 CK3 战役后再查询"
        );
        let data = pointer(memory, world + 0xa0)?;
        ensure!(data != 0, "请进入 CK3 战役后再查询");
        Ok(Self {
            world,
            data,
            synchronization,
            date: u32::from_le_bytes(fresh(memory, world + 8)?),
            tick: u32::from_le_bytes(fresh(memory, synchronization + 0x68)?),
        })
    }

    fn follows(self, earlier: Self) -> bool {
        self.world == earlier.world
            && self.data == earlier.data
            && self.synchronization == earlier.synchronization
            && self.tick >= earlier.tick
            && self.date >= earlier.date
    }
}

fn date_text(raw: u32) -> String {
    let days = raw / 24;
    let year = (days / 365) as i32 - 5000;
    let mut day = days % 365;
    let mut month = 1;
    for length in [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30] {
        if day < length {
            break;
        }
        day -= length;
        month += 1;
    }
    let date = format!("{year}.{month}.{}", day + 1);
    let hour = raw % 24;
    if hour == 0 {
        date
    } else {
        format!("{date} {hour:02}:00")
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Table {
    database: u64,
    slots: u64,
    length: u32,
}

impl Table {
    fn read(memory: &dyn Memory, base: u64, layout: Layout) -> Result<Self> {
        let database = pointer(memory, base + layout.global)?;
        ensure!(database != 0, "CK3 {}数据库暂不可用", layout.object);
        let header = fresh::<16>(memory, database + 0x20)?;
        let slots = decode_pointer(array(&header[..8]))?;
        let length = u32::from_le_bytes(array(&header[12..]));
        ensure!(
            length <= MAX_SLOTS && (length == 0 || slots != 0),
            "CK3 {}数据库长度无效",
            layout.object
        );
        Ok(Self {
            database,
            slots,
            length,
        })
    }

    fn slot(self, id: u32) -> Option<u64> {
        let index = id & 0x00ff_ffff;
        (id != NO_REFERENCE && index < self.length)
            .then(|| self.slots + u64::from(index) * SLOT_BYTES + 8)
    }

    fn revision(self, memory: &dyn Memory) -> Result<u32> {
        Ok(u32::from_le_bytes(fresh(memory, self.database + 0x3c)?))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Provinces {
    array: u64,
    count: u32,
}

impl Provinces {
    fn read(memory: &dyn Memory, base: u64, world_data: u64) -> Result<Self> {
        let manager = world_data + 0x118;
        ensure!(
            pointer(memory, manager)? == base + PROVINCE_MANAGER_VTABLE,
            "省份管理器暂不可用"
        );
        let header = fresh::<16>(memory, manager + 0x28)?;
        let array_ptr = decode_pointer(array(&header[..8]))?;
        let count = u32::from_le_bytes(array(&header[12..]));
        ensure!(
            count <= MAX_SLOTS && (count == 0 || array_ptr != 0),
            "省份列表长度无效"
        );
        Ok(Self {
            array: array_ptr,
            count,
        })
    }

    fn slot(self, id: u32) -> Result<u64> {
        ensure!(id > 0 && id < self.count, "找不到省份 {id}");
        Ok(self.array + u64::from(id) * 8)
    }
}

struct TitleIndex {
    table: Table,
    revision: u32,
    keys: HashMap<String, u32>,
}

struct BaronyIndex {
    provinces: Provinces,
    titles: HashMap<u32, u32>,
}

#[derive(Default)]
pub(super) struct Catalogs {
    last_clock: Option<Clock>,
    titles: Option<TitleIndex>,
    baronies: Option<BaronyIndex>,
}

impl Catalogs {
    pub fn query(&mut self, memory: &dyn Memory, base: u64, request: Query<'_>) -> Result<Value> {
        let mut last_error = None;
        for _ in 0..3 {
            let before = Clock::read(memory, base)?;
            if self
                .last_clock
                .is_some_and(|earlier| !before.follows(earlier))
            {
                self.titles = None;
                self.baronies = None;
            }
            self.last_clock = Some(before);
            let mut view = WorldView::new(ReadView::new(memory), base, before, self);
            let result = view.execute(request).and_then(|data| {
                view.validate_references()?;
                Ok(data)
            });
            let after = Clock::read(memory, base)?;
            if before != after {
                last_error = Some(anyhow!("游戏在读取期间推进，请重试查询"));
                continue;
            }
            match result {
                Ok(data) => {
                    return Ok(json!({
                        "source": "live", "date": date_text(before.date),
                        "date_raw": before.date, "tick": before.tick, "data": data,
                    }));
                }
                Err(error) => last_error = Some(error),
            }
        }
        Err(last_error.expect("at least one read attempt"))
    }
}

#[derive(Serialize)]
struct Player {
    name: String,
    character_id: Option<u32>,
    machineid: u32,
    reported_turn: u32,
    flags: u8,
}

#[derive(Serialize)]
struct TitleOverview {
    id: u32,
    key: Option<String>,
    tier: &'static str,
    holder_id: Option<u32>,
    de_facto_liege_id: Option<u32>,
    de_jure_liege_id: Option<u32>,
}

#[derive(Serialize)]
struct TitleDetail {
    #[serde(flatten)]
    overview: TitleOverview,
    holder: Option<Value>,
    controller: Option<Value>,
    capital_title_id: Option<u32>,
    de_jure_vassal_ids: Vec<u32>,
    heir_ids: Vec<u32>,
    laws: Vec<Option<String>>,
    lessee_id: Option<u32>,
    province_id: Option<u32>,
}

struct WorldView<'a> {
    read: ReadView<'a>,
    base: u64,
    clock: Clock,
    catalogs: &'a mut Catalogs,
    tables: HashMap<Layout, Table>,
    references: HashSet<(Layout, u32, u64)>,
    provinces: Option<Provinces>,
    province_references: HashSet<(u32, u64)>,
    players: Option<Vec<Player>>,
}

impl<'a> WorldView<'a> {
    fn new(read: ReadView<'a>, base: u64, clock: Clock, catalogs: &'a mut Catalogs) -> Self {
        Self {
            read,
            base,
            clock,
            catalogs,
            tables: HashMap::new(),
            references: HashSet::new(),
            provinces: None,
            province_references: HashSet::new(),
            players: None,
        }
    }

    fn execute(&mut self, request: Query<'_>) -> Result<Value> {
        match request {
            Query::Summary => Ok(json!({"build": CK3_BUILD, "players": self.players()?})),
            Query::Character(id) => self.character(id),
            Query::Title(key) => {
                let title = if let Ok(id) = key.parse() {
                    self.title(id, None)?
                } else {
                    let id = self.title_id(key)?;
                    let title = self.title(id, None)?;
                    ensure!(
                        title.overview.key.as_deref() == Some(key),
                        "头衔键已变化，请重新查询"
                    );
                    title
                };
                Ok(serde_json::to_value(title)?)
            }
            Query::Province(id) => self.province(id),
        }
    }

    fn table(&mut self, layout: Layout) -> Result<Table> {
        if let Some(table) = self.tables.get(&layout) {
            return Ok(*table);
        }
        let table = Table::read(self.read.memory, self.base, layout)?;
        self.tables.insert(layout, table);
        Ok(table)
    }

    fn lookup(&mut self, layout: Layout, id: u32) -> Result<u64> {
        let table = self.table(layout)?;
        let slot = table
            .slot(id)
            .with_context(|| format!("找不到{} {id}", layout.object))?;
        let object = self.read.pointer(slot)?;
        ensure!(
            object != 0
                && self.read.pointer(object)? == self.base + layout.vtable
                && self.read.u32(object + layout.id_offset)? == id,
            "{} {id} 不存在或对象编号已失效",
            layout.object
        );
        self.references.insert((layout, id, object));
        Ok(object)
    }

    fn validate_references(&self) -> Result<()> {
        let memory = self.read.memory;
        for (&layout, &expected) in &self.tables {
            ensure!(
                Table::read(memory, self.base, layout)? == expected,
                "CK3 {}数据库在读取期间变化",
                layout.object
            );
        }
        for &(layout, id, object) in &self.references {
            let slot = self.tables[&layout].slot(id).expect("recorded slot");
            ensure!(
                pointer(memory, slot)? == object
                    && pointer(memory, object)? == self.base + layout.vtable
                    && u32::from_le_bytes(fresh(memory, object + layout.id_offset)?) == id,
                "CK3 {}对象在读取期间变化",
                layout.object
            );
        }
        if let Some(provinces) = self.provinces {
            ensure!(
                Provinces::read(memory, self.base, self.clock.data)? == provinces,
                "省份列表在读取期间变化"
            );
            for &(id, object) in &self.province_references {
                ensure!(
                    pointer(memory, provinces.slot(id)?)? == object
                        && pointer(memory, object)? == self.base + PROVINCE_VTABLE
                        && u32::from_le_bytes(fresh(memory, object + 0x10)?) == id,
                    "CK3 省份对象在读取期间变化"
                );
            }
        }
        Ok(())
    }

    fn players(&mut self) -> Result<&[Player]> {
        if self.players.is_none() {
            let manager = self.read.pointer(self.base + PLAYER_MANAGER)?;
            ensure!(manager != 0, "CK3 玩家信息暂不可用");
            let array = self.read.pointer(manager + 0x1d8)?;
            let count = self.read.u32(manager + 0x1e4)?;
            ensure!(
                count <= 64 && (count == 0 || array != 0),
                "CK3 玩家列表长度无效"
            );
            let mut players = Vec::with_capacity(count as usize);
            for index in 0..u64::from(count) {
                let player = self.read.pointer(array + index * 8)?;
                ensure!(player != 0, "CK3 玩家对象暂不可用");
                players.push(Player {
                    name: self.read.string(player + 8)?,
                    character_id: self.read.reference(player + 0x58)?,
                    machineid: self.read.u32(player + 0x70)?,
                    reported_turn: self.read.u32(player + 0x74)?,
                    flags: self.read.u8(player + 0x48)?,
                });
            }
            self.players = Some(players);
        }
        Ok(self.players.as_deref().expect("players initialized"))
    }

    fn controller(&mut self, id: u32) -> Result<Value> {
        let player = self
            .players()?
            .iter()
            .find(|player| player.character_id == Some(id));
        Ok(match player {
            Some(player) => json!({"type": "player", "name": player.name}),
            None => json!({"type": "game_ai"}),
        })
    }

    fn character_name(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(CHARACTER, id)?;
        Ok(json!({"id": id, "name": self.read.string(object + 0x20)?}))
    }

    fn culture(&mut self, id: u32) -> Result<Option<Value>> {
        if id == NO_REFERENCE {
            return Ok(None);
        }
        let object = self.lookup(CULTURE, id)?;
        let definition = self.read.pointer(object + 0x28)?;
        Ok(Some(
            json!({"id": id, "name": self.read.string(object + 0x30)?,
            "key": self.read.key(definition)?}),
        ))
    }

    fn faith(&mut self, rite_id: u32) -> Result<Option<Value>> {
        if rite_id == NO_REFERENCE {
            return Ok(None);
        }
        let rite = self.lookup(RITE, rite_id)?;
        let definition = self.read.pointer(rite + 0x4b0)?;
        let key = match self.read.key(definition)? {
            Some(key) if !key.is_empty() => key,
            _ => self.read.string(rite + 0x518)?,
        };
        let faith_id = self.read.reference(rite + 0x4b8)?;
        let rite = json!({
            "id": rite_id, "key": key, "name": self.read.string(rite + 0x540)?,
            "faith_id": faith_id, "religious_head_id": self.read.reference(rite + 0x4c0)?,
        });
        let Some(id) = faith_id else {
            return Ok(Some(
                json!({"id": null, "key": null, "name": null, "rite": rite}),
            ));
        };
        let faith = self.lookup(FAITH, id)?;
        Ok(Some(
            json!({"id": id, "key": self.read.string(faith + 0xe0)?,
            "name": self.read.string(faith + 0x108)?, "rite": rite}),
        ))
    }

    fn skills(&mut self, address: u64) -> Result<Value> {
        let mut skills = Map::new();
        for (index, key) in [
            "diplomacy",
            "martial",
            "stewardship",
            "intrigue",
            "learning",
            "prowess",
        ]
        .into_iter()
        .enumerate()
        {
            skills.insert(
                key.into(),
                self.read.i32(address + index as u64 * 4)?.into(),
            );
        }
        Ok(Value::Object(skills))
    }

    fn traits(&mut self, object: u64) -> Result<Vec<Value>> {
        let ids = self.read.ids(object + 0xf8)?;
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let database = self.read.pointer(self.base + TRAIT_DATABASE)?;
        ensure!(database != 0, "CK3 人物特质暂不可用");
        let count = self.read.u32(database + 0x5c)?;
        let entries = self.read.pointer(database + 0x50)?;
        ids.into_iter()
            .map(|id| {
                ensure!(id < count && entries != 0, "CK3 特质 ID 超出范围");
                let definition = self.read.pointer(entries + u64::from(id) * 8)?;
                Ok(json!({"id": id, "key": self.read.key(definition)?}))
            })
            .collect()
    }

    fn character(&mut self, id: u32) -> Result<Value> {
        let object = self.lookup(CHARACTER, id)?;
        let alive = self.read.pointer(object + 0x1b0)?;
        let dead = if alive == 0 {
            self.read.pointer(object + 0x1d0)?
        } else {
            0
        };
        let landed = self.read.pointer(object + 0x1c0)?;
        let family = self.read.pointer(object + 0x1a8)?;
        let court = self.read.pointer(object + 0x1b8)?;
        let culture = self.read.u32(object + 0xb0)?;
        let rite = self.read.u32(object + 0xb4)?;
        let identity = json!({
            "name": self.read.string(object + 0x20)?, "regnal_name": self.read.string(object + 0x40)?,
            "birth": date_text(self.read.u32(object + 0x60)?),
            "death_date": if dead != 0 { Some(date_text(self.read.u32(dead + 4)?)) } else { None },
            "age": self.read.i16(object + 0x68)?, "female": self.read.u8(object + 0x1a1)? != 0,
            "alive": alive != 0, "culture": self.culture(culture)?, "faith": self.faith(rite)?,
            "dynasty_house_id": self.read.reference(object + 0x158)?,
        });
        let (controller, resources, focus) = if alive != 0 {
            let resources = json!({
                "gold": self.read.fixed(alive + 0x100)?, "piety": self.read.fixed(alive + 0x110)?,
                "prestige": self.read.fixed(alive + 0x130)?, "influence": self.read.fixed(alive + 0x150)?,
                "piety_accumulated": self.read.fixed(alive + 0x118)?, "prestige_accumulated": self.read.fixed(alive + 0x138)?,
                "income": self.read.fixed(alive + 0x2b0)?, "health": self.read.fixed(alive + 0x2e8)?,
                "fertility": self.read.fixed(alive + 0x2d8)?, "stress": self.read.i32(alive + 0x2f8)?,
            });
            let focus = self.read.pointer(alive + 0x280)?;
            let focus = if focus != 0 {
                let definition = self.read.pointer(focus + 8)?;
                json!({"key": self.read.key(definition)?})
            } else {
                Value::Null
            };
            (self.controller(id)?, resources, focus)
        } else {
            (Value::Null, Value::Null, Value::Null)
        };
        let family = if family != 0 {
            json!({
                "father_id": self.read.reference(family)?, "mother_id": self.read.reference(family + 4)?,
                "spouse_id": self.read.reference(family + 0x14)?, "betrothed_id": self.read.reference(family + 0x10)?,
                "child_ids": self.read.ids(family + 0x38)?, "spouse_ids": self.read.ids(family + 0x20)?,
            })
        } else {
            Value::Null
        };
        let court = if court != 0 {
            json!({
                "employer_id": self.read.reference(court + 0xc8)?, "host_id": self.read.reference(court + 0xcc)?,
                "army_id": self.read.reference(court + 0xf4)?, "knight": self.read.u8(court + 0x16c)? != 0,
            })
        } else {
            Value::Null
        };
        let (domain, military) = if landed != 0 {
            let government = self.read.pointer(landed + 0x3f8)?;
            let titles = self.read.ids(landed + 0x1e0)?;
            let primary = titles
                .first()
                .map(|&id| self.title_overview(id))
                .transpose()?;
            let liege = self.character_liege(id, primary.as_ref())?;
            (
                json!({
                    "government": self.read.key(government)?, "primary_title": primary, "liege_id": liege,
                    "title_ids": titles, "capital_title_id": self.read.reference(landed + 0x1f8)?,
                    "domain_limit": self.read.i32(landed + 0x3d8)?, "vassal_limit": self.read.i32(landed + 0x3dc)?,
                    "vassals_towards_limit": self.read.i32(landed + 0x3e0)?, "contract_ids": self.read.ids(landed + 0x218)?,
                    "succession_ids": self.read.ids(landed + 0x3a0)?, "laws": self.read.keys(landed + 0x200)?,
                }),
                json!({
                    "current_strength": self.read.i32(landed + 0x2f0)?, "strength": self.read.i32(landed + 0x2f4)?,
                    "levy": self.read.i32(landed + 0x2f8)?, "strength_for_liege": self.read.i32(landed + 0x2fc)?,
                    "strength_without_hires": self.read.i32(landed + 0x300)?, "war_ids": self.read.ids(landed + 0x318)?,
                    "unit_ids": self.read.ids(landed + 0x278)?, "dread": self.read.fixed(landed + 0x350)?,
                    "tyranny": self.read.fixed(landed + 0x358)?,
                }),
            )
        } else {
            (Value::Null, Value::Null)
        };
        Ok(json!({
            "id": id, "identity": identity, "controller": controller,
            "skills": self.skills(object + 0xd8)?, "base_skills": self.skills(object + 0xc0)?,
            "traits": self.traits(object)?, "resources": resources, "focus": focus, "family": family,
            "court": court, "domain": domain, "military": military,
        }))
    }

    fn title_id(&mut self, key: &str) -> Result<u32> {
        let table = self.table(TITLE)?;
        let revision = table.revision(self.read.memory)?;
        let cached = self
            .catalogs
            .titles
            .as_ref()
            .filter(|index| index.table == table && index.revision == revision)
            .and_then(|index| index.keys.get(key))
            .copied();
        if let Some(id) = cached
            && let Ok(object) = self.lookup(TITLE, id)
        {
            let definition = self.read.pointer(object + 0x48)?;
            if self.read.key(definition)?.as_deref() == Some(key) {
                return Ok(id);
            }
        }
        let mut keys = HashMap::new();
        const CHUNK: u32 = 1024;
        let mut bytes = vec![0; CHUNK as usize * SLOT_BYTES as usize];
        for start in (0..table.length).step_by(CHUNK as usize) {
            let count = (table.length - start).min(CHUNK);
            let bytes = &mut bytes[..count as usize * SLOT_BYTES as usize];
            self.read
                .memory
                .read(table.slots + u64::from(start) * SLOT_BYTES, bytes)?;
            for (index, slot) in bytes
                .as_chunks::<{ SLOT_BYTES as usize }>()
                .0
                .iter()
                .enumerate()
            {
                let object = decode_pointer(array(&slot[8..]))?;
                if object == 0 {
                    continue;
                }
                ensure!(
                    self.read.pointer(object)? == self.base + TITLE.vtable,
                    "CK3 头衔对象类型无效"
                );
                let id = self.read.u32(object + TITLE.id_offset)?;
                ensure!(
                    id & 0x00ff_ffff == start + index as u32,
                    "CK3 头衔对象编号无效"
                );
                let definition = self.read.pointer(object + 0x48)?;
                if let Some(key) = self.read.key(definition)? {
                    keys.insert(key, id);
                }
            }
        }
        ensure!(
            Table::read(self.read.memory, self.base, TITLE)? == table
                && table.revision(self.read.memory)? == revision
                && Clock::read(self.read.memory, self.base)?.follows(self.clock),
            "CK3 头衔索引在读取期间变化"
        );
        let id = keys.get(key).copied();
        self.catalogs.titles = Some(TitleIndex {
            table,
            revision,
            keys,
        });
        id.with_context(|| format!("找不到头衔 {key}"))
    }

    fn title_overview(&mut self, id: u32) -> Result<TitleOverview> {
        let object = self.lookup(TITLE, id)?;
        let definition = self.read.pointer(object + 0x48)?;
        let tier = if definition != 0 {
            self.read.u32(definition + 0x64)?
        } else {
            0
        };
        Ok(TitleOverview {
            id,
            key: self.read.key(definition)?,
            tier: match tier {
                1 => "barony",
                2 => "county",
                3 => "duchy",
                4 => "kingdom",
                5 => "empire",
                6 => "hegemony",
                _ => "special",
            },
            holder_id: self.read.reference(object + 0x128)?,
            de_facto_liege_id: self.read.reference(object + 0xe8)?,
            de_jure_liege_id: self.read.reference(object + 0x108)?,
        })
    }

    fn title(&mut self, id: u32, province_id: Option<u32>) -> Result<TitleDetail> {
        let object = self.lookup(TITLE, id)?;
        let overview = self.title_overview(id)?;
        let holder = overview
            .holder_id
            .map(|id| self.character_name(id))
            .transpose()?;
        let controller = overview
            .holder_id
            .map(|id| self.controller(id))
            .transpose()?;
        let province_id = if overview.tier == "barony" && province_id.is_none() {
            self.barony_province(id)?
        } else {
            province_id
        };
        Ok(TitleDetail {
            overview,
            holder,
            controller,
            province_id,
            capital_title_id: self.read.reference(object + 0xe4)?,
            de_jure_vassal_ids: self.read.ids(object + 0x110)?,
            heir_ids: self.read.ids(object + 0x150)?,
            laws: self.read.keys(object + 0x228)?,
            lessee_id: self.read.reference(object + 0x12c)?,
        })
    }

    fn primary_title(&mut self, id: u32) -> Result<Option<TitleOverview>> {
        let object = self.lookup(CHARACTER, id)?;
        let landed = self.read.pointer(object + 0x1c0)?;
        if landed == 0 {
            return Ok(None);
        }
        let titles = self.read.ids(landed + 0x1e0)?;
        titles
            .first()
            .map(|&title| self.title_overview(title))
            .transpose()
    }

    fn title_holder(&mut self, mut next: Option<u32>, except: Option<u32>) -> Result<Option<u32>> {
        let mut visited = HashSet::new();
        while let Some(id) = next {
            ensure!(visited.insert(id), "CK3 头衔领主关系出现循环");
            let title = self.title_overview(id)?;
            if title.holder_id.is_some() && title.holder_id != except {
                return Ok(title.holder_id);
            }
            next = title.de_facto_liege_id;
        }
        Ok(None)
    }

    fn character_liege(&mut self, id: u32, primary: Option<&TitleOverview>) -> Result<Option<u32>> {
        self.title_holder(primary.and_then(|title| title.de_facto_liege_id), Some(id))
    }

    fn realm(&mut self, id: Option<u32>) -> Result<Option<Value>> {
        let mut current = id;
        let mut visited = HashSet::new();
        while let Some(id) = current {
            ensure!(visited.insert(id), "CK3 人物领主关系出现循环");
            let Some(primary) = self.primary_title(id)? else {
                return Ok(None);
            };
            current = self.character_liege(id, Some(&primary))?;
            if current.is_none() {
                return Ok(Some(json!({
                    "ruler": self.character_name(id)?, "primary_title": primary, "controller": self.controller(id)?,
                })));
            }
        }
        Ok(None)
    }

    fn provinces(&mut self) -> Result<Provinces> {
        if let Some(provinces) = self.provinces {
            return Ok(provinces);
        }
        let provinces = Provinces::read(self.read.memory, self.base, self.clock.data)?;
        self.provinces = Some(provinces);
        Ok(provinces)
    }

    fn province_object(&mut self, id: u32) -> Result<u64> {
        let slot = self.provinces()?.slot(id)?;
        let object = self.read.pointer(slot)?;
        ensure!(
            object != 0
                && self.read.pointer(object)? == self.base + PROVINCE_VTABLE
                && self.read.u32(object + 0x10)? == id,
            "省份 {id} 的对象编号已失效"
        );
        self.province_references.insert((id, object));
        Ok(object)
    }

    fn barony_province(&mut self, title: u32) -> Result<Option<u32>> {
        let provinces = self.provinces()?;
        if let Some(id) = self
            .catalogs
            .baronies
            .as_ref()
            .filter(|index| index.provinces == provinces)
            .and_then(|index| index.titles.get(&title))
            .copied()
        {
            let object = self.province_object(id)?;
            if self.read.reference(object + 0x738)? == Some(title) {
                return Ok(Some(id));
            }
        }
        let mut titles = HashMap::new();
        for id in 1..provinces.count {
            let object = self.read.pointer(provinces.slot(id)?)?;
            if object != 0
                && self.read.pointer(object)? == self.base + PROVINCE_VTABLE
                && self.read.u32(object + 0x10)? == id
                && let Some(title) = self.read.reference(object + 0x738)?
            {
                titles.insert(title, id);
            }
        }
        ensure!(
            Provinces::read(self.read.memory, self.base, self.clock.data)? == provinces
                && Clock::read(self.read.memory, self.base)?.follows(self.clock),
            "CK3 省份索引在读取期间变化"
        );
        let id = titles.get(&title).copied();
        self.catalogs.baronies = Some(BaronyIndex { provinces, titles });
        if let Some(id) = id {
            let object = self.province_object(id)?;
            ensure!(
                self.read.reference(object + 0x738)? == Some(title),
                "省份关联的头衔已变化"
            );
        }
        Ok(id)
    }

    fn province(&mut self, id: u32) -> Result<Value> {
        let object = self.province_object(id)?;
        let title = self
            .read
            .reference(object + 0x738)?
            .map(|title| self.title(title, Some(id)))
            .transpose()?;
        let holder = match &title {
            Some(title) => match title.overview.holder_id {
                Some(holder) => Some(holder),
                None => self.title_holder(title.overview.de_facto_liege_id, None)?,
            },
            None => None,
        };
        let occupier = self.read.reference(object + 0x73c)?;
        let holding = object + 0x620;
        let holding_type = self.read.pointer(holding)?;
        let county = self.read.pointer(object + 0x848)?;
        let county = if county != 0 && self.read.pointer(county)? == self.base + COUNTY_DATA_VTABLE
        {
            let culture = self.read.u32(county + 0x388)?;
            let rite = self.read.u32(county + 0x384)?;
            json!({
                "development": self.read.u32(county + 0x38c)?, "development_progress": self.read.fixed(county + 0x390)?,
                "control": self.read.fixed(county + 0x398)?, "culture": self.culture(culture)?, "faith": self.faith(rite)?,
            })
        } else {
            Value::Null
        };
        Ok(json!({
            "id": id, "title": title, "realm": self.realm(holder)?, "county": county,
            "occupying_character_id": occupier, "occupying_realm": self.realm(occupier)?,
            "holding_type": self.read.key(holding_type)?, "fort_level": self.read.i32(object + 0x850)?,
            "monthly_income": self.read.fixed(holding + 0xf8)?,
            "levy_id": self.read.reference(holding + 0xe8)?, "garrison_id": self.read.reference(holding + 0xec)?,
        }))
    }
}
