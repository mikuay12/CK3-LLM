use std::collections::{BTreeMap, HashMap, HashSet};
use std::ops::Range;
use std::sync::OnceLock;

use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Map, Value, json};

const EQUAL: u16 = 1;
const OPEN: u16 = 3;
const CLOSE: u16 = 4;
const RGB: u16 = 0x243;
const BLOB: u16 = 0x1fc;
const DATA: u16 = 0xf0;

pub type Span = Range<usize>;
pub type Index = BTreeMap<String, Vec<Span>>;

#[derive(Deserialize)]
struct TokenFile {
    tokens: HashMap<String, String>,
}

fn symbol(tag: u16) -> Option<&'static str> {
    static TOKENS: OnceLock<HashMap<u16, String>> = OnceLock::new();
    let tokens = TOKENS.get_or_init(|| {
        let file: TokenFile = serde_json::from_str(include_str!("ck3_tokens.json"))
            .expect("bundled token dictionary");
        file.tokens
            .into_iter()
            .map(|(key, value)| {
                let tag =
                    u16::from_str_radix(key.strip_prefix("0x").expect("token hex prefix"), 16)
                        .expect("u16 token");
                (tag, value)
            })
            .collect()
    });
    tokens.get(&tag).map(String::as_str)
}

pub fn index(bytes: &[u8], span: Span, wrapped: bool) -> Result<Index> {
    let mut reader = Reader::new(bytes, span)?;
    if wrapped {
        ensure!(
            reader.peek() == Some(OPEN) && reader.bytes.ends_with(&CLOSE.to_le_bytes()),
            "世界查询路径需要对象"
        );
        reader.take(2)?;
        reader.bytes = &reader.bytes[..reader.bytes.len() - 2];
    }
    let mut output: Index = BTreeMap::new();
    while reader.position < reader.bytes.len() {
        let raw_key = reader.token()?;
        let key = key(raw_key)?;
        if reader.peek() == Some(EQUAL) {
            reader.take(2)?;
        } else {
            ensure!(
                raw_key.tag == DATA && reader.peek() == Some(BLOB),
                "世界属性缺少赋值标记"
            );
        }
        let start = reader.base + reader.position;
        reader.skip_value()?;
        output
            .entry(key)
            .or_default()
            .push(start..reader.base + reader.position);
    }
    Ok(output)
}

pub fn decode(bytes: &[u8], span: Span) -> Result<Value> {
    let mut reader = Reader::new(bytes, span)?;
    let node = reader.value(0)?;
    ensure!(
        reader.position == reader.bytes.len(),
        "世界属性包含未解析数据"
    );
    Ok(node)
}

pub fn blob(bytes: &[u8], span: Span) -> Result<&[u8]> {
    let mut reader = Reader::new(bytes, span)?;
    let raw = reader.token()?;
    ensure!(
        raw.tag == BLOB && reader.position == reader.bytes.len(),
        "预期二进制数据块"
    );
    Ok(raw.data)
}

struct Reader<'a> {
    bytes: &'a [u8],
    position: usize,
    base: usize,
}

#[derive(Copy, Clone)]
struct Raw<'a> {
    tag: u16,
    data: &'a [u8],
}

impl<'a> Reader<'a> {
    fn new(bytes: &'a [u8], span: Span) -> Result<Self> {
        let base = span.start;
        let bytes = bytes.get(span).context("世界属性位置越界")?;
        Ok(Self {
            bytes,
            position: 0,
            base,
        })
    }

