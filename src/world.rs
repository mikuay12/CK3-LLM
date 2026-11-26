pub(crate) mod binary;
pub mod live;

use std::collections::HashMap;
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use zip::ZipArchive;

use binary::{Index, Span, decode, index};

const MAX_WORLD_BYTES: u64 = 1 << 30;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    #[default]
    Summary,
    Character,
    Title,
    Province,
}

impl QueryKind {
    pub fn request(self, id: &str) -> Result<Query<'_>> {
        Ok(match self {
            Self::Summary => Query::Summary,
            Self::Character => Query::Character(id.parse().context("人物 ID 需要是整数")?),
            Self::Title => {
                ensure!(!id.is_empty(), "请输入头衔 ID 或脚本键");
                Query::Title(id)
            }
            Self::Province => Query::Province(id.parse().context("省份 ID 需要是整数")?),
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub enum Query<'a> {
    Summary,
    Character(u32),
    Title(&'a str),
    Province(u32),
}

pub trait QuerySource: Send + Sync {
    fn query(&self, request: Query<'_>) -> Result<Value>;
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
        }
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
