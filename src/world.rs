pub(crate) mod binary;
pub mod live;
pub mod scripts;
pub mod store;

use std::collections::HashMap;
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use zip::ZipArchive;

use binary::{Index, Span, decode, index};

const MAX_WORLD_BYTES: u64 = 1 << 30;

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    #[default]
    Summary,
    Character,
    Title,
    Province,
    War,
    Army,
    Unit,
    UnitMovement,
    Combat,
    CombatResult,
    Siege,
    Regiment,
    ArmyRegiment,
    MercenaryCompany,
    HoldingBuildings,
    Adjacencies,
    Interaction,
    Decision,
    ActivityType,
    CouncilTaskType,
    CourtPositionType,
    CourtPositionTask,
    SchemeCountermeasure,
    Law,
    MenAtArmsType,
    NotificationType,
    EventDefinition,
    Event,
    IncomingInteraction,
    ActivityInvitation,
    Activity,
    CouncilTask,
    Faction,
    Scheme,
    CourtPosition,
    HolyOrder,
    Secret,
    TaskContract,
    Contract,
    Realm,
    Titles,
    Realms,
    Alerts,
    PlayerMessages,
    MessageFeed,
    PlayerSuccessions,
    ProvinceMap,
    Route,
    Neighbors,
    Borders,
    WarTheater,
    BuildingType,
    ConstructionQuote,
    CasusBelli,
    DeclarationPreview,
    Relations,
    Opinions,
    ObservedCommands,
}

