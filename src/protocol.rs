use anyhow::{Context, Result, bail, ensure};

pub const GAME_DATA_OPCODE: i64 = 255;
const HEADER_SIZE: usize = 48;
const SUFFIX_SIZE: usize = 64;
const RECORD_HEADER_SIZE: usize = 12;
const MAX_FRAME_SIZE: usize = 1 << 20;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u16)]
pub enum Magic {
    Directed = 0xcafe,
    Alternate = 0xbca5,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Message<'a> {
    pub word_4: u32,
    pub word_8: u32,
    pub payload: &'a [u8],
}

#[derive(Debug, Eq, PartialEq)]
pub struct Envelope<'a> {
    pub magic: Magic,
    pub recipient: &'a str,
    pub sequence: u64,
    pub source_user: &'a str,
    pub messages: Vec<Message<'a>>,
}

impl Envelope<'_> {
    pub fn encode(&self) -> Result<Vec<u8>> {
        check_identifier(self.recipient, 39).context("目标玩家 ID")?;
        check_identifier(self.source_user, 48).context("发送玩家 ID")?;
        let count = u16::try_from(self.messages.len()).context("消息数超过 65535")?;

        let mut frame_size = HEADER_SIZE;
        for message in &self.messages {
            let available = MAX_FRAME_SIZE - frame_size;
            ensure!(
                available >= RECORD_HEADER_SIZE
                    && message.payload.len() <= available - RECORD_HEADER_SIZE,
                "PDX 帧超过 1 MiB"
            );
            frame_size += RECORD_HEADER_SIZE + message.payload.len();
        }

        let mut bytes = vec![0; HEADER_SIZE];
        bytes.reserve(frame_size + SUFFIX_SIZE + 1 - HEADER_SIZE);
        bytes[..2].copy_from_slice(&(self.magic as u16).to_le_bytes());
        bytes[2..4].copy_from_slice(&count.to_le_bytes());
        bytes[4..8].copy_from_slice(&(frame_size as u32).to_le_bytes());
        if self.magic == Magic::Directed {
            bytes[8..8 + self.recipient.len()].copy_from_slice(self.recipient.as_bytes());
        }

        for message in &self.messages {
            let record_size = (message.payload.len() + 8) as u32;
            bytes.extend_from_slice(&record_size.to_le_bytes());
            bytes.extend_from_slice(&message.word_4.to_le_bytes());
            bytes.extend_from_slice(&message.word_8.to_le_bytes());
            bytes.extend_from_slice(message.payload);
        }

        let mut suffix = [0; SUFFIX_SIZE + 1];
        suffix[..2].copy_from_slice(&(SUFFIX_SIZE as u16).to_le_bytes());
        suffix[8..16].copy_from_slice(&self.sequence.to_le_bytes());
        suffix[16..16 + self.source_user.len()].copy_from_slice(self.source_user.as_bytes());
        bytes.extend_from_slice(&suffix);
        Ok(bytes)
    }
}

pub fn decode(bytes: &[u8]) -> Result<Envelope<'_>> {
    ensure!(bytes.len() > HEADER_SIZE + SUFFIX_SIZE, "PDX 帧过短");
    let frame_size = bytes.len() - SUFFIX_SIZE - 1;
    let (frame, suffix) = bytes.split_at(frame_size);
    ensure!(suffix[SUFFIX_SIZE] == 0, "PDX 帧缺少结尾零字节");
    ensure!(
        u16_at(suffix, 0) as usize == SUFFIX_SIZE,
        "PDX 后缀长度错误"
    );
    ensure!(
        frame_size == u32_at(frame, 4) as usize && frame_size <= MAX_FRAME_SIZE,
        "PDX 帧长度不匹配"
    );

    let magic = match u16_at(frame, 0) {
        0xcafe => Magic::Directed,
        0xbca5 => Magic::Alternate,
        _ => bail!("未知 PDX 帧类型"),
    };
    let count = u16_at(frame, 2) as usize;
    let mut records = &frame[HEADER_SIZE..];
    ensure!(
        count <= records.len() / RECORD_HEADER_SIZE,
        "PDX 消息头被截断"
    );

    let mut messages = Vec::with_capacity(count);
    for _ in 0..count {
        ensure!(records.len() >= RECORD_HEADER_SIZE, "PDX 消息头被截断");
        let size = u32_at(records, 0) as usize;
        ensure!(size >= 8 && size <= records.len() - 4, "PDX 消息长度错误");
        let (record, remaining) = records.split_at(size + 4);
        messages.push(Message {
            word_4: u32_at(record, 4),
            word_8: u32_at(record, 8),
            payload: &record[RECORD_HEADER_SIZE..],
        });
        records = remaining;
    }
    ensure!(records.is_empty(), "PDX 消息计数与帧内容不匹配");

    Ok(Envelope {
        magic,
        recipient: read_identifier(&frame[8..HEADER_SIZE])?,
        sequence: u64::from_le_bytes(suffix[8..16].try_into().unwrap()),
        source_user: read_identifier(&suffix[16..SUFFIX_SIZE])?,
        messages,
    })
}

fn check_identifier(value: &str, max_len: usize) -> Result<()> {
    ensure!(
        value.len() <= max_len && value.is_ascii() && !value.contains('\0'),
        "ID 必须是长度不超过 {max_len} 的 ASCII 字符串"
    );
    Ok(())
}

fn read_identifier(bytes: &[u8]) -> Result<&str> {
    let id = bytes.split(|&byte| byte == 0).next().unwrap_or_default();
    ensure!(id.is_ascii(), "PDX 玩家 ID 包含非 ASCII 字节");
    Ok(str::from_utf8(id)?)
}

fn u16_at(bytes: &[u8], at: usize) -> u16 {
    u16::from_le_bytes(bytes[at..at + 2].try_into().unwrap())
}

fn u32_at(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap())
}
