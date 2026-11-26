//! Read-only views over the supported local CK3 executable.

#[cfg(any(test, all(windows, target_arch = "x86_64")))]
mod objects;
#[cfg(all(windows, target_arch = "x86_64"))]
mod process;

use anyhow::Result;
use serde_json::Value;

use super::{Query, QuerySource};

#[cfg(all(windows, target_arch = "x86_64"))]
use std::sync::Mutex;

pub struct LiveWorld {
    #[cfg(all(windows, target_arch = "x86_64"))]
    process: process::Process,
    #[cfg(all(windows, target_arch = "x86_64"))]
    catalogs: Mutex<objects::Catalogs>,
}

impl LiveWorld {
    pub fn connect(pid: Option<u32>) -> Result<Self> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return Ok(Self {
            process: process::Process::open(pid)?,
            catalogs: Mutex::default(),
        });
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = pid;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }

    pub fn is_running(&self) -> bool {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.process.is_running();
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        false
    }

    pub fn pid(&self) -> u32 {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.process.pid;
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        0
    }
}

impl QuerySource for LiveWorld {
    fn query(&self, request: Query<'_>) -> Result<Value> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        {
            anyhow::ensure!(self.is_running(), "实时 CK3 进程已退出");
            let mut catalogs = self
                .catalogs
                .lock()
                .map_err(|_| anyhow::anyhow!("实时世界读取中断，请重新连接"))?;
            catalogs.query(&self.process, self.process.base, request)
        }
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = request;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }
}
