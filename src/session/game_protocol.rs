use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::Value;

use crate::world::binary::{self, Index, Span};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Compatibility {
    compatibility_a_hex: String,
    compatibility_b_hex: String,
    compatibility_c_hex: String,
    handshake_flag: u8,
    platform: u32,
}

impl Compatibility {
    pub fn handshake(&self, player: &str) -> Result<Vec<u8>> {
        ensure!(
            !player.is_empty() && !player.contains('\0') && player.len() <= u16::MAX as usize,
            "玩家名称无效"
        );
        let mut bytes = control(1, 90 + player.len() + 1);
        let width = (player.len() + 1) as u32;
        bytes[5..9].copy_from_slice(&width.to_le_bytes());
        bytes[9..9 + player.len()].copy_from_slice(player.as_bytes());
        let offset = 10 + player.len();
        write_hex(&self.compatibility_a_hex, &mut bytes[offset..offset + 16])?;
        write_hex(
            &self.compatibility_b_hex,
            &mut bytes[offset + 16..offset + 32],
        )?;
        write_hex(
            &self.compatibility_c_hex,
            &mut bytes[offset + 32..offset + 65],
        )?;
        bytes[offset + 65] = self.handshake_flag;
        bytes[offset + 66..offset + 70].copy_from_slice(&self.platform.to_le_bytes());
        Ok(bytes)
    }
}

fn write_hex(text: &str, target: &mut [u8]) -> Result<()> {
    let digits: String = text
        .chars()
        .filter(|character| !character.is_ascii_whitespace())
        .collect();
    ensure!(
        digits.len() == target.len() * 2 && digits.is_ascii(),
        "游戏握手兼容字段长度无效"
    );
    for (index, byte) in target.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&digits[index * 2..index * 2 + 2], 16)
            .context("游戏握手字段包含无效十六进制")?;
    }
    Ok(())
}

pub enum Control {
    Password,
    AddPeer { id: i32, local: bool },
    RemovePeer(i32),
    HostDisconnect(u32),
    Handshake(u32),
    Connected,
    DisconnectAck,
    Unknown,
}

impl Control {
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(bytes.len() >= 5, "游戏会话头被截断");
        let size = u32::from_le_bytes(bytes[1..5].try_into().unwrap());
        ensure!(size as usize == bytes.len(), "游戏会话长度不匹配");
        let number = |offset: usize| -> Result<u32> {
            let part = bytes
                .get(offset..offset + 4)
                .context("游戏会话字段被截断")?;
            Ok(u32::from_le_bytes(part.try_into().unwrap()))
        };
        Ok(match bytes[0] {
            1 => Self::Password,
            2 => Self::AddPeer {
                id: number(5)? as i32,
                local: *bytes.get(13).context("玩家标记被截断")? != 0,
            },
            3 => Self::RemovePeer(number(5)? as i32),
            4 => Self::HostDisconnect(number(5)?),
            5 => Self::Handshake(number(5)?),
            6 => Self::Connected,
            7 => Self::DisconnectAck,
            _ => Self::Unknown,
        })
    }
}

pub fn control(kind: u8, size: usize) -> Vec<u8> {
    let mut bytes = vec![0; size];
    bytes[0] = kind;
    bytes[1..5].copy_from_slice(&(size as u32).to_le_bytes());
    bytes
}

pub fn disconnect(kind: u8, reason: u32) -> Vec<u8> {
    let mut bytes = control(kind, 24);
    bytes[5..9].copy_from_slice(&reason.to_le_bytes());
    bytes
}

pub struct Incoming<'a> {
    pub kind: u16,
    bytes: &'a [u8],
    header: Index,
    body: Index,
}

impl<'a> Incoming<'a> {
    pub fn decode(bytes: &'a [u8]) -> Result<Self> {
        let raw = bytes.get(..2).context("游戏命令头被截断")?;
        let kind = u16::from_le_bytes(raw.try_into().unwrap());
        let root = binary::index(bytes, 0..bytes.len(), false)?;
        ensure!(root.len() == 1, "游戏命令需要一个顶层对象");
        let sections = binary::index(
            bytes,
            only(root.values().next().context("游戏命令为空")?)?,
            true,
        )?;
        let header = fields(bytes, &sections, "default_state")?;
        let body = fields(bytes, &sections, "object")?;
        Ok(Self {
            kind,
            bytes,
            header,
            body,
        })
    }

    pub fn sender(&self) -> Result<i32> {
        let value = binary::decode(self.bytes, lookup(&self.header, "sender")?)?;
        Ok(i32::try_from(value.as_i64().context("命令发送者无效")?)?)
    }

    pub fn get(&self, name: &str) -> Result<Value> {
        binary::decode(self.bytes, lookup(&self.body, name)?)
    }

