mod feedback;
mod game;
pub(crate) mod game_protocol;
pub mod native;
pub mod replay;
mod transfer;

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use reqwest::{Url, blocking::Client};
use serde::Deserialize;

use self::game::Game;
use self::game_protocol::Compatibility;
use crate::protocol::{self, Envelope, GAME_DATA_OPCODE};
use crate::settings::{ConnectionSettings, read_json};
pub use feedback::{ActionReceipt, ActionRequest, ActionStatus};
use native::{Event, NakamaSdk, WireFormat};

pub const CK3_BUILD: &str = "1.20.1.2";
pub(crate) const EXE_SHA256: &str =
    "52c4678588c11ef6dd49c815198bc0108134d69699a30b90bc66f080268e8110";
const PHASE_TIMEOUT: Duration = Duration::from_secs(15);
const TICK_DELAY: Duration = Duration::from_millis(20);
const REPORT_DELAY: Duration = Duration::from_millis(200);

#[derive(Deserialize)]
struct Profile {
    exe_sha256: String,
    host: String,
    port: u16,
    ssl: bool,
    match_id: String,
    host_user_id: String,
    handshake: serde_json::Map<String, serde_json::Value>,
}

impl Profile {
    fn load(path: &Path) -> Result<Self> {
        let profile: Self = read_json(path)?;
        ensure!(
            profile.exe_sha256.eq_ignore_ascii_case(EXE_SHA256),
            "房间配置需要 CK3 {CK3_BUILD}"
        );
        ensure!(profile.port != 0, "房间配置的端口无效");
        for (name, value) in [
            ("host", &profile.host),
            ("match_id", &profile.match_id),
            ("host_user_id", &profile.host_user_id),
        ] {
            ensure!(
                !value.trim().is_empty() && !value.contains('\0'),
                "房间配置的 {name} 无效"
            );
        }
        ensure!(!profile.handshake.is_empty(), "房间配置缺少握手信息");
        ensure!(
            !profile.host.contains(['/', '\\', '?', '#', '@'])
                && !profile.host.chars().any(char::is_whitespace),
            "房间主机地址无效"
        );
        Ok(profile)
    }

    fn auth_url(&self) -> Result<Url> {
        let base = if self.ssl {
            "https://localhost"
        } else {
            "http://localhost"
        };
        let mut url = Url::parse(base)?;
        url.set_host(Some(&self.host)).context("房间主机地址无效")?;
        url.set_port(Some(self.port))
            .map_err(|_| anyhow!("房间端口无效"))?;
        url.set_path("/v2/account/authenticate/custom");
        Ok(url)
    }
}

#[derive(Deserialize)]
struct TokenResponse {
    token: String,
}

#[derive(Deserialize)]
struct TokenClaims {
    uid: String,
    exp: u64,
}

fn validate_token(token: &str) -> Result<()> {
    let parts: Vec<_> = token.split('.').collect();
    ensure!(
        parts.len() == 3
            && parts.iter().all(|part| {
                !part.is_empty()
                    && part
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
            }),
        "会话令牌的 JWT 格式无效"
    );
    let claims = URL_SAFE_NO_PAD
        .decode(parts[1])
        .context("JWT 数据编码无效")?;
    let claims: TokenClaims = serde_json::from_slice(&claims).context("JWT 字段无效")?;
    ensure!(
        !claims.uid.is_empty() && !claims.uid.contains('\0'),
        "JWT 缺少有效的 uid"
    );
    ensure!(
        claims.exp < (i64::MAX as u64) / 1000,
        "JWT 过期时间超出范围"
    );
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    ensure!(claims.exp > now, "会话令牌已过期");
    Ok(())
}

