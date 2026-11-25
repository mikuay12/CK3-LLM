use std::ffi::CString;
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail, ensure};
use frida::{Device, DeviceManager, Frida, Message, ScriptHandler, ScriptOption, SpawnOptions};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::HostUpdate;
use super::auth::SERVER_KEY;
use super::backend::LocalBackend;
use super::frontend::Frontend;
use crate::session::{CK3_BUILD, EXE_SHA256};
use crate::settings::{HostSettings, Settings, write_json};

const FRIDA_VERSION: &str = "17.26.2";
const ADAPTER: &str = include_str!("bridge.js");
static FRIDA: OnceLock<Frida> = OnceLock::new();
static HOST_SLOT: Mutex<()> = Mutex::new(());

pub fn run(
    game_dir: PathBuf,
    settings: HostSettings,
    stop: &Receiver<()>,
    updates: &Sender<HostUpdate>,
) -> Result<()> {
    let _slot = HOST_SLOT
        .try_lock()
        .map_err(|_| anyhow!("本地房主已经在运行"))?;
    let username = settings.username.trim();
    ensure!(
        !username.is_empty() && !username.contains('\0'),
        "请填写有效的房主名称"
    );
    let directory = std::path::absolute(&game_dir).context("检查 CK3 安装目录")?;
    let executable = directory.join("binaries/ck3.exe");
    verify_executable(&executable, stop)?;
    if stopped(stop, Duration::ZERO) {
        return Ok(());
    }

    let frida = FRIDA.get_or_init(|| unsafe { Frida::obtain() });
    ensure!(
        Frida::version() == FRIDA_VERSION,
        "Frida 版本不匹配，需要 {FRIDA_VERSION}"
    );
    let manager = DeviceManager::obtain(frida);
    let list = manager
        .get_local_device()?
        .enumerate_processes()
        .iter()
        .any(|process| {
            let name = process.get_name();
            name.eq_ignore_ascii_case("ck3.exe") || name.eq_ignore_ascii_case("ck3")
        });
    ensure!(!list, "已有 CK3 在运行，请在游戏中退出后再启动本地房主");

    let settings_file = Settings::path()?;
    let capture = settings_file
        .parent()
        .context("找不到数据存储目录")?
        .join("captures")
        .join(format!("local-game-{}", Uuid::new_v4()));
    let backend = LocalBackend::start(settings.port, Some(&capture))?;
    updates.send(HostUpdate::Listening {
        port: backend.port(),
        capture: capture.clone(),
    })?;
    let mut frontend = Frontend::apply(directory.join("game/gui/frontend_main.gui"), &capture)?;
    let result = run_game(
        &manager,
        &executable,
        username,
        &backend,
        &capture,
        stop,
        updates,
    );
    let restored = frontend.restore();
    if restored.is_ok() {
        backend.record("frontend_restored", json!({}));
    }
    match (result, restored) {
        (Ok(()), result) | (result, Ok(())) => result,
        (Err(launch), Err(restore)) => Err(launch.context(format!("{restore:#}"))),
    }
}

fn verify_executable(path: &Path, stop: &Receiver<()>) -> Result<()> {
    let mut file =
        BufReader::new(File::open(path).with_context(|| format!("找不到 {}", path.display()))?);
    let mut hash = Sha256::new();
    let mut block = vec![0; 256 * 1024];
    loop {
        if stopped(stop, Duration::ZERO) {
            bail!("已取消启动房主");
        }
        let size = file.read(&mut block)?;
        if size == 0 {
            break;
        }
        hash.update(&block[..size]);
    }
    ensure!(
        format!("{:x}", hash.finalize()) == EXE_SHA256,
        "本地房主需要已验证的 CK3 {CK3_BUILD}"
    );
    Ok(())
}

