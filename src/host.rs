mod auth;
pub mod backend;
#[cfg(feature = "host-bridge")]
mod bridge;
#[cfg(feature = "host-bridge")]
mod frida_runtime;
#[cfg(any(feature = "host-bridge", test))]
mod frontend;
mod wire;

use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender, TryRecvError};
use std::thread::JoinHandle;

use anyhow::Result;

use crate::settings::HostSettings;

pub enum HostUpdate {
    Listening { port: u16, capture: PathBuf },
    Started { pid: u32 },
    Ready,
    Profile(PathBuf),
    Ended(Result<()>),
}

pub struct HostTask {
    stop: Sender<()>,
    updates: Receiver<HostUpdate>,
    thread: Option<JoinHandle<()>>,
}

impl HostTask {
    #[cfg(feature = "host-bridge")]
    pub fn start(game_dir: PathBuf, settings: HostSettings) -> Result<Self> {
        let (stop, stopped) = std::sync::mpsc::channel();
        let (send, updates) = std::sync::mpsc::channel();
        let thread = std::thread::Builder::new()
            .name("ck3-host".into())
            .spawn(move || {
                let result = bridge::run(game_dir, settings, &stopped, &send);
                let _ = send.send(HostUpdate::Ended(result));
            })?;
        Ok(Self {
            stop,
            updates,
            thread: Some(thread),
        })
    }

    #[cfg(not(feature = "host-bridge"))]
    pub fn start(_: PathBuf, _: HostSettings) -> Result<Self> {
        anyhow::bail!("当前开发构建缺少房主运行时，请使用 GitHub Actions 的 Windows 安装包")
    }

    pub fn try_recv(&self) -> Result<HostUpdate, TryRecvError> {
        self.updates.try_recv()
    }

    pub fn stop(&self) {
        let _ = self.stop.send(());
    }
}

impl Drop for HostTask {
    fn drop(&mut self) {
        self.stop();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