fn authenticate(profile: &Profile, options: &ConnectionSettings) -> Result<String> {
    let identity = options.identity.trim();
    let username = options.username.trim();
    ensure!(
        !identity.is_empty() && !username.is_empty(),
        "请填写玩家名称和会话身份"
    );
    let http = Client::builder().timeout(Duration::from_secs(10)).build()?;
    let response = http
        .post(profile.auth_url()?)
        .basic_auth("ck3-local", Some(""))
        .query(&[("username", username)])
        .json(&serde_json::json!({"id": identity}))
        .send()
        .map_err(reqwest::Error::without_url)
        .context("Nakama 登录请求失败")?;
    let status = response.status();
    ensure!(
        status.is_success(),
        "Nakama 登录返回 HTTP {}",
        status.as_u16()
    );
    let payload: TokenResponse = response
        .json()
        .map_err(reqwest::Error::without_url)
        .context("Nakama 登录回复缺少会话令牌")?;
    Ok(payload.token)
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Traffic {
    pub packets: u64,
    pub bytes: u64,
    pub pdx_frames: u64,
    pub pdx_messages: u64,
    pub invalid_frames: u64,
    pub last_sequence: Option<u64>,
}

impl Traffic {
    fn record<'a>(&mut self, opcode: i64, payload: &'a [u8]) -> Option<Envelope<'a>> {
        self.packets += 1;
        self.bytes += payload.len() as u64;
        if opcode != GAME_DATA_OPCODE {
            return None;
        }
        match protocol::decode(payload) {
            Ok(frame) => {
                self.pdx_frames += 1;
                self.pdx_messages += frame.messages.len() as u64;
                self.last_sequence = Some(frame.sequence);
                Some(frame)
            }
            Err(_) => {
                self.invalid_frames += 1;
                None
            }
        }
    }
}

pub enum Update {
    Authenticated,
    Joining,
    Joined {
        match_id: String,
        user_id: String,
    },
    GameHandshake(u32),
    GameConnected {
        machine: i32,
    },
    GameTransfer {
        received: u32,
        total: u32,
    },
    GameSnapshot {
        path: std::path::PathBuf,
        machine: i32,
    },
    ActionSubmitted(String),
    ActionFeedback(ActionReceipt),
    Traffic(Traffic),
    Notice(String),
    Ended(Result<()>),
}

pub struct SessionTask {
    commands: Sender<Input>,
    updates: Receiver<Update>,
    worker: Option<JoinHandle<()>>,
    active: Arc<AtomicBool>,
}

#[derive(Clone)]
pub struct ActionClient {
    commands: Sender<Input>,
    active: Arc<AtomicBool>,
}

impl ActionClient {
    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    pub fn same_session(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.active, &other.active)
    }

    pub fn submit(
        &self,
        request: ActionRequest,
        stop: Arc<AtomicBool>,
    ) -> Result<Receiver<Result<ActionReceipt>>> {
        let (reply, response) = mpsc::channel();
        self.commands
            .send(Input::Command {
                request,
                reply: Some(reply),
                stop: Some(stop),
            })
            .context("房间已断开")?;
        Ok(response)
    }

    pub fn status(&self, request_id: String) -> Result<Receiver<Result<ActionReceipt>>> {
        let (reply, response) = mpsc::channel();
        self.commands
            .send(Input::ActionStatus { request_id, reply })
            .context("房间已断开")?;
        Ok(response)
    }

    pub(crate) fn replay_interaction(
        &self,
        request: ActionRequest,
    ) -> Result<Receiver<Result<Option<ActionReceipt>>>> {
        let (reply, response) = mpsc::channel();
        self.commands
            .send(Input::ReplayInteraction { request, reply })
            .context("房间已断开")?;
        Ok(response)
    }
}

enum Input {
    Stop,
    Select(u32),
    Ready(bool),
    Command {
        request: ActionRequest,
        reply: Option<Sender<Result<ActionReceipt>>>,
        stop: Option<Arc<AtomicBool>>,
    },
    ActionStatus {
        request_id: String,
        reply: Sender<Result<ActionReceipt>>,
    },
    ReplayInteraction {
        request: ActionRequest,
        reply: Sender<Result<Option<ActionReceipt>>>,
    },
}

impl SessionTask {
    pub fn start(options: ConnectionSettings) -> Result<Self> {
        let (commands, control) = mpsc::channel();
        let (messages, updates) = mpsc::channel();
        let active = Arc::new(AtomicBool::new(true));
        let worker_active = active.clone();
        let worker = thread::Builder::new()
            .name("nakama-session".into())
            .spawn(move || {
                let result = run(options, &control, &messages);
                worker_active.store(false, Ordering::Release);
                let _ = messages.send(Update::Ended(result));
            })
            .context("启动联机线程")?;
        Ok(Self {
            commands,
            updates,
            worker: Some(worker),
            active,
        })
    }

