use std::fs;
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use uuid::Uuid;

use crate::protocol::{Envelope, GAME_DATA_OPCODE, Magic, Message};
use crate::settings::{Settings, TransferMode};
#[cfg(test)]
use crate::world::World;

use super::feedback::Journal;
use super::game_protocol::{self, Control, Incoming, TransferInfo, Writer};
use super::native::{NativeClient, Presence};
#[cfg(test)]
use super::transfer::Checksum;
use super::transfer::{
    ANNOUNCE, FAST_CHUNK as CHUNK, FAST_HANDSHAKE as TRANSFER_HANDSHAKE, SERIAL_CHUNK,
    SERIAL_METADATA, Transfer, verify,
};
use super::{ActionReceipt, ActionRequest, Update};

#[cfg(test)]
mod test_peer;

const REQUEST_TRANSFER: u16 = 0x618;
const INITIALIZE: u16 = 0x61a;
const BITMAP: u16 = 0x61c;
const REQUEST_SERIAL_METADATA: u16 = 0x6a1;
const REQUEST_SERIAL_CHUNK: u16 = 0x6d6;
const ACK_SERIAL_CHUNK: u16 = 0x6d8;
const ADD_PLAYER: u16 = 0x69f;
const PLAYER_HERE: u16 = 0x2a9;
const READY: u16 = 0x6a6;
const SELECT_CHARACTER: u16 = 0x6de;
const PING: u16 = 0x2788;
const TRANSFER_ATTRIBS: u8 = 47;
const SERIAL_RETRY: Duration = Duration::from_secs(1);

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
    actions: Journal,
    transfer_mode: TransferMode,
    serial_requested: Option<Instant>,
}

impl Game {
    pub fn connect(
        client: &NativeClient<'_>,
        room: String,
        player: String,
        handshake: &[u8],
        host: Presence,
        own: Presence,
        transfer_mode: TransferMode,
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
            actions: Journal::default(),
            transfer_mode,
            serial_requested: None,
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
                    if matches!(
                        kind,
                        ANNOUNCE
                            | TRANSFER_HANDSHAKE
                            | CHUNK
                            | PING
                            | SERIAL_METADATA
                            | SERIAL_CHUNK
                    ) {
                        self.receive_command(client, Incoming::decode(item.payload)?)?;
                    }
                    if let Some(receipt) = self.actions.observe(kind, item.payload) {
                        updates.send(Update::ActionFeedback(receipt))?;
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
                self.serial_requested = None;
                if self.transfer_mode == TransferMode::Fast {
                    self.transfer_command(
                        client,
                        REQUEST_TRANSFER,
                        command.sender()?,
                        &info,
                        None,
                    )?;
                }
            }
            TRANSFER_HANDSHAKE if self.transfer_mode == TransferMode::Fast => {
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
            CHUNK if self.transfer_mode == TransferMode::Fast => {
                let Some(transfer) = self
                    .current_transfer
                    .as_mut()
                    .filter(|transfer| transfer.info == info)
                else {
                    return Ok(());
                };
                transfer.chunk(command.number("chunk_id")?, command.data()?)?;
            }
            SERIAL_METADATA if self.transfer_mode == TransferMode::Serial => {
                if let Some(transfer) = self
                    .current_transfer
                    .as_mut()
                    .filter(|transfer| transfer.info == info)
                    && transfer.initialize_serial(&self.directory, &command)?
                {
                    self.serial_requested = None;
                }
            }
            SERIAL_CHUNK if self.transfer_mode == TransferMode::Serial => {
                if let Some(transfer) = self
                    .current_transfer
                    .as_mut()
                    .filter(|transfer| transfer.info == info)
                    && transfer.progress().is_some()
                {
                    let chunk = command.number("chunk_id")?;
                    transfer.chunk(chunk, command.data()?)?;
                    let (received, total) =
                        transfer.progress().expect("serial transfer initialized");
                    let peer = transfer.peer;
                    self.serial_requested = None;
                    let mut body = Writer::default();
                    info.encode(&mut body)?;
                    body.signed(0x16, self.machine.context("本地玩家编号缺失")?);
                    body.signed(0x6ca, chunk as i32);
                    body.unsigned(0x6cb, received);
                    body.unsigned(0x6cc, total);
                    self.command(client, ACK_SERIAL_CHUNK, peer, TRANSFER_ATTRIBS, body)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub fn tick(&mut self, client: &NativeClient<'_>, updates: &Sender<Update>) -> Result<()> {
        for receipt in self.actions.poll() {
            updates.send(Update::ActionFeedback(receipt))?;
        }
        if self.transfer_mode == TransferMode::Serial && self.preparing.is_none() && !self.ready {
            self.request_serial(client)?;
        }
        if let Some(transfer) = self.current_transfer.as_mut()
            && let Some(status) = transfer.status()
        {
            if self.transfer_mode == TransferMode::Fast {
                self.transfer_command(
                    client,
                    BITMAP,
                    status.peer,
                    &status.info,
                    Some(&status.bitmap),
                )?;
            }
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

    fn request_serial(&mut self, client: &NativeClient<'_>) -> Result<()> {
        let Some(transfer) = &self.current_transfer else {
            return Ok(());
        };
        if self
            .serial_requested
            .is_some_and(|sent| sent.elapsed() < SERIAL_RETRY)
        {
            return Ok(());
        }
        let mut body = Writer::default();
        transfer.info.encode(&mut body)?;
        let token = if transfer.progress().is_none() {
            REQUEST_SERIAL_METADATA
        } else if let Some(chunk) = transfer.next_missing() {
            body.signed(0x6ca, chunk as i32);
            REQUEST_SERIAL_CHUNK
        } else {
            return Ok(());
        };
        self.command(client, token, transfer.peer, TRANSFER_ATTRIBS, body)?;
        self.serial_requested = Some(Instant::now());
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

    pub fn submit(
        &mut self,
        client: &NativeClient<'_>,
        request: ActionRequest,
    ) -> Result<ActionReceipt> {
        ensure!(self.ready, "完成快照加载后才能提交游戏命令");
        if let Some(receipt) = self.actions.replay(&request)? {
            return Ok(receipt);
        }
        let machine = self.machine.context("本地玩家编号缺失")?;
        let identity = self.identity;
        let packet = crate::commands::encode(
            &request.name,
            &request.fields,
            identity,
            machine,
            0,
            request.attribs,
        )?;
        self.actions.prepare(request, &Incoming::decode(&packet)?)?;
        let result = self.send(client, 0, &packet);
        self.identity = self.identity.wrapping_add(1);
        Ok(self.actions.sent(identity, result))
    }

    pub fn action_status(&self, request_id: &str) -> Result<ActionReceipt> {
        self.actions.status(request_id)
    }

    pub fn replay_interaction(&self, request: &ActionRequest) -> Result<Option<ActionReceipt>> {
        self.actions.replay_interaction(request)
    }

    pub fn disconnected(&mut self) -> Vec<ActionReceipt> {
        self.actions.disconnect()
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
