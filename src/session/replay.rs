use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::protocol::{self, GAME_DATA_OPCODE};
use crate::settings::write_json;
use crate::world::{
    World,
    store::{Observation, WorldStore},
};

use super::game_protocol::{Incoming, TransferInfo};
use super::transfer::{
    ANNOUNCE, FAST_CHUNK, FAST_HANDSHAKE, SERIAL_CHUNK, SERIAL_METADATA, Transfer, verify,
};

#[cfg(test)]
mod tests;

const MAX_PACKET: u64 = (1 << 20) + 65;
const MAX_LOG_LINE: u64 = 16 << 20;

pub struct Replay {
    pub records: Vec<Record>,
    pub files: Vec<Reassembled>,
    pub summary: Summary,
    pub errors: VecDeque<String>,
    pub output: PathBuf,
}

#[derive(Default, Serialize)]
pub struct Summary {
    pub frames: u64,
    pub commands: BTreeMap<String, u64>,
    pub control_messages: u64,
    pub decode_errors: u64,
    pub verified_files: u64,
    pub incomplete_transfers: usize,
    pub synchronization: Synchronization,
}

#[derive(Default, Serialize)]
pub struct Synchronization {
    pub hotjoin: Option<Value>,
    pub peer_turns: BTreeMap<u32, u32>,
    pub daily_ticks: u64,
    pub event_ticks: u64,
}

#[derive(Serialize)]
pub struct Reassembled {
    pub name: String,
    pub path: PathBuf,
    pub snapshot: bool,
    pub size: u32,
    pub checksum: i32,
    #[serde(skip)]
    room: String,
}

pub struct Record {
    pub name: String,
    pub sequence: u64,
    pub channel: u32,
    pub recipient: String,
    pub source: String,
    pub line: usize,
    packet: Arc<Packet>,
    message: usize,
}

impl Record {
    pub fn describe(&self) -> Result<Value> {
        let bytes = self.packet.read()?;
        let frame = protocol::decode(&bytes)?;
        let message = frame
            .messages
            .get(self.message)
            .context("记录中的消息已不存在")?;
        let message_data = match message.word_8 {
            0 => Incoming::decode(message.payload)?.describe()?,
            1 => json!({"type": control_type(message.payload)?, "bytes": message.payload.len()}),
            _ => json!({"bytes": message.payload.len()}),
        };
        Ok(json!({
            "line": self.line, "time": self.packet.time, "match_id": self.packet.room,
            "direction": self.packet.direction, "source": self.source, "recipient": self.recipient,
            "sequence": self.sequence, "channel": self.channel, "message": message_data,
        }))
    }

    fn observation(&self) -> Result<Observation> {
        let bytes = self.packet.read()?;
        let frame = protocol::decode(&bytes)?;
        let item = frame
            .messages
            .get(self.message)
            .context("记录中的消息已不存在")?;
        let command = Incoming::decode(item.payload)?;
        let data = if matches!(command.kind, FAST_CHUNK | SERIAL_CHUNK) {
            let info = command.info()?;
            json!({"type":command.name(), "fields":{"filename":info.filename, "size":info.size, "checksum":info.checksum,
                "chunk_id":command.number("chunk_id")?, "chunk_bytes":command.data()?.len()}})
        } else {
            command.describe()?
        };
        let path = self
            .packet
            .path
            .strip_prefix(&self.packet.root)
            .unwrap_or(&self.packet.path);
        let packet = path.to_string_lossy();
        let key = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&json!([
                self.packet.room,
                self.packet.direction,
                self.source,
                packet,
                self.message,
                format!("{:x}", Sha256::digest(&bytes)),
            ]))?)
        );
        Ok(Observation {
            time: self.packet.time.as_str().map(str::to_owned),
            direction: self.packet.direction,
            source: self.source.clone(),
            packet: packet.into_owned(),
            name: self.name.clone(),
            identity: command.identity().ok(),
            data,
            key,
        })
    }
}

struct Packet {
    root: Arc<Path>,
    path: PathBuf,
    digest: String,
    size: Option<u64>,
    time: Value,
    room: String,
    direction: &'static str,
}

impl Packet {
    fn from_event(root: Arc<Path>, event: &Value, direction: &'static str) -> Result<Self> {
        let name = event["payload_file"]
            .as_str()
            .context("记录缺少包文件路径")?;
        Ok(Self {
            path: root.join(name),
            root,
            digest: event["payload_sha256"].as_str().unwrap_or_default().into(),
            size: event["payload_size"].as_u64(),
            time: event["time"].clone(),
            room: event["match_id"].as_str().unwrap_or_default().into(),
            direction,
        })
    }

    fn read(&self) -> Result<Vec<u8>> {
        let path = fs::canonicalize(&self.path).context("打开记录中的包文件")?;
        ensure!(
            path.starts_with(&self.root) && path.is_file(),
            "包文件需要位于原抓包目录内"
        );
        let mut bytes = Vec::new();
        File::open(path)?
            .take(MAX_PACKET + 1)
            .read_to_end(&mut bytes)?;
        ensure!(bytes.len() as u64 <= MAX_PACKET, "记录中的 PDX 包超过上限");
        ensure!(
            self.size.is_none_or(|size| bytes.len() as u64 == size),
            "包长度与记录不一致"
        );
        ensure!(
            self.digest.is_empty()
                || self
                    .digest
                    .eq_ignore_ascii_case(&format!("{:x}", Sha256::digest(&bytes))),
            "包 SHA-256 与记录不一致"
        );
        Ok(bytes)
    }
}

