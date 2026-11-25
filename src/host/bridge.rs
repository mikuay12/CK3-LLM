use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::HostUpdate;
use super::auth::SERVER_KEY;
use super::backend::LocalBackend;
use super::frida_runtime::{Device, Session};
use super::frontend::Frontend;
use crate::session::{CK3_BUILD, EXE_SHA256};
use crate::settings::{HostSettings, Settings, write_json};

const FRIDA_VERSION: &str = "17.26.2";
const ADAPTER: &str = include_str!("bridge.js");
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
    ensure!(
        settings.port != 0,
        "请选择明确的 CK3 联机端口，默认端口为 7350"
    );
    let directory = std::path::absolute(&game_dir).context("检查 CK3 安装目录")?;
    let executable = directory.join("binaries/ck3.exe");
    verify_executable(&executable, stop)?;
    if stopped(stop, Duration::ZERO) {
        return Ok(());
    }

    let settings_file = Settings::path()?;
    let data = settings_file.parent().context("找不到数据存储目录")?;
    std::fs::create_dir_all(data)?;
    let launch_lock = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(data.join("host.lock"))?;
    launch_lock
        .try_lock()
        .context("另一个 CK3-LLM 窗口正在管理房主")?;
    let device = Device::local(FRIDA_VERSION)?;
    ensure!(
        !device.ck3_running()?,
        "已有 CK3 在运行，请在游戏中退出后再启动本地房主"
    );

    let capture = data
        .join("captures")
        .join(format!("local-game-{}", Uuid::new_v4()));
    let backend = LocalBackend::start(settings.port, Some(&capture))?;
    updates.send(HostUpdate::Listening {
        port: backend.port(),
        capture: capture.clone(),
    })?;
    let mut frontend = Frontend::apply(directory.join("game/gui/frontend_main.gui"), &capture)?;
    let result = run_game(
        &device,
        &executable,
        username,
        &backend,
        &capture,
        stop,
        updates,
    );
    if let Err(error) = &result {
        backend.record("bridge_error", json!({"message": format!("{error:#}")}));
    }
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
    device: &Device,
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
    let arguments = [
        game,
        &key,
        "-nakama_host=127.0.0.1",
        &port,
        "-nakama_ssl=0",
        "-debug_mode",
    ];
    let pid = device
        .spawn(game, cwd, &arguments)
        .context("启动 CK3 房主进程")?;
    let mut resumed = false;
    let result = attach(
        device,
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
        device.cancel_spawn(pid);
    }
    result
}

#[allow(clippy::too_many_arguments)]
fn attach(
    device: &Device,
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
    let (send, events) = mpsc::channel();
    let _script = session
        .load(&source, send)
        .context("加载 CK3 本地房主适配器")?;
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

    process_events(&session, backend, capture, &events, stop, updates)
}

fn process_events(
    session: &Session,
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