    pub fn info(&self) -> Result<TransferInfo> {
        let filename = self
            .get("filename")?
            .as_str()
            .context("快照名称无效")?
            .to_owned();
        let size = self.number("size")?;
        let checksum = i32::try_from(self.get("checksum")?.as_i64().context("快照校验值无效")?)?;
        Ok(TransferInfo {
            filename,
            size,
            checksum,
        })
    }

    pub fn number(&self, name: &str) -> Result<u32> {
        let value = self.get(name)?;
        if let Some(number) = value.as_u64() {
            return Ok(u32::try_from(number)?);
        }
        bail!("命令字段 {name} 需要非负整数")
    }

    pub fn data(&self) -> Result<&'a [u8]> {
        binary::blob(self.bytes, lookup(&self.body, "data")?)
    }
}

fn fields(bytes: &[u8], table: &Index, name: &str) -> Result<Index> {
    binary::index(bytes, lookup(table, name)?, true)
}

fn lookup(table: &Index, name: &str) -> Result<Span> {
    only(
        table
            .get(name)
            .with_context(|| format!("游戏命令缺少 {name}"))?,
    )
}

fn only(spans: &[Span]) -> Result<Span> {
    if let [span] = spans {
        return Ok(span.clone());
    }
    bail!("游戏命令字段重复")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransferInfo {
    pub filename: String,
    pub size: u32,
    pub checksum: i32,
}

impl TransferInfo {
    pub fn encode(&self, writer: &mut Writer) -> Result<()> {
        writer.text(0x3b6, &self.filename)?;
        writer.unsigned(0x2f, self.size);
        writer.signed(0x179, self.checksum);
        Ok(())
    }
}

#[derive(Default)]
pub struct Writer(Vec<u8>);

impl Writer {
    pub fn command(
        kind: u16,
        identity: u32,
        sender: i32,
        receiver: i32,
        attribs: u8,
        body: Self,
    ) -> Vec<u8> {
        let mut common = Self::default();
        common.unsigned(0xdb, identity);
        common.signed(0x240, sender);
        common.signed(0xc6, receiver);
        common.unsigned(0x69c, attribs.into());
        let mut sections = Self::default();
        sections.object(0x1f3, common);
        sections.object(0x41, body);
        let mut result = Self::default();
        result.object(kind, sections);
        result.0
    }

    fn word(&mut self, value: u16) {
        self.0.extend(value.to_le_bytes());
    }

    fn field(&mut self, key: u16, kind: u16) {
        self.word(key);
        self.word(1);
        self.word(kind);
    }

    fn object(&mut self, key: u16, body: Self) {
        self.field(key, 3);
        self.0.extend(body.0);
        self.word(4);
    }

    pub fn text(&mut self, key: u16, value: &str) -> Result<()> {
        let length = u16::try_from(value.len()).context("命令字符串过长")?;
        self.field(key, 0x0f);
        self.word(length);
        self.0.extend(value.as_bytes());
        Ok(())
    }

    pub fn boolean(&mut self, key: u16, value: bool) {
        self.field(key, 0x0e);
        self.0.push(u8::from(value));
    }

    pub fn unsigned(&mut self, key: u16, value: u32) {
        self.field(key, 0x14);
        self.0.extend(value.to_le_bytes());
    }

    pub fn signed(&mut self, key: u16, value: i32) {
        self.field(key, 0x0c);
        self.0.extend(value.to_le_bytes());
    }

    pub fn data(&mut self, bytes: &[u8]) {
        self.word(0xf0);
        self.word(0x1fc);
        self.0.extend((bytes.len() as u32).to_le_bytes());
        self.0.extend(bytes);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn control_and_command_match_the_reference_encoder() {
        let fields: Compatibility = serde_json::from_value(json!({
            "compatibility_a_hex": "01".repeat(16), "compatibility_b_hex": "02".repeat(16),
            "compatibility_c_hex": "03".repeat(33), "handshake_flag": 1, "platform": 0x4e4d5453,
        }))
        .unwrap();
        let mut expected = [0; 101];
        write_hex("01650000000b00000041494f627365727665720001010101010101010101010101010101020202020202020202020202020202020303030303030303030303030303030303030303030303030303030303030303030153544d4e0000000000000000000000", &mut expected).unwrap();
        assert_eq!(fields.handshake("AIObserver").unwrap(), expected);

        let mut body = Writer::default();
        body.signed(0x16, 2);
        body.boolean(0xd0, true);
        let wire = Writer::command(0x6a6, 17, 2, 0, 14, body);
        let mut expected = vec![0; wire.len()];
        write_hex("a60601000300f30101000300db000100140011000000400201000c0002000000c60001000c00000000009c06010014000e0000000400410001000300160001000c0002000000d00001000e000104000400", &mut expected).unwrap();
        assert_eq!(wire, expected);
        let command = Incoming::decode(&wire).unwrap();
        assert_eq!(command.sender().unwrap(), 2);
        assert_eq!(command.get("status").unwrap(), true);
    }
}