impl QueryKind {
    pub const SNAPSHOT: &'static [Self] =
        &[Self::Summary, Self::Character, Self::Title, Self::Province];
    pub const LIVE: &'static [Self] = &[
        Self::Summary,
        Self::Character,
        Self::Title,
        Self::Province,
        Self::War,
        Self::Army,
        Self::Unit,
        Self::UnitMovement,
        Self::Combat,
        Self::CombatResult,
        Self::Siege,
        Self::Regiment,
        Self::ArmyRegiment,
        Self::MercenaryCompany,
        Self::HoldingBuildings,
        Self::Adjacencies,
        Self::Interaction,
        Self::Decision,
        Self::ActivityType,
        Self::CouncilTaskType,
        Self::CourtPositionType,
        Self::CourtPositionTask,
        Self::SchemeCountermeasure,
        Self::Law,
        Self::MenAtArmsType,
        Self::NotificationType,
        Self::EventDefinition,
        Self::Event,
        Self::IncomingInteraction,
        Self::ActivityInvitation,
        Self::Activity,
        Self::CouncilTask,
        Self::Faction,
        Self::Scheme,
        Self::CourtPosition,
        Self::HolyOrder,
        Self::Secret,
        Self::TaskContract,
        Self::Contract,
        Self::Realm,
        Self::Titles,
        Self::Realms,
        Self::Alerts,
        Self::PlayerMessages,
        Self::MessageFeed,
        Self::PlayerSuccessions,
        Self::ProvinceMap,
        Self::Route,
        Self::Neighbors,
        Self::Borders,
        Self::WarTheater,
        Self::BuildingType,
        Self::ConstructionQuote,
        Self::CasusBelli,
        Self::DeclarationPreview,
        Self::Relations,
        Self::Opinions,
    ];

    pub fn request(self, id: &str) -> Result<Query<'_>> {
        self.request_for(id, None)
    }

    pub fn uses_character(self) -> bool {
        matches!(
            self,
            Self::Province
                | Self::Decision
                | Self::ActivityType
                | Self::Event
                | Self::IncomingInteraction
                | Self::ActivityInvitation
                | Self::Activity
                | Self::Alerts
                | Self::PlayerMessages
                | Self::Titles
                | Self::WarTheater
                | Self::ConstructionQuote
                | Self::DeclarationPreview
                | Self::Relations
                | Self::Opinions
        ) || self.is_entity()
    }

    pub fn is_entity(self) -> bool {
        matches!(
            self,
            Self::CouncilTask
                | Self::Faction
                | Self::Scheme
                | Self::CourtPosition
                | Self::HolyOrder
                | Self::Secret
                | Self::TaskContract
                | Self::Contract
                | Self::MercenaryCompany
        )
    }

    pub fn is_collection(self) -> bool {
        self.is_entity()
            || matches!(
                self,
                Self::Titles
                    | Self::Realms
                    | Self::Alerts
                    | Self::PlayerMessages
                    | Self::MessageFeed
                    | Self::Activity
                    | Self::PlayerSuccessions
            )
    }

    pub(crate) fn collection_alias(name: &str) -> Option<Self> {
        Some(match name {
            "council_tasks" => Self::CouncilTask,
            "factions" => Self::Faction,
            "schemes" => Self::Scheme,
            "court_positions" => Self::CourtPosition,
            "holy_orders" => Self::HolyOrder,
            "secrets" => Self::Secret,
            "task_contracts" => Self::TaskContract,
            "contracts" => Self::Contract,
            "mercenary_companies" => Self::MercenaryCompany,
            "activities" => Self::Activity,
            _ => return None,
        })
    }

    pub fn request_for(self, id: &str, character: Option<u32>) -> Result<Query<'_>> {
        Ok(match self {
            Self::Summary => Query::Summary,
            Self::Character => Query::Character(id.parse().context("人物 ID 需要是整数")?),
            Self::Title => {
                ensure!(!id.is_empty(), "请输入头衔 ID 或脚本键");
                Query::Title(id)
            }
            Self::Realm => {
                ensure!(!id.is_empty(), "请输入领主 ID 或头衔脚本键");
                Query::Realm(id)
            }
            Self::ProvinceMap => Query::ProvinceMap(id.parse().context("省份 ID 需要是整数")?),
            Self::Route => {
                let (origin, target) = id
                    .split_once(',')
                    .context("请输入起点和终点省份 ID，以逗号分隔")?;
                Query::Navigation(Navigation::Route {
                    origin: origin.trim().parse()?,
                    target: target.trim().parse()?,
                    speed: None,
                    unit: None,
                    include_straits: true,
                    max_nodes: 20_000,
                })
            }
            kind @ (Self::Neighbors | Self::Borders) => {
                ensure!(!id.is_empty(), "请输入领主 ID 或头衔脚本键");
                Query::Navigation(Navigation::Borders {
                    realm: id,
                    other: None,
                    neighbors: kind == Self::Neighbors,
                    include_straits: false,
                    offset: 0,
                    limit: 20,
                })
            }
            Self::WarTheater => Query::Navigation(Navigation::Theater {
                war: id.parse()?,
                character,
                max_armies: 8,
                max_targets: 8,
                max_nodes: 4000,
            }),
            Self::BuildingType if id.is_empty() => Query::BuildingTypes {
                search: "",
                offset: 0,
                limit: 20,
            },
            Self::BuildingType => Query::BuildingType(id),
            Self::ConstructionQuote => {
                let parts: Vec<_> = id.split(',').map(str::trim).collect();
                let [province, building, slot @ ..] = parts.as_slice() else {
                    bail!("请输入省份 ID、建筑键及可选槽位，以逗号分隔");
                };
                ensure!(slot.len() <= 1 && !building.is_empty(), "建筑报价参数无效");
                Query::ConstructionQuote {
                    province: province.parse()?,
                    building,
                    character,
                    slot: slot.first().map(|slot| slot.parse()).transpose()?,
                    treasury: false,
                }
            }
            Self::DeclarationPreview => {
                let (target, cb) = id
                    .split_once(',')
                    .context("请输入目标角色 ID 和宣战理由键，以逗号分隔")?;
                Query::Declaration {
                    actor: character,
                    target: target.trim().parse()?,
                    casus_belli: cb.trim(),
                    titles: &[],
                    claimant: u32::MAX,
                }
            }
            kind @ (Self::Relations | Self::Opinions) => Query::People {
                kind,
                id: id.parse()?,
                other: character,
                offset: 0,
                limit: 20,
            },
            Self::Province => {
                let province = id.parse().context("省份 ID 需要是整数")?;
                match character {
                    Some(character) => Query::ProvinceSupply {
                        province,
                        character,
                    },
                    None => Query::Province(province),
                }
            }
            kind if kind.is_collection()
                && (id.is_empty() || !kind.is_entity() && kind != Self::Activity) =>
            {
                Query::Entities(EntityList {
                    kind,
                    character,
                    search: id,
                    offset: 0,
                    limit: 20,
                    include_inactive: false,
                })
            }
            kind @ (Self::Event
            | Self::IncomingInteraction
            | Self::ActivityInvitation
            | Self::Activity) => Query::Pending {
                kind,
                id: id.parse().context("查询 ID 需要是整数")?,
                character,
            },
            kind if kind.is_script() && id.is_empty() => Query::ScriptList {
                kind,
                search: "",
                group: None,
                offset: 0,
                limit: 20,
            },
            kind if kind.is_script() => Query::Script {
                kind,
                id,
                character,
            },
            Self::ObservedCommands => Query::ObservedCommands {
                name: id,
                offset: 0,
                limit: 20,
            },
            kind => Query::Native(kind, id.parse().context("查询 ID 需要是整数")?),
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub enum Query<'a> {
    Summary,
    Character(u32),
    Title(&'a str),
    Realm(&'a str),
    Province(u32),
    ProvinceSupply {
        province: u32,
        character: u32,
    },
    Native(QueryKind, u32),
    Military {
        kind: MilitaryList,
        character: u32,
        offset: usize,
        limit: usize,
    },
    Script {
        kind: QueryKind,
        id: &'a str,
        character: Option<u32>,
    },
    ScriptList {
        kind: QueryKind,
        search: &'a str,
        group: Option<&'a str>,
        offset: usize,
        limit: usize,
    },
    Pending {
        kind: QueryKind,
        id: u32,
        character: Option<u32>,
    },
    ObservedCommands {
        name: &'a str,
        offset: usize,
        limit: usize,
    },
    Entities(EntityList<'a>),
    ProvinceMap(u32),
    Navigation(Navigation<'a>),
    BuildingType(&'a str),
    BuildingTypes {
        search: &'a str,
        offset: usize,
        limit: usize,
    },
    ConstructionQuote {
        province: u32,
        building: &'a str,
        character: Option<u32>,
        slot: Option<i32>,
        treasury: bool,
    },
    Declaration {
        actor: Option<u32>,
        target: u32,
        casus_belli: &'a str,
        titles: &'a [u32],
        claimant: u32,
    },
    People {
        kind: QueryKind,
        id: u32,
        other: Option<u32>,
        offset: usize,
        limit: usize,
    },
}

#[derive(Clone, Copy, Debug)]
pub enum Navigation<'a> {
    Route {
        origin: u32,
        target: u32,
        speed: Option<f64>,
        unit: Option<u32>,
        include_straits: bool,
        max_nodes: usize,
    },
    Borders {
        realm: &'a str,
        other: Option<&'a str>,
        neighbors: bool,
        include_straits: bool,
        offset: usize,
        limit: usize,
    },
    Theater {
        war: u32,
        character: Option<u32>,
        max_armies: usize,
        max_targets: usize,
        max_nodes: usize,
    },
}

#[derive(Clone, Copy, Debug)]
pub struct EntityList<'a> {
    pub kind: QueryKind,
    pub character: Option<u32>,
    pub search: &'a str,
    pub offset: usize,
    pub limit: usize,
    pub include_inactive: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MilitaryList {
    Wars,
    Armies,
    Combats,
    CombatResults,
    Sieges,
}

pub trait QuerySource: Send + Sync {
    fn query(&self, request: Query<'_>) -> Result<Value>;

    fn kinds(&self) -> &'static [QueryKind] {
        QueryKind::SNAPSHOT
    }
}

pub struct World {
    bytes: Vec<u8>,
    root: Arc<Index>,
    objects: Mutex<HashMap<Span, Arc<Index>>>,
    layers: Vec<Value>,
    source_sha256: String,
}

impl World {
    pub fn open(path: &Path) -> Result<Self> {
        let mut bytes = Vec::new();
        let file = File::open(path).with_context(|| format!("读取存档 {}", path.display()))?;
        file.take(MAX_WORLD_BYTES + 1).read_to_end(&mut bytes)?;
        Self::from_bytes(bytes)
    }

    pub fn from_bytes(bytes: Vec<u8>) -> Result<Self> {
        ensure!(bytes.len() as u64 <= MAX_WORLD_BYTES, "世界快照超过 1 GiB");
        let source_sha256 = format!("{:x}", Sha256::digest(&bytes));
        let (bytes, layers) = unpack(bytes)?;
        let root = Arc::new(index(&bytes, 0..bytes.len(), false)?);
        Ok(Self {
            bytes,
            root,
            layers,
            source_sha256,
            objects: Mutex::default(),
        })
    }

    pub(crate) fn binary(&self) -> &[u8] {
        &self.bytes
    }

    pub fn summary(&self) -> Result<Value> {
        let sections: Map<_, _> = self
            .root
            .iter()
            .map(|(key, values)| (key.clone(), json!(values.len())))
            .collect();
        let mut counts = Map::new();
        for name in ["living", "dead_unprunable", "provinces", "landed_titles"] {
            if self.root.contains_key(name) {
                counts.insert(name.into(), json!(self.records(name)?.len()));
            }
        }
        let mut result = json!({"encoding": "jomini_binary", "world_bytes": self.bytes.len(),
            "containers": self.layers, "sections": sections, "source_sha256": self.source_sha256, "record_counts": counts});
        for name in [
            "meta_data",
            "date",
            "speed",
            "currently_played_characters",
            "played_character",
        ] {
            if let Some(spans) = self.root.get(name) {
                result[name] = self.values(spans)?;
            }
        }
        Ok(result)
    }

    pub fn character(&self, id: u32) -> Result<Value> {
        let key = id.to_string();
        for section in ["living", "dead_unprunable", "characters"] {
            if self.root.contains_key(section)
                && let Some(spans) = self.records(section)?.get(&key)
            {
                return Ok(json!({"id": id, "section": section, "data": self.values(spans)?}));
            }
        }
        bail!("找不到人物 {id}")
    }

    pub fn province(&self, id: u32) -> Result<Value> {
        let entries = self.records("provinces")?;
        let spans = entries
            .get(&id.to_string())
            .with_context(|| format!("找不到省份 {id}"))?;
        Ok(json!({"id": id, "data": self.values(spans)?}))
    }

    pub fn title(&self, reference: &str) -> Result<Value> {
        let entries = self.records("landed_titles")?;
        let id = if let Ok(id) = reference.parse::<u32>() {
            id.to_string()
        } else {
            let mut found = None;
            for (id, spans) in entries.iter() {
                let span = single(spans)?;
                if self.bytes[span.start..].starts_with(&[3, 0]) {
                    let fields = self.object(span)?;
                    if let Some(spans) = fields.get("key")
                        && self.values(spans)?.as_str() == Some(reference)
                    {
                        found = Some(id.to_owned());
                        break;
                    }
                }
            }
            found.with_context(|| format!("找不到头衔 {reference}"))?
        };
        let spans = entries
            .get(&id)
            .with_context(|| format!("找不到头衔 {id}"))?;
        let data = decode(&self.bytes, single(spans)?)?;
        ensure!(data != "none", "头衔 {id} 已删除");
        Ok(json!({"id": id.parse::<u32>()?, "data": data}))
    }

    fn values(&self, spans: &[Span]) -> Result<Value> {
        if let [span] = spans {
            return decode(&self.bytes, span.clone());
        }
        let values = spans
            .iter()
            .cloned()
            .map(|span| decode(&self.bytes, span))
            .collect::<Result<Vec<_>>>()?;
        Ok(Value::Array(values))
    }

    fn records(&self, name: &str) -> Result<Arc<Index>> {
        let spans = self
            .root
            .get(name)
            .with_context(|| format!("快照缺少 {name}"))?;
        let mut group = self.object(single(spans)?)?;
        if name == "landed_titles" {
            let spans = group.get(name).context("快照缺少头衔记录")?;
            group = self.object(single(spans)?)?;
        }
        Ok(group)
    }

    fn object(&self, span: Span) -> Result<Arc<Index>> {
        let mut cache = self.objects.lock().unwrap();
        if let Some(group) = cache.get(&span) {
            return Ok(Arc::clone(group));
        }
        let group = Arc::new(index(&self.bytes, span.clone(), true)?);
        cache.insert(span, Arc::clone(&group));
        Ok(group)
    }
}

impl QuerySource for World {
    fn query(&self, request: Query<'_>) -> Result<Value> {
        match request {
            Query::Summary => self.summary(),
            Query::Character(id) => self.character(id),
            Query::Title(id) => self.title(id),
            Query::Province(id) => self.province(id),
            Query::Native(..)
            | Query::Military { .. }
            | Query::Pending { .. }
            | Query::ProvinceSupply { .. }
            | Query::Realm(_)
            | Query::Entities(_)
            | Query::Navigation(_)
            | Query::BuildingType(_)
            | Query::BuildingTypes { .. }
            | Query::ConstructionQuote { .. }
            | Query::Declaration { .. }
            | Query::People { .. } => bail!("此查询需要实时游戏"),
            Query::ProvinceMap(_) => bail!("此查询需要游戏安装目录"),
            Query::Script { .. } | Query::ScriptList { .. } => bail!("此查询需要游戏安装目录"),
            Query::ObservedCommands { .. } => bail!("此查询需要回放数据库"),
        }
    }
}

pub(crate) fn date_text(raw: u32) -> String {
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

fn single(spans: &[Span]) -> Result<Span> {
    if let [span] = spans {
        return Ok(span.clone());
    }
    bail!("同名对象出现多次")
}

fn unpack(mut bytes: Vec<u8>) -> Result<(Vec<u8>, Vec<Value>)> {
    let mut layers = Vec::new();
    for _ in 0..4 {
        if let Ok(mut archive) = ZipArchive::new(Cursor::new(bytes.as_slice())) {
            let mut all = Vec::new();
            for position in 0..archive.len() {
                let entry = archive.by_index(position)?;
                if !entry.is_dir() {
                    all.push((position, entry.name().to_owned()));
                }
            }
            let mut candidates: Vec<_> =
                all.iter().filter(|(_, name)| name == "gamestate").collect();
            if candidates.is_empty() {
                candidates.extend(all.iter().filter(|(_, name)| name.starts_with("hotjoin_")));
            }
            if candidates.is_empty() && all.len() == 1 {
                candidates.extend(&all);
            }
            ensure!(
                candidates.len() == 1,
                "ZIP 需要可唯一识别的 gamestate 或 hotjoin 条目"
            );
            let (position, name) = candidates[0];
            let mut entry = archive.by_index(*position)?;
            ensure!(entry.size() <= MAX_WORLD_BYTES, "解压后的世界超过 1 GiB");
            let mut decoded = Vec::new();
            (&mut entry)
                .take(MAX_WORLD_BYTES + 1)
                .read_to_end(&mut decoded)?;
            ensure!(
                decoded.len() as u64 <= MAX_WORLD_BYTES,
                "解压后的世界超过 1 GiB"
            );
            layers.push(json!({"kind": "zip", "entry": name, "size": decoded.len()}));
            drop(entry);
            drop(archive);
            bytes = decoded;
        } else if bytes.starts_with(b"SAV") {
            let end = bytes
                .iter()
                .take(64)
                .position(|&byte| byte == b'\n')
                .context("无法识别存档头")?;
            layers.push(json!({"kind": "sav_header", "header": str::from_utf8(&bytes[..end])?}));
            bytes.drain(..=end);
        } else {
            ensure!(
                bytes.len() >= 6 && bytes[2..4] == [1, 0],
                "需要 CK3 二进制存档或热加入快照"
            );
            return Ok((bytes, layers));
        }
    }
    bail!("世界快照容器层数超出范围")
}
