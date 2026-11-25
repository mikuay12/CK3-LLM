use std::fs;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use tempfile::NamedTempFile;
use uuid::Uuid;

use crate::protocol::{Envelope, GAME_DATA_OPCODE, Magic, Message};
use crate::settings::Settings;
use crate::world::World;

use super::Update;
use super::game_protocol::{self, Control, Incoming, TransferInfo, Writer};
use super::native::{NativeClient, Presence};

#[cfg(test)]
mod test_peer;

const ANNOUNCE: u16 = 0x6a0;
const REQUEST_TRANSFER: u16 = 0x618;
const TRANSFER_HANDSHAKE: u16 = 0x619;
const INITIALIZE: u16 = 0x61a;
const CHUNK: u16 = 0x61b;
const BITMAP: u16 = 0x61c;
const ADD_PLAYER: u16 = 0x69f;
const PLAYER_HERE: u16 = 0x2a9;
const READY: u16 = 0x6a6;
const SELECT_CHARACTER: u16 = 0x6de;
const PING: u16 = 0x2788;
const TRANSFER_ATTRIBS: u8 = 47;
const REPORT_PERIOD: Duration = Duration::from_millis(300);

pub struct Game {
    room: String,
    player: String,
    host: Presence,
    own: Presence,
    directory: PathBuf,
    sequence: u64,
    identity: u32,
    machine: Option<i32>,
    connected: bool,
    ready: bool,
    closed: bool,
    current_transfer: Option<Transfer>,
    preparing: Option<Receiver<Result<PathBuf>>>,
    pending_turn: Option<u32>,
    last_turn: Option<u32>,
}

impl Game {
    pub fn connect(
        client: &NativeClient<'_>,
        room: String,
        player: String,
        handshake: &[u8],
        host: Presence,
        own: Presence,
    ) -> Result<Self> {
        let directory = Settings::path()?
            .with_file_name("captures")
            .join(format!("client-{}", Uuid::new_v4()));
        fs::create_dir_all(&directory)?;
        let mut game = Self {
            room,
            player,
            host,
            own,
            directory,
            sequence: 0,
            identity: 0,
            machine: None,
            connected: false,
            ready: false,
            closed: false,
            current_transfer: None,
            preparing: None,
            pending_turn: None,
            last_turn: None,
        };
        game.send(client, 1, handshake)?;
        Ok(game)
    }

    pub fn connected(&self) -> bool {
        self.connected
    }
    pub fn closed(&self) -> bool {
        self.closed
    }

    pub fn receive(
        &mut self,
        client: &NativeClient<'_>,
        sender: &Presence,
        frame: Envelope<'_>,
        updates: &Sender<Update>,
    ) -> Result<()> {
        if sender.user_id != self.host.user_id
            || sender.session_id != self.host.session_id
            || (!frame.source_user.is_empty() && frame.source_user != self.host.user_id)
            || (!frame.recipient.is_empty() && frame.recipient != self.own.user_id)
        {
            return Ok(());
        }
        for item in frame.messages {
            match item.word_8 {
                1 => self.receive_control(client, Control::decode(item.payload)?, updates)?,
                0 => {
                    let Some(raw) = item.payload.get(..2) else {
                        continue;
                    };
                    let kind = u16::from_le_bytes(raw.try_into().unwrap());
                    if matches!(kind, ANNOUNCE | TRANSFER_HANDSHAKE | CHUNK | PING) {
                        self.receive_command(client, Incoming::decode(item.payload)?)?;
                    }
                }
                _ => {}
            }
            if self.closed {
                break;
            }
        }
        Ok(())
    }

