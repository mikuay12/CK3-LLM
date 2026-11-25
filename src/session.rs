pub mod native;

use std::path::Path;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use reqwest::{Url, blocking::Client};
use serde::Deserialize;

use crate::protocol::{self, GAME_DATA_OPCODE};
use crate::settings::{ConnectionSettings, read_json};
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
    fn record(&mut self, opcode: i64, payload: &[u8]) {
        self.packets += 1;
        self.bytes += payload.len() as u64;
        if opcode == GAME_DATA_OPCODE {
            match protocol::decode(payload) {
                Ok(frame) => {
                    self.pdx_frames += 1;
                    self.pdx_messages += frame.messages.len() as u64;
                    self.last_sequence = Some(frame.sequence);
                }
                Err(_) => self.invalid_frames += 1,
            }
        }
    }
}

pub enum Update {
    Authenticated,
    Joining,
    Joined { match_id: String, user_id: String },
    Traffic(Traffic),
    Notice(String),
    Ended(Result<()>),
}

pub struct SessionTask {
    stop: Sender<()>,
    updates: Receiver<Update>,
    worker: Option<JoinHandle<()>>,
}

impl SessionTask {
    pub fn start(options: ConnectionSettings) -> Result<Self> {
        let (stop, control) = mpsc::channel();
        let (messages, updates) = mpsc::channel();
        let worker = thread::Builder::new()
            .name("nakama-session".into())
            .spawn(move || {
                let result = run(options, &control, &messages);
                let _ = messages.send(Update::Ended(result));
            })
            .context("启动联机线程")?;
        Ok(Self {
            stop,
            updates,
            worker: Some(worker),
        })
    }

    pub fn try_recv(&self) -> Result<Update, TryRecvError> {
        self.updates.try_recv()
    }

    pub fn stop(&self) {
        let _ = self.stop.send(());
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

fn run(options: ConnectionSettings, stop: &Receiver<()>, updates: &Sender<Update>) -> Result<()> {
    ensure!(
        !options.profile_path.trim().is_empty(),
        "请选择房间配置文件"
    );
    let profile = Profile::load(Path::new(options.profile_path.trim()))?;
    let sdk = NakamaSdk::load(Path::new(options.game_dir.trim()))?;
    let token = authenticate(&profile, &options)?;
    if !matches!(stop.try_recv(), Err(TryRecvError::Empty)) {
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
    loop {
        for event in client.poll() {
            match event {
                Event::Connected => {
                    client.join(&profile.match_id)?;
                    deadline = Instant::now() + PHASE_TIMEOUT;
                    updates.send(Update::Joining)?;
                }
                Event::Joined { match_id, own, .. } if match_id == profile.match_id => {
                    joined = true;
                    updates.send(Update::Joined {
                        match_id,
                        user_id: own.user_id,
                    })?;
                }
                Event::Data {
                    match_id,
                    opcode,
                    payload,
                    ..
                } if match_id == profile.match_id => {
                    traffic.record(opcode, &payload);
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
                    bail!("Nakama 已断开（{code}）{reason}")
                }
                _ => {}
            }
        }
        ensure!(
            joined || Instant::now() < deadline,
            "等待 Nakama 连接或入房超时"
        );
        if last_report.elapsed() >= REPORT_DELAY && traffic != reported {
            updates.send(Update::Traffic(traffic.clone()))?;
            reported = traffic.clone();
            last_report = Instant::now();
        }
        if stop.recv_timeout(TICK_DELAY) != Err(RecvTimeoutError::Timeout) {
            if traffic != reported {
                updates.send(Update::Traffic(traffic))?;
            }
            return Ok(());
        }
    }
}
