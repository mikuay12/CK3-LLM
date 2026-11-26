use std::ffi::{CStr, c_int, c_void};
use std::fs::File;
use std::path::PathBuf;
use std::ptr::{NonNull, null_mut};
use std::sync::OnceLock;

use anyhow::{Context, Result, bail, ensure};
use libloading::Library;
use sha2::{Digest, Sha256};

type Handle = *mut c_void;
type Status = c_int;
const DLL_SHA256: &str = "010d85dce84b8f6ffaec3d5420f1b5f7768f859eb831be574c7da23e656ee8f1";

struct Api {
    _library: Library,
    open: unsafe extern "C" fn(c_int, c_int, *mut Handle) -> Status,
    close: unsafe extern "C" fn(Handle) -> Status,
    error: unsafe extern "C" fn(Status) -> *const std::ffi::c_char,
    control: unsafe extern "C" fn(Handle, u32, ...) -> Status,
    memory_map: unsafe extern "C" fn(Handle, u64, usize, u32) -> Status,
    memory_read: unsafe extern "C" fn(Handle, u64, *mut c_void, usize) -> Status,
    memory_write: unsafe extern "C" fn(Handle, u64, *const c_void, usize) -> Status,
    register_read: unsafe extern "C" fn(Handle, c_int, *mut c_void) -> Status,
    register_write: unsafe extern "C" fn(Handle, c_int, *const c_void) -> Status,
    start: unsafe extern "C" fn(Handle, u64, u64, u64, usize) -> Status,
    stop: unsafe extern "C" fn(Handle) -> Status,
    hook: unsafe extern "C" fn(
        Handle,
        *mut usize,
        c_int,
        *const c_void,
        *mut c_void,
        u64,
        u64,
        ...
    ) -> Status,
}

impl Api {
    fn load() -> Result<&'static Self> {
        static API: OnceLock<Api> = OnceLock::new();
        if let Some(api) = API.get() {
            return Ok(api);
        }
        let path = if let Some(path) = std::env::var_os("CK3_UNICORN_DLL") {
            PathBuf::from(path)
        } else {
            std::env::current_exe()?.with_file_name("unicorn.dll")
        };
        let path =
            std::fs::canonicalize(&path).context("找不到随程序提供的 Unicorn 2.1.4 运行时")?;
        let mut file = File::open(&path)?;
        let mut hash = Sha256::new();
        std::io::copy(&mut file, &mut hash)?;
        ensure!(
            format!("{:x}", hash.finalize()) == DLL_SHA256,
            "Unicorn 运行时校验失败"
        );

        // The pinned x64 Unicorn binary exports this C ABI.
        let library = unsafe { Library::new(path) }.context("加载 Unicorn 2.1.4 运行时")?;
        let api = unsafe {
            Self {
                open: *library.get(b"uc_open\0")?,
                close: *library.get(b"uc_close\0")?,
                error: *library.get(b"uc_strerror\0")?,
                control: *library.get(b"uc_ctl\0")?,
                memory_map: *library.get(b"uc_mem_map\0")?,
                memory_read: *library.get(b"uc_mem_read\0")?,
                memory_write: *library.get(b"uc_mem_write\0")?,
                register_read: *library.get(b"uc_reg_read\0")?,
                register_write: *library.get(b"uc_reg_write\0")?,
                start: *library.get(b"uc_emu_start\0")?,
                stop: *library.get(b"uc_emu_stop\0")?,
                hook: *library.get(b"uc_hook_add\0")?,
                _library: library,
            }
        };
        Ok(API.get_or_init(|| api))
    }

    fn check(&self, status: Status) -> Result<()> {
        if status == 0 {
            return Ok(());
        }
        let message = unsafe { CStr::from_ptr((self.error)(status)) }.to_string_lossy();
        bail!("Unicorn {message}");
    }
}

#[derive(Clone, Copy)]
pub(super) struct Cpu {
    api: &'static Api,
    handle: NonNull<c_void>,
}