    fn receive_control(
        &mut self,
        client: &NativeClient<'_>,
        control: Control,
        updates: &Sender<Update>,
    ) -> Result<()> {
        match control {
            Control::Password => bail!("该房间需要游戏密码"),
            Control::AddPeer { id, local } => {
                self.send(client, 1, &game_protocol::control(5, 16))?;
                if local && self.machine != Some(id) {
                    self.machine = Some(id);
                    self.send(client, 1, &game_protocol::control(2, 16))?;
                }
            }
            Control::Connected if !self.connected => {
                let machine = self.machine.context("房主未分配玩家编号")?;
                let mut body = Writer::default();
                body.text(0x1b, &self.player)?;
                body.signed(0x16, machine);
                body.boolean(0x50f, false);
                self.command(client, ADD_PLAYER, 0, 14, body)?;
                self.connected = true;
                updates.send(Update::GameConnected { machine })?;
            }
            Control::Handshake(code) => updates.send(Update::GameHandshake(code))?,
            Control::RemovePeer(id) if self.machine == Some(id) => self.closed = true,
            Control::HostDisconnect(reason) => {
                self.send(client, 1, &game_protocol::disconnect(6, reason))?;
                self.closed = true;
            }
            Control::DisconnectAck => self.closed = true,
            _ => {}
        }
        Ok(())
    }

    fn receive_command(&mut self, client: &NativeClient<'_>, command: Incoming<'_>) -> Result<()> {
        if command.kind == PING {
            if self.connected && command.sender()? == 0 {
                let turn = command.number("turn")?;
                if self
                    .pending_turn
                    .or(self.last_turn)
                    .is_none_or(|previous| matches!(turn.wrapping_sub(previous), 1..=0x7fff_ffff))
                {
                    self.pending_turn = Some(turn);
                }
            }
            return Ok(());
        }
        if !self.connected {
            return Ok(());
        }
        let info = command.info()?;
        if !info.filename.starts_with("hotjoin_") {
            return Ok(());
        }
        match command.kind {
            ANNOUNCE
                if !self.ready
                    && self.preparing.is_none()
                    && self
                        .current_transfer
                        .as_ref()
                        .is_none_or(|transfer| transfer.info != info) =>
            {
                ensure!(
                    info.size > 0 && info.size <= (1 << 30),
                    "热加入快照长度无效"
                );
                self.current_transfer = Some(Transfer::announced(info.clone(), command.sender()?));
                self.transfer_command(client, REQUEST_TRANSFER, command.sender()?, &info, None)?;
            }
            TRANSFER_HANDSHAKE => {
                let Some(transfer) = self
                    .current_transfer
                    .as_mut()
                    .filter(|transfer| transfer.info == info)
                else {
                    return Ok(());
                };
                if transfer.initialize(
                    &self.directory,
                    command.number("filetransfer_fast_fld_chunk_size")?,
                )? {
                    let peer = transfer.peer;
                    self.transfer_command(client, INITIALIZE, peer, &info, None)?;
                }
            }
            CHUNK => {
                let Some(transfer) = self
                    .current_transfer
                    .as_mut()
                    .filter(|transfer| transfer.info == info)
                else {
                    return Ok(());
                };
                transfer.chunk(command.number("chunk_id")?, command.data()?)?;
            }
            _ => {}
        }
        Ok(())
    }