fn control_type(bytes: &[u8]) -> Result<u8> {
    let header = bytes.get(..5).context("会话控制头被截断")?;
    let length = u32::from_le_bytes(header[1..5].try_into().unwrap());
    ensure!(length as usize == bytes.len(), "会话控制长度与记录不一致");
    Ok(header[0])
}

impl Replay {
    pub fn save_database(&self, snapshot: &Path, destination: &Path) -> Result<()> {
        let file = self
            .files
            .iter()
            .find(|file| file.snapshot && file.path == snapshot)
            .context("请先选择已校验的热加入快照")?;
        let bytes = fs::read(snapshot)?;
        ensure!(
            bytes.len() == file.size as usize
                && super::transfer::Checksum::of(&bytes) == file.checksum,
            "回放快照已变化，请重新读取抓包日志"
        );
        let world = World::from_bytes(bytes)?;
        let commands = self
            .records
            .iter()
            .filter(|record| {
                record.channel == 0
                    && record.packet.room == file.room
                    && record.name != "命令解析失败"
            })
            .map(Record::observation);
        WorldStore::index(destination, &world, snapshot, None, commands)?;
        Ok(())
    }

    pub fn open(log: &Path, destination: &Path) -> Result<Self> {
        let log = fs::canonicalize(log).context("打开抓包事件日志")?;
        let root: Arc<Path> = log.parent().context("抓包日志缺少目录")?.into();
        let mut reader = BufReader::new(File::open(log)?);
        fs::create_dir_all(destination)?;
        let output = tempfile::Builder::new()
            .prefix("replay-")
            .tempdir_in(destination)?;
        let mut replay = Self {
            records: Vec::new(),
            files: Vec::new(),
            summary: Summary::default(),
            errors: VecDeque::new(),
            output: output.path().to_path_buf(),
        };
        let mut transfers = Transfers::default();
        let mut line = Vec::new();
        let mut number = 0;
        loop {
            line.clear();
            if (&mut reader)
                .take(MAX_LOG_LINE + 1)
                .read_until(b'\n', &mut line)?
                == 0
            {
                break;
            }
            number += 1;
            ensure!(
                line.len() as u64 <= MAX_LOG_LINE,
                "抓包日志第 {number} 行过长"
            );
            let bytes = if number == 1 {
                line.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&line)
            } else {
                &line
            };
            if bytes.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            let event: Value =
                serde_json::from_slice(bytes).with_context(|| format!("抓包日志第 {number} 行"))?;
            if event["opcode"] != GAME_DATA_OPCODE || event["routing_ignored"] == true {
                continue;
            }
            let direction = match event["event"].as_str() {
                Some("match_data") => "rx",
                Some("send_submitted") => "tx",
                _ => continue,
            };
            if direction == "rx" {
                replay.summary.frames += 1;
            }
            if let Err(error) =
                replay.packet(root.clone(), &event, direction, number, &mut transfers)
            {
                replay.record_error(number, error);
            }
        }
        replay.summary.incomplete_transfers = transfers.active.len();
        write_json(&replay.output.join("summary.json"), &replay.summary)?;
        replay.output = output.keep();
        Ok(replay)
    }

    fn packet(
        &mut self,
        root: Arc<Path>,
        event: &Value,
        direction: &'static str,
        line: usize,
        transfers: &mut Transfers,
    ) -> Result<()> {
        let packet = Arc::new(Packet::from_event(root, event, direction)?);
        let bytes = packet.read()?;
        let frame = protocol::decode(&bytes)?;
        let source = event["source"]["user_id"]
            .as_str()
            .or_else(|| event["presence"]["user_id"].as_str())
            .unwrap_or(frame.source_user);
        let recipient = frame.recipient;
        for (message, item) in frame.messages.iter().enumerate() {
            let label = match item.word_8 {
                0 => match Incoming::decode(item.payload) {
                    Ok(command) => {
                        if direction == "rx" {
                            *self
                                .summary
                                .commands
                                .entry(command.name().into())
                                .or_default() += 1;
                            if let Err(error) = self.synchronize(&command).and_then(|()| {
                                transfers
                                    .ingest(source, &command, &self.output, (&packet, message))
                                    .map(|file| {
                                        if let Some(file) = file {
                                            self.files.push(file);
                                            self.summary.verified_files += 1;
                                        }
                                    })
                            }) {
                                self.record_error(line, error);
                            }
                        }
                        command.name().into()
                    }
                    Err(error) => {
                        self.record_error(line, error);
                        "命令解析失败".into()
                    }
                },
                1 => match control_type(item.payload) {
                    Ok(kind) => {
                        if direction == "rx" {
                            self.summary.control_messages += 1;
                        }
                        format!("会话控制 {kind}")
                    }
                    Err(error) => {
                        self.record_error(line, error);
                        "会话控制解析失败".into()
                    }
                },
                channel => format!("通道 {channel}"),
            };
            self.records.push(Record {
                name: label,
                sequence: frame.sequence,
                channel: item.word_8,
                recipient: recipient.into(),
                source: source.into(),
                line,
                packet: packet.clone(),
                message,
            });
        }
        Ok(())
    }

    fn record_error(&mut self, line: usize, error: anyhow::Error) {
        self.summary.decode_errors += 1;
        if self.errors.len() == 64 {
            self.errors.pop_front();
        }
        self.errors.push_back(format!("第 {line} 行 · {error:#}"));
    }

    fn synchronize(&mut self, command: &Incoming<'_>) -> Result<()> {
        let current = &mut self.summary.synchronization;
        match command.name() {
            "hotjoin_sync_command" => {
                let data = command.get("data")?;
                let tick = data["tick"].as_u64().context("热加入缺少同步刻")?;
                ensure!(
                    u32::try_from(tick).is_ok()
                        && data["pause_game"].is_boolean()
                        && data["host_has_started_game"].is_boolean(),
                    "热加入同步字段无效"
                );
                let hotjoin = json!({"sender": command.sender()?, "data": data});
                if current.hotjoin.as_ref() != Some(&hotjoin) {
                    *current = Synchronization {
                        hotjoin: Some(hotjoin),
                        ..Default::default()
                    };
                }
            }
            "client_ping" => {
                current
                    .peer_turns
                    .insert(command.number("machineid")?, command.number("turn")?);
            }
            "daily_tick" => current.daily_ticks += u64::from(current.hotjoin.is_some()),
            "event_tick" => current.event_ticks += u64::from(current.hotjoin.is_some()),
            _ => {}
        }
        Ok(())
    }
}