    fn take(&mut self, size: usize) -> Result<&'a [u8]> {
        let end = self.position + size;
        let bytes = self
            .bytes
            .get(self.position..end)
            .with_context(|| format!("世界数据在偏移 {} 截断", self.base + self.position))?;
        self.position = end;
        Ok(bytes)
    }

    fn peek(&self) -> Option<u16> {
        Some(u16::from_le_bytes(
            self.bytes
                .get(self.position..self.position + 2)?
                .try_into()
                .ok()?,
        ))
    }

    fn token(&mut self) -> Result<Raw<'a>> {
        let tag = u16::from_le_bytes(self.take(2)?.try_into().unwrap());
        let size = match tag {
            0x0c | 0x0d | 0x14 => 4,
            0x0e => 1,
            0x167 | 0x29c => 8,
            0x0f | 0x17 => u16::from_le_bytes(self.take(2)?.try_into().unwrap()) as usize,
            BLOB => u32::from_le_bytes(self.take(4)?.try_into().unwrap()) as usize,
            EQUAL | OPEN | CLOSE => 0,
            tag if tag < 0x16 && symbol(tag).is_none() => bail!("无法识别保留的世界词元 {tag:#x}"),
            _ => 0,
        };
        Ok(Raw {
            tag,
            data: self.take(size)?,
        })
    }

    fn skip_value(&mut self) -> Result<()> {
        let mut first = self.token()?.tag;
        if first == RGB && self.peek() == Some(OPEN) {
            first = self.token()?.tag;
        }
        ensure!(first != EQUAL && first != CLOSE, "世界属性值无效");
        if first != OPEN {
            return Ok(());
        }
        let mut depth = 1;
        while depth > 0 {
            match self.token()?.tag {
                OPEN => {
                    depth += 1;
                    ensure!(depth <= 128, "世界对象嵌套过深");
                }
                CLOSE => depth -= 1,
                _ => {}
            }
        }
        Ok(())
    }

    fn value(&mut self, depth: usize) -> Result<Value> {
        let raw = self.token()?;
        self.read_value(raw, depth)
    }

    fn read_value(&mut self, raw: Raw<'a>, depth: usize) -> Result<Value> {
        ensure!(depth <= 128, "世界对象嵌套过深");
        match raw.tag {
            OPEN => self.group(depth + 1),
            RGB if self.peek() == Some(OPEN) => Ok(json!({"rgb": self.value(depth + 1)?})),
            EQUAL | CLOSE => bail!("无效的世界属性值"),
            _ => scalar(raw),
        }
    }

    fn group(&mut self, depth: usize) -> Result<Value> {
        let mut named = Map::new();
        let mut anonymous = Vec::new();
        let mut repeated = HashSet::new();
        loop {
            let raw = self.token()?;
            if raw.tag == CLOSE {
                break;
            }
            let next = self.peek();
            if next == Some(EQUAL) || (raw.tag == DATA && next == Some(BLOB)) {
                if next == Some(EQUAL) {
                    self.take(2)?;
                }
                let name = key(raw)?;
                let value = self.value(depth)?;
                if repeated.contains(&name) {
                    named
                        .get_mut(&name)
                        .and_then(Value::as_array_mut)
                        .context("重复属性无效")?
                        .push(value);
                } else if let Some(previous) = named.insert(name.clone(), value) {
                    let current = named.remove(&name).unwrap();
                    repeated.insert(name.clone());
                    named.insert(name, Value::Array(vec![previous, current]));
                }
            } else {
                anonymous.push(self.read_value(raw, depth)?);
            }
        }
        Ok(match (named.is_empty(), anonymous.is_empty()) {
            (true, _) => Value::Array(anonymous),
            (false, true) => Value::Object(named),
            _ => json!({"properties": named, "values": anonymous}),
        })
    }
}

fn key(raw: Raw<'_>) -> Result<String> {
    match scalar(raw)? {
        Value::String(text) => Ok(text),
        Value::Number(number) if matches!(raw.tag, 0x0c | 0x14 | 0x29c) => Ok(number.to_string()),
        _ => bail!("世界属性名称无效"),
    }
}

fn scalar(raw: Raw<'_>) -> Result<Value> {
    let data = raw.data;
    Ok(match raw.tag {
        0x0c => json!(i32::from_le_bytes(data.try_into().unwrap())),
        0x0d => json!(f32::from_le_bytes(data.try_into().unwrap())),
        0x0e => match data[0] {
            0 => json!(false),
            1 => json!(true),
            _ => bail!("无效的布尔值"),
        },
        0x0f | 0x17 => json!(String::from_utf8_lossy(data)),
        0x14 => json!(u32::from_le_bytes(data.try_into().unwrap())),
        0x167 => json!(i64::from_le_bytes(data.try_into().unwrap()) as f64 / 100_000.0),
        0x29c => json!(u64::from_le_bytes(data.try_into().unwrap())),
        BLOB => {
            const HEX: &[u8] = b"0123456789abcdef";
            let mut text = String::with_capacity(data.len() * 2);
            for byte in data {
                text.push(HEX[(byte >> 4) as usize] as char);
                text.push(HEX[(byte & 15) as usize] as char);
            }
            json!({"hex": text})
        }
        OPEN | CLOSE | EQUAL => bail!("结构标记不能作为标量"),
        tag => json!(symbol(tag).map_or_else(|| format!("token_{tag:#06x}"), str::to_owned)),
    })
}