    pub fn tick(&mut self, client: &NativeClient<'_>, updates: &Sender<Update>) -> Result<()> {
        if let Some(transfer) = self.current_transfer.as_mut()
            && let Some(status) = transfer.status()
        {
            self.transfer_command(
                client,
                BITMAP,
                status.peer,
                &status.info,
                Some(&status.bitmap),
            )?;
            updates.send(Update::GameTransfer {
                received: status.received,
                total: status.total,
            })?;
            if let Some(file) = status.file {
                let folder = self.directory.clone();
                let (send, result) = mpsc::channel();
                thread::Builder::new()
                    .name("verify-hotjoin".into())
                    .spawn(move || {
                        let _ = send.send(verify(file, &folder, &status.info));
                    })?;
                self.preparing = Some(result);
            }
        }
        if let Some(result) = &self.preparing {
            match result.try_recv() {
                Ok(path) => {
                    let path = path?;
                    self.preparing = None;
                    let machine = self.machine.context("本地玩家编号缺失")?;
                    let mut body = Writer::default();
                    body.signed(0x16, machine);
                    self.command(client, PLAYER_HERE, 0, 14, body)?;
                    self.ready = true;
                    updates.send(Update::GameSnapshot { path, machine })?;
                }
                Err(TryRecvError::Empty) => {}
                Err(TryRecvError::Disconnected) => bail!("验证热加入快照的线程已退出"),
            }
        }
        if self.ready
            && let Some(turn) = self.pending_turn.take()
        {
            let mut body = Writer::default();
            body.unsigned(0x278a, turn);
            body.unsigned(0x16, self.machine.context("本地玩家编号缺失")? as u32);
            self.command(client, PING, 0, 41, body)?;
            self.last_turn = Some(turn);
        }
        Ok(())
    }

    pub fn select(&mut self, client: &NativeClient<'_>, character: u32) -> Result<()> {
        ensure!(self.ready, "完成快照加载后才能选角");
        let mut body = Writer::default();
        body.signed(0x16, self.machine.context("本地玩家编号缺失")?);
        body.unsigned(0x6df, character);
        self.command(client, SELECT_CHARACTER, 0, 14, body)
    }

    pub fn ready(&mut self, client: &NativeClient<'_>, ready: bool) -> Result<()> {
        ensure!(self.ready, "完成快照加载后才能更改准备状态");
        let mut body = Writer::default();
        body.signed(0x16, self.machine.context("本地玩家编号缺失")?);
        body.boolean(0xd0, ready);
        self.command(client, READY, 0, 14, body)
    }

    pub fn request_disconnect(&mut self, client: &NativeClient<'_>) -> Result<bool> {
        if self.closed || self.machine.is_none() {
            return Ok(false);
        }
        self.send(client, 1, &game_protocol::disconnect(3, 0))?;
        Ok(true)
    }

    fn transfer_command(
        &mut self,
        client: &NativeClient<'_>,
        kind: u16,
        peer: i32,
        info: &TransferInfo,
        data: Option<&[u8]>,
    ) -> Result<()> {
        let mut body = Writer::default();
        info.encode(&mut body)?;
        if let Some(data) = data {
            body.data(data);
        }
        self.command(client, kind, peer, TRANSFER_ATTRIBS, body)
    }

    fn command(
        &mut self,
        client: &NativeClient<'_>,
        kind: u16,
        receiver: i32,
        attribs: u8,
        body: Writer,
    ) -> Result<()> {
        let machine = self.machine.context("本地玩家编号缺失")?;
        let payload = Writer::command(kind, self.identity, machine, receiver, attribs, body);
        self.send(client, 0, &payload)?;
        self.identity = self.identity.wrapping_add(1);
        Ok(())
    }

    fn send(&mut self, client: &NativeClient<'_>, channel: u32, payload: &[u8]) -> Result<()> {
        let packet = Envelope {
            magic: Magic::Directed,
            recipient: &self.host.user_id,
            sequence: self.sequence,
            source_user: &self.own.user_id,
            messages: vec![Message {
                word_4: 5,
                word_8: channel,
                payload,
            }],
        }
        .encode()?;
        client.send(
            &self.room,
            GAME_DATA_OPCODE,
            &packet,
            std::slice::from_ref(&self.host),
        )?;
        self.sequence = self.sequence.wrapping_add(1);
        Ok(())
    }
}

struct Transfer {
    info: TransferInfo,
    peer: i32,
    chunk_size: u32,
    count: u32,
    bitmap: Vec<u8>,
    received: u32,
    file: Option<NamedTempFile>,
    last: Instant,
}

struct TransferStatus {
    info: TransferInfo,
    peer: i32,
    bitmap: Vec<u8>,
    received: u32,
    total: u32,
    file: Option<NamedTempFile>,
}