    pub fn try_recv(&self) -> Result<Update, TryRecvError> {
        self.updates.try_recv()
    }

    pub fn stop(&self) {
        self.active.store(false, Ordering::Release);
        let _ = self.commands.send(Input::Stop);
    }

    pub fn select_character(&self, character: u32) -> Result<()> {
        self.commands
            .send(Input::Select(character))
            .context("房间已断开")
    }

    pub fn set_ready(&self, ready: bool) -> Result<()> {
        self.commands
            .send(Input::Ready(ready))
            .context("房间已断开")
    }

    pub fn submit_command(
        &self,
        name: String,
        fields: serde_json::Map<String, serde_json::Value>,
        attribs: u8,
    ) -> Result<()> {
        self.commands
            .send(Input::Command {
                request: ActionRequest {
                    request_id: uuid::Uuid::new_v4().to_string(),
                    name,
                    fields,
                    attribs,
                },
                reply: None,
                stop: None,
            })
            .context("房间已断开")
    }

    pub fn actions(&self) -> ActionClient {
        ActionClient {
            commands: self.commands.clone(),
            active: self.active.clone(),
        }
    }
}

impl Drop for SessionTask {
    fn drop(&mut self) {
        self.stop();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn run(
    options: ConnectionSettings,
    commands: &Receiver<Input>,
    updates: &Sender<Update>,
) -> Result<()> {
    ensure!(
        !options.profile_path.trim().is_empty(),
        "请选择房间配置文件"
    );
    let profile = Profile::load(Path::new(options.profile_path.trim()))?;
    let username = options.username.trim().to_owned();
    let handshake = if options.join_game {
        let compatibility: Compatibility =
            serde_json::from_value(serde_json::Value::Object(profile.handshake.clone()))
                .context("房间配置的游戏握手数据无效")?;
        Some(compatibility.handshake(&username)?)
    } else {
        None
    };
    let sdk = NakamaSdk::load(Path::new(options.game_dir.trim()))?;
    let token = authenticate(&profile, &options)?;
    if !matches!(commands.try_recv(), Err(TryRecvError::Empty)) {
        return Ok(());
    }
    updates.send(Update::Authenticated)?;
    let mut client = sdk.client(&profile.host, profile.port, profile.ssl, &token)?;
    client.connect(WireFormat::Protobuf)?;

    let mut joined = false;
    let mut deadline = Instant::now() + PHASE_TIMEOUT;
    let mut last_report = Instant::now();
    let mut traffic = Traffic::default();
    let mut reported = Traffic::default();
    let mut game: Option<Game> = None;
    let mut leaving = None;
    let result = (|| {
        loop {
            for event in client.poll() {
                match event {
                    Event::Connected => {
                        client.join(&profile.match_id)?;
                        deadline = Instant::now() + PHASE_TIMEOUT;
                        updates.send(Update::Joining)?;
                    }
                    Event::Joined {
                        match_id,
                        own,
                        peers,
                        ..
                    } if match_id == profile.match_id => {
                        joined = true;
                        updates.send(Update::Joined {
                            match_id: match_id.clone(),
                            user_id: own.user_id.clone(),
                        })?;
                        if let Some(handshake) = &handshake {
                            let host = peers
                                .into_iter()
                                .find(|peer| peer.user_id == profile.host_user_id)
                                .context("房主当前不在房间中")?;
                            game = Some(Game::connect(
                                &client,
                                match_id,
                                username.clone(),
                                handshake,
                                host,
                                own,
                                options.transfer,
                            )?);
                            deadline = Instant::now() + PHASE_TIMEOUT;
                        }
                    }
                    Event::Data {
                        match_id,
                        opcode,
                        payload,
                        presence,
                    } if match_id == profile.match_id => {
                        let frame = traffic.record(opcode, &payload);
                        if let (Some(game), Some(frame)) = (&mut game, frame) {
                            game.receive(&client, &presence, frame, updates)?;
                        }
                    }
                    Event::JoinError { code, message } => bail!("加入房间失败（{code}）{message}"),
                    Event::Error { code, message } => {
                        let detail = format!("Nakama 错误（{code}）{message}");
                        if !joined {
                            bail!(detail);
                        }
                        updates.send(Update::Notice(detail))?;
                    }
                    Event::Disconnected { code, reason, .. } => {
                        if leaving.is_some() {
                            return Ok(());
                        }
                        bail!("Nakama 已断开（{code}）{reason}")
                    }
                    _ => {}
                }
            }
            ensure!(
                joined || Instant::now() < deadline,
                "等待 Nakama 连接或入房超时"
            );
            if let Some(game) = &mut game {
                if game.closed() {
                    break;
                }
                if leaving.is_none() {
                    ensure!(
                        game.connected() || Instant::now() < deadline,
                        "等待 CK3 游戏握手超时"
                    );
                    game.tick(&client, updates)?;
                }
            }
            if leaving.is_some_and(|until| Instant::now() >= until) {
                break;
            }
            if last_report.elapsed() >= REPORT_DELAY && traffic != reported {
                updates.send(Update::Traffic(traffic.clone()))?;
                reported = traffic.clone();
                last_report = Instant::now();
            }
            match commands.recv_timeout(TICK_DELAY) {
                Ok(Input::Stop) | Err(RecvTimeoutError::Disconnected) if leaving.is_none() => {
                    if let Some(game) = &mut game
                        && game.request_disconnect(&client)?
                    {
                        leaving = Some(Instant::now() + Duration::from_secs(3));
                    } else {
                        break;
                    }
                }
                Ok(Input::Select(character)) if leaving.is_none() => {
                    let game = game.as_mut().context("尚未进入游戏会话")?;
                    match game.select(&client, character) {
                        Ok(()) => updates
                            .send(Update::ActionSubmitted(format!("已提交选角 {character}")))?,
                        Err(error) => updates.send(Update::Notice(format!("{error:#}")))?,
                    }
                }
                Ok(Input::Ready(ready)) if leaving.is_none() => {
                    let game = game.as_mut().context("尚未进入游戏会话")?;
                    match game.ready(&client, ready) {
                        Ok(()) => updates.send(Update::ActionSubmitted(if ready {
                            "已提交准备".into()
                        } else {
                            "已提交取消准备".into()
                        }))?,
                        Err(error) => updates.send(Update::Notice(format!("{error:#}")))?,
                    }
                }
                Ok(Input::Command {
                    request,
                    reply,
                    stop,
                }) => {
                    let result = if leaving.is_some()
                        || stop.is_some_and(|stop| stop.load(Ordering::Acquire))
                    {
                        Err(anyhow!("操作已取消"))
                    } else {
                        game.as_mut()
                            .context("尚未进入游戏会话")
                            .and_then(|game| game.submit(&client, request))
                    };
                    if let Ok(receipt) = &result {
                        updates.send(Update::ActionFeedback(receipt.clone()))?;
                    }
                    if let Some(reply) = reply {
                        let _ = reply.send(result);
                    } else if let Err(error) = result {
                        updates.send(Update::Notice(format!("{error:#}")))?;
                    }
                }
                Ok(Input::ActionStatus { request_id, reply }) => {
                    let result = game
                        .as_ref()
                        .context("尚未进入游戏会话")
                        .and_then(|game| game.action_status(&request_id));
                    let _ = reply.send(result);
                }
                Ok(Input::ReplayInteraction { request, reply }) => {
                    let result = game
                        .as_ref()
                        .context("尚未进入游戏会话")
                        .and_then(|game| game.replay_interaction(&request));
                    let _ = reply.send(result);
                }
                _ => {}
            }
        }
        if traffic != reported {
            updates.send(Update::Traffic(traffic))?;
        }
        Ok(())
    })();
    if result.is_err()
        && leaving.is_none()
        && let Some(game) = &mut game
    {
        let _ = game.request_disconnect(&client);
    }
    if let Some(game) = &mut game {
        for receipt in game.disconnected() {
            let _ = updates.send(Update::ActionFeedback(receipt));
        }
    }
    result
}