#[derive(PartialEq, Eq, Hash)]
struct TransferKey {
    room: String,
    source: String,
    info: TransferInfo,
}

#[derive(Default)]
struct Transfers {
    active: HashMap<TransferKey, PendingTransfer>,
    finished: HashSet<TransferKey>,
}

struct PendingTransfer {
    transfer: Transfer,
    early_fast_chunks: Vec<(Arc<Packet>, usize)>,
}

impl Transfers {
    fn ingest(
        &mut self,
        source: &str,
        command: &Incoming<'_>,
        directory: &Path,
        (packet, message): (&Arc<Packet>, usize),
    ) -> Result<Option<Reassembled>> {
        if !matches!(
            command.kind,
            ANNOUNCE | FAST_HANDSHAKE | SERIAL_METADATA | FAST_CHUNK | SERIAL_CHUNK
        ) {
            return Ok(None);
        }
        let info = command.info()?;
        let key = TransferKey {
            room: packet.room.clone(),
            source: source.into(),
            info: info.clone(),
        };
        if self.finished.contains(&key) {
            return Ok(None);
        }
        let pending = self.active.entry(key).or_insert_with(|| PendingTransfer {
            transfer: Transfer::announced(info.clone(), 0),
            early_fast_chunks: Vec::new(),
        });
        let transfer = &mut pending.transfer;
        match command.kind {
            FAST_HANDSHAKE => {
                transfer.initialize(
                    directory,
                    command.number("filetransfer_fast_fld_chunk_size")?,
                )?;
            }
            SERIAL_METADATA => {
                transfer.initialize_serial(directory, command)?;
            }
            FAST_CHUNK => {
                let index = command.number("chunk_id")?;
                let data = command.data()?;
                if transfer.progress().is_none() {
                    if index != 0 {
                        pending.early_fast_chunks.push((packet.clone(), message));
                        return Ok(None);
                    }
                    // A verified chunk zero gives the size even in captures with no handshake.
                    transfer.initialize(directory, u32::try_from(data.len())?)?;
                }
                transfer.chunk(index, data)?;
            }
            SERIAL_CHUNK => transfer.chunk(command.number("chunk_id")?, command.data()?)?,
            _ => {}
        }
        if transfer.progress().is_some() {
            while let Some((packet, message)) = pending.early_fast_chunks.pop() {
                let bytes = packet.read()?;
                let frame = protocol::decode(&bytes)?;
                let item = frame
                    .messages
                    .get(message)
                    .context("快照恢复时找不到原始消息")?;
                let command = Incoming::decode(item.payload)?;
                transfer.chunk(command.number("chunk_id")?, command.data()?)?;
            }
        }
        let Some(file) = transfer.take_complete() else {
            return Ok(None);
        };
        let key = TransferKey {
            room: packet.room.clone(),
            source: source.into(),
            info: info.clone(),
        };
        self.active.remove(&key);
        let path = verify(file, directory, &info)?;
        self.finished.insert(key);
        Ok(Some(Reassembled {
            snapshot: info.filename.starts_with("hotjoin_"),
            name: info.filename,
            path,
            size: info.size,
            checksum: info.checksum,
            room: packet.room.clone(),
        }))
    }
}
