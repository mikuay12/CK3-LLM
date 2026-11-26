use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Error};
use std::mem::size_of;
use std::os::windows::ffi::OsStringExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::PathBuf;

use anyhow::{Context, Result, bail, ensure};
use sha2::{Digest, Sha256};
use windows_sys::Win32::Foundation::{ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE, STILL_ACTIVE};
use windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory;
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, MODULEENTRY32W, Module32FirstW, Module32NextW, PROCESSENTRY32W,
    Process32FirstW, Process32NextW, TH32CS_SNAPMODULE, TH32CS_SNAPPROCESS,
};
use windows_sys::Win32::System::Threading::{
    GetExitCodeProcess, OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    QueryFullProcessImageNameW,
};

use crate::session::{CK3_BUILD, EXE_SHA256};

use super::objects::{MAX_READ_BYTES, Memory, REQUIRED_IMAGE_BYTES};

pub(super) struct Process {
    handle: OwnedHandle,
    pub pid: u32,
    pub base: u64,
}

impl Process {
    pub fn open(pid: Option<u32>) -> Result<Self> {
        let pid = match pid {
            Some(pid) => pid,
            None => find_game()?,
        };
        // The validated process handle stays open for the lifetime of the reader.
        let handle = unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid) };
        if handle.is_null() {
            return Err(Error::last_os_error()).context("打开 CK3 进程失败");
        }
        let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
        let mut path = vec![0u16; 32_768];
        let mut length = path.len() as u32;
        if unsafe {
            QueryFullProcessImageNameW(handle.as_raw_handle(), 0, path.as_mut_ptr(), &mut length)
        } == 0
        {
            return Err(Error::last_os_error()).context("无法读取 CK3 进程的可执行文件路径");
        }
        let image = PathBuf::from(OsString::from_wide(&path[..length as usize]));
        ensure!(
            image
                .file_name()
                .is_some_and(|name| name.eq_ignore_ascii_case("ck3.exe")),
            "PID {pid} 对应的进程不是 CK3"
        );
        let mut executable = File::open(&image).context("无法打开 CK3 可执行文件")?;
        let mut hash = Sha256::new();
        io::copy(&mut executable, &mut hash).context("无法校验 CK3 可执行文件")?;
        ensure!(
            format!("{:x}", hash.finalize()) == EXE_SHA256,
            "实时查询需要已验证的 CK3 {CK3_BUILD}"
        );
        let base = game_base(pid)?;
        let process = Self { handle, pid, base };
        ensure!(process.is_running(), "CK3 进程已退出");
        Ok(process)
    }

    pub fn is_running(&self) -> bool {
        let mut code = 0;
        let success = unsafe { GetExitCodeProcess(self.handle.as_raw_handle(), &mut code) };
        success != 0 && code == STILL_ACTIVE as u32
    }
}

impl Memory for Process {
    fn read(&self, address: u64, buffer: &mut [u8]) -> Result<()> {
        ensure!(
            address > 0 && address < isize::MAX as u64 && buffer.len() <= MAX_READ_BYTES,
            "无效的 CK3 内存读取范围"
        );
        let mut read = 0;
        // Windows copies bytes into this slice via the read-only process handle.
        let success = unsafe {
            ReadProcessMemory(
                self.handle.as_raw_handle(),
                address as usize as *const _,
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut read,
            )
        };
        if success == 0 {
            return Err(Error::last_os_error()).context("CK3 内存暂不可读");
        }
        ensure!(read == buffer.len(), "CK3 内存读取不完整");
        Ok(())
    }
}

fn snapshot(flags: u32, pid: u32) -> Result<OwnedHandle> {
    let handle = unsafe { CreateToolhelp32Snapshot(flags, pid) };
    if handle == INVALID_HANDLE_VALUE {
        return Err(Error::last_os_error()).context("无法列举本机 CK3 进程或模块");
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}

fn ck3_name(name: &[u16]) -> bool {
    let end = name
        .iter()
        .position(|&value| value == 0)
        .unwrap_or(name.len());
    OsString::from_wide(&name[..end]).eq_ignore_ascii_case("ck3.exe")
}

fn find_game() -> Result<u32> {
    let snapshot = snapshot(TH32CS_SNAPPROCESS, 0)?;
    let mut entry = PROCESSENTRY32W {
        dwSize: size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut success = unsafe { Process32FirstW(snapshot.as_raw_handle(), &mut entry) };
    let mut pid = None;
    while success != 0 {
        if ck3_name(&entry.szExeFile) {
            ensure!(pid.is_none(), "检测到多个 CK3 进程，请保留要查询的进程");
            pid = Some(entry.th32ProcessID);
        }
        success = unsafe { Process32NextW(snapshot.as_raw_handle(), &mut entry) };
    }
    check_enumeration_end()?;
    pid.context("请先启动 CK3，再连接实时游戏")
}

fn game_base(pid: u32) -> Result<u64> {
    let snapshot = snapshot(TH32CS_SNAPMODULE, pid)?;
    let mut entry = MODULEENTRY32W {
        dwSize: size_of::<MODULEENTRY32W>() as u32,
        ..Default::default()
    };
    let mut success = unsafe { Module32FirstW(snapshot.as_raw_handle(), &mut entry) };
    while success != 0 {
        if ck3_name(&entry.szModule) {
            ensure!(
                !entry.modBaseAddr.is_null()
                    && u64::from(entry.modBaseSize) >= REQUIRED_IMAGE_BYTES,
                "CK3 进程的模块布局无法识别"
            );
            return Ok(entry.modBaseAddr as u64);
        }
        success = unsafe { Module32NextW(snapshot.as_raw_handle(), &mut entry) };
    }
    check_enumeration_end()?;
    bail!("CK3 游戏模块尚未加载")
}

fn check_enumeration_end() -> Result<()> {
    let error = Error::last_os_error();
    if error.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
        return Err(error).context("列举 CK3 进程或模块失败");
    }
    Ok(())
}