fn run_game(
    manager: &DeviceManager<'_>,
    executable: &Path,
    username: &str,
    backend: &LocalBackend,
    capture: &Path,
    stop: &Receiver<()>,
    updates: &Sender<HostUpdate>,
) -> Result<()> {
    if stopped(stop, Duration::ZERO) {
        return Ok(());
    }
    let game = executable.to_str().context("CK3 路径不能转为 UTF-8")?;
    let cwd = executable
        .parent()
        .and_then(Path::to_str)
        .context("无法读取 CK3 工作目录")?;
    let port = format!("-nakama_port={}", backend.port());
    let key = format!("-nakama_key={SERVER_KEY}");
    let options = SpawnOptions::new().cwd(CString::new(cwd)?).argv([
        game,
        &key,
        "-nakama_host=127.0.0.1",
        &port,
        "-nakama_ssl=0",
        "-debug_mode",
    ]);
    let mut device = manager.get_local_device()?;
    let pid = device.spawn(game, &options).context("启动 CK3 房主进程")?;
    let mut resumed = false;
    let result = attach(
        &device,
        pid,
        username,
        backend,
        capture,
        stop,
        updates,
        &mut resumed,
    );
    if !resumed {
        // This PID was created suspended by this launch attempt and has no game state.
        let _ = manager
            .get_local_device()
            .and_then(|mut local| local.kill(pid));
    }
    result
}

struct AdapterMessages(Sender<Result<Value>>);

impl ScriptHandler for AdapterMessages {
    fn on_message(&mut self, message: Message, _: Option<Vec<u8>>) {
        let event = match message {
            Message::Send(message) if message.payload.r#type == "ck3:host" => {
                Ok(message.payload.returns)
            }
            Message::Error(error) => Err(anyhow!("{}", error.description)),
            Message::Other(value) => Err(anyhow!("房主脚本消息格式错误 {value}")),
            _ => return,
        };
        let _ = self.0.send(event);
    }
}

#[allow(clippy::too_many_arguments)]
fn attach(
    device: &Device<'_>,
    pid: u32,
    username: &str,
    backend: &LocalBackend,
    capture: &Path,
    stop: &Receiver<()>,
    updates: &Sender<HostUpdate>,
    resumed: &mut bool,
) -> Result<()> {
    let session = device.attach(pid).context("连接刚启动的 CK3 房主")?;
    let config = json!({"host": "127.0.0.1", "port": backend.port(), "username": username,
        "exe_sha256": EXE_SHA256});
    let source = format!("const localHostConfig = {config};\n{ADAPTER}");
    let mut script = session
        .create_script(&source, &mut ScriptOption::new().set_name("ck3-local-host"))
        .context("创建 CK3 本地房主适配器")?;
    let (send, events) = mpsc::channel();
    script.handle_message(AdapterMessages(send))?;
    script.load().context("加载 CK3 本地房主适配器")?;
    if stopped(stop, Duration::ZERO) {
        return Ok(());
    }
    device.resume(pid).context("继续运行 CK3")?;
    *resumed = true;
    updates.send(HostUpdate::Started { pid })?;
    backend.record(
        "local_game_started",
        json!({"process_id": pid, "username": username}),
    );

    let result = process_events(&session, backend, capture, &events, stop, updates);
    if !session.is_detached() {
        let _ = script.unload();
        session.detach().context("卸载 CK3 房主适配器")?;
    }
    result
}

fn process_events(
    session: &frida::Session<'_>,
    backend: &LocalBackend,
    capture: &Path,
    events: &Receiver<Result<Value>>,
    stop: &Receiver<()>,
    updates: &Sender<HostUpdate>,
) -> Result<()> {
    let profile_path = capture.join("session_profile.json");
    let mut candidate = None;
    while !stopped(stop, Duration::ZERO) && !session.is_detached() {
        match events.recv_timeout(Duration::from_millis(100)) {
            Ok(result) => {
                let event = result.context("CK3 房主适配器错误")?;
                match event["event"].as_str() {
                    Some("local_game_status") if event["connected"].as_bool() == Some(true) => {
                        updates.send(HostUpdate::Ready)?;
                    }
                    Some("local_session_profile") => candidate = event.get("profile").cloned(),
                    Some("local_bridge_error") => bail!("CK3 房主适配器错误 {}", event["message"]),
                    _ => {}
                }
                let name = event["event"]
                    .as_str()
                    .unwrap_or("bridge_message")
                    .to_owned();
                backend.record(&name, event);
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if let Some(profile) = &candidate {
            let room = profile["match_id"].as_str().context("房间 ID 缺失")?;
            let owner = profile["host_user_id"].as_str().context("房主 ID 缺失")?;
            if backend.owns_joined_room(room, owner) {
                write_json(&profile_path, profile)?;
                updates.send(HostUpdate::Profile(profile_path.clone()))?;
                candidate = None;
            }
        }
    }
    Ok(())
}

fn stopped(stop: &Receiver<()>, duration: Duration) -> bool {
    stop.recv_timeout(duration) != Err(RecvTimeoutError::Timeout)
}