pub(super) struct Engine(Cpu);

impl Engine {
    pub fn open() -> Result<Self> {
        let api = Api::load()?;
        let mut handle = null_mut();
        api.check(unsafe { (api.open)(4, 8, &mut handle) })?; // UC_ARCH_X86, UC_MODE_64
        let engine = Self(Cpu {
            api,
            handle: NonNull::new(handle).context("Unicorn 返回空句柄")?,
        });
        // UC_CTL_WRITE(UC_CTL_CPU_MODEL, 1), UC_CPU_X86_HASWELL
        api.check(unsafe { (api.control)(handle, (1 << 30) | (1 << 26) | 7, 19_i32) })?;
        let cr4 = engine.0.register(CR4)?;
        engine
            .0
            .set_register(CR4, cr4 | (1 << 18) | (1 << 9) | (1 << 10))?;
        Ok(engine)
    }

    pub fn cpu(&self) -> Cpu {
        self.0
    }

    // Callers keep the callback and its context valid until this engine is dropped.
    pub unsafe fn hook(
        &self,
        kind: c_int,
        callback: *const c_void,
        data: *mut c_void,
        begin: u64,
        end: u64,
    ) -> Result<()> {
        let mut hook = 0_usize;
        self.0.api.check(unsafe {
            (self.0.api.hook)(
                self.0.handle.as_ptr(),
                &mut hook,
                kind,
                callback,
                data,
                begin,
                end,
            )
        })
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        unsafe { (self.0.api.close)(self.0.handle.as_ptr()) };
    }
}

pub(super) const RAX: c_int = 35;
pub(super) const RBP: c_int = 36;
pub(super) const RBX: c_int = 37;
pub(super) const RCX: c_int = 38;
pub(super) const RDX: c_int = 40;
pub(super) const RIP: c_int = 41;
pub(super) const RSP: c_int = 44;
const CR4: c_int = 54;
pub(super) const R8: c_int = 106;
pub(super) const R9: c_int = 107;
pub(super) const R13: c_int = 111;
pub(super) const GS_BASE: c_int = 251;
pub(super) const CODE_HOOK: c_int = 4;
pub(super) const UNMAPPED_HOOK: c_int = 112;

impl Cpu {
    pub fn map(self, address: u64, len: usize, flags: u32) -> Result<()> {
        self.api
            .check(unsafe { (self.api.memory_map)(self.handle.as_ptr(), address, len, flags) })
    }

    pub fn read(self, address: u64, bytes: &mut [u8]) -> Result<()> {
        self.api.check(unsafe {
            (self.api.memory_read)(
                self.handle.as_ptr(),
                address,
                bytes.as_mut_ptr().cast(),
                bytes.len(),
            )
        })
    }

    pub fn write(self, address: u64, bytes: &[u8]) -> Result<()> {
        self.api.check(unsafe {
            (self.api.memory_write)(
                self.handle.as_ptr(),
                address,
                bytes.as_ptr().cast(),
                bytes.len(),
            )
        })
    }

    pub fn register(self, code: c_int) -> Result<u64> {
        let mut value = 0_u64;
        self.api.check(unsafe {
            (self.api.register_read)(self.handle.as_ptr(), code, (&raw mut value).cast())
        })?;
        Ok(value)
    }

    pub fn set_register(self, code: c_int, value: u64) -> Result<()> {
        self.api.check(unsafe {
            (self.api.register_write)(self.handle.as_ptr(), code, (&raw const value).cast())
        })
    }

    pub fn run(self, begin: u64, end: u64, instructions: usize) -> Result<()> {
        self.api.check(unsafe {
            (self.api.start)(self.handle.as_ptr(), begin, end, 1_000_000, instructions)
        })?;
        ensure!(self.register(RIP)? == end, "原生求值未在指令预算内完成");
        Ok(())
    }

    pub fn stop(self) {
        unsafe { (self.api.stop)(self.handle.as_ptr()) };
    }
}