impl Transfer {
    fn announced(info: TransferInfo, peer: i32) -> Self {
        Self {
            info,
            peer,
            chunk_size: 0,
            count: 0,
            bitmap: Vec::new(),
            received: 0,
            file: None,
            last: Instant::now(),
        }
    }

    fn initialize(&mut self, directory: &Path, chunk_size: u32) -> Result<bool> {
        ensure!(
            chunk_size > 0 && chunk_size <= (1 << 20),
            "快照分块大小无效"
        );
        if self.chunk_size != 0 {
            ensure!(self.chunk_size == chunk_size, "快照分块大小中途改变");
            return Ok(false);
        }
        let count = self.info.size.div_ceil(chunk_size);
        let length = count.div_ceil(8) as usize;
        ensure!(length <= (1 << 20) - 512, "快照分块位图过大");
        let file = NamedTempFile::new_in(directory)?;
        file.as_file().set_len(u64::from(self.info.size))?;
        self.chunk_size = chunk_size;
        self.count = count;
        self.bitmap.resize(length, 0);
        self.file = Some(file);
        self.last = Instant::now();
        Ok(true)
    }

    fn chunk(&mut self, chunk: u32, data: &[u8]) -> Result<()> {
        let Some(file) = &mut self.file else {
            return Ok(());
        };
        ensure!(chunk < self.count, "快照分块编号越界");
        let offset = chunk * self.chunk_size;
        let size = self.chunk_size.min(self.info.size - offset) as usize;
        ensure!(size == data.len(), "快照分块长度不匹配");
        file.as_file_mut()
            .seek(SeekFrom::Start(u64::from(offset)))?;
        let bitmap = &mut self.bitmap[(chunk / 8) as usize];
        let mask = 1 << (chunk % 8);
        if *bitmap & mask != 0 {
            let mut existing = vec![0; size];
            file.as_file_mut().read_exact(&mut existing)?;
            ensure!(existing == data, "房主重传的分块内容冲突");
        } else {
            file.as_file_mut().write_all(data)?;
            *bitmap |= mask;
            self.received += 1;
        }
        Ok(())
    }

    fn status(&mut self) -> Option<TransferStatus> {
        self.file.as_ref()?;
        let complete = self.received == self.count;
        if !complete && self.last.elapsed() < REPORT_PERIOD {
            return None;
        }
        self.last = Instant::now();
        let file = if complete { self.file.take() } else { None };
        Some(TransferStatus {
            info: self.info.clone(),
            peer: self.peer,
            bitmap: self.bitmap.clone(),
            received: self.received,
            total: self.count,
            file,
        })
    }
}

fn verify(file: NamedTempFile, directory: &Path, info: &TransferInfo) -> Result<PathBuf> {
    let mut handle = file.reopen()?;
    let mut disk = Checksum::default();
    let mut buffer = vec![0; 256 * 1024];
    loop {
        let amount = handle.read(&mut buffer)?;
        if amount == 0 {
            break;
        }
        disk.feed(&buffer[..amount]);
    }
    drop(handle);
    let world = World::open(file.path())?;
    if disk.value() != info.checksum {
        let mut entry = Checksum::default();
        entry.feed(world.binary());
        ensure!(entry.value() == info.checksum, "热加入快照校验失败");
    }
    world.summary()?;
    let snapshot = directory.join(format!("world-{}.bin", Uuid::new_v4()));
    file.persist(&snapshot)?;
    Ok(snapshot)
}

#[derive(Default)]
struct Checksum {
    first: u32,
    second: u32,
}

impl Checksum {
    fn feed(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.first += u32::from(byte);
            if self.first > 65_535 {
                self.first -= 65_535;
            }
            self.second += self.first;
            if self.second > 65_535 {
                self.second -= 65_535;
            }
        }
    }

    fn value(&self) -> i32 {
        ((self.first << 16) | self.second) as i32
    }
}
