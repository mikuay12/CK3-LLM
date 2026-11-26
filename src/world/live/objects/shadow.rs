mod rules;
#[cfg(test)]
mod tests;
mod unicorn;

use std::collections::{BTreeMap, HashMap, HashSet};
use std::ffi::{c_int, c_void};
use std::panic::{AssertUnwindSafe, catch_unwind};

use anyhow::{Context, Result, anyhow, bail, ensure};

use super::Memory;
pub(super) use rules::Interaction;
use unicorn::{CODE_HOOK, Cpu, Engine, GS_BASE, R8, R9, RAX, RCX, RDX, RSP, UNMAPPED_HOOK};

const PAGE: u64 = 4096;
const CODE_START: u64 = 0x1000;
const CODE_END: u64 = 0x43e9500;
const IMAGE_END: u64 = 0x61da000;

const STACK: u64 = 0x7000_0000_0000;
const TLS: u64 = STACK + 0x100000;
const ARENA: u64 = STACK + 0x200000;
const STUB: u64 = STACK + 0x1000000;
const STOP: u64 = STUB + 0xf000;
const STACK_SIZE: usize = 0x40000;
const ARENA_SIZE: usize = 0x800000;
const STACK_TOP: u64 = STACK + STACK_SIZE as u64 - 0x108;

pub(super) struct Shadow<'a> {
    engine: Engine,
    state: Box<State<'a>>,
}

struct State<'a> {
    memory: &'a dyn Memory,
    base: u64,
    cpu: Cpu,
    pages: HashSet<u64>,
    patches: BTreeMap<u64, Vec<u8>>,
    stubs: HashMap<u64, Stub>,
    allocations: HashMap<u64, usize>,
    next: u64,
    tls_values: HashMap<u64, u64>,
    tls_next: u64,
    counter: u64,
    error: Option<anyhow::Error>,
}

#[derive(Clone, Copy)]
enum Stub {
    Passthrough,
    Return(u64),
    Malloc,
    Free,
    HeapAlloc,
    HeapRealloc,
    HeapFree,
    HeapSize,
    VirtualAlloc,
    TlsAlloc,
    TlsFree,
    TlsGet,
    TlsSet,
    Counter,
    Frequency,
    FileTime,
    WriteFile,
    Find(usize),
}

const IMPORTS: &[(u64, Stub)] = &[
    (0x43ea1a0, Stub::FileTime),    // GetSystemTimeAsFileTime
    (0x43ea1c8, Stub::Passthrough), // AcquireSRWLockShared
    (0x43ea1d0, Stub::Passthrough), // ReleaseSRWLockShared
    (0x43ea240, Stub::Return(1)),   // InitializeCriticalSectionEx
    (0x43ea2e0, Stub::TlsFree),
    (0x43ea388, Stub::HeapRealloc),
    (0x43ea460, Stub::TlsGet),
    (0x43ea468, Stub::TlsAlloc),
    (0x43ea470, Stub::TlsSet),
    (0x43ea4e8, Stub::Passthrough), // DeleteCriticalSection
    (0x43ea4f0, Stub::Passthrough), // LeaveCriticalSection
    (0x43ea4f8, Stub::Return(1)),   // InitializeCriticalSectionAndSpinCount
    (0x43ea500, Stub::Passthrough), // EnterCriticalSection
    (0x43ea508, Stub::Return(1)),   // TryEnterCriticalSection
    (0x43ea540, Stub::Passthrough), // Sleep
    (0x43ea548, Stub::Return(1)),   // SetFilePointerEx
    (0x43ea598, Stub::Return(1)),   // GetCurrentThreadId
    (0x43ea630, Stub::Counter),
    (0x43ea638, Stub::Frequency),
    (0x43ea648, Stub::Return(0)), // SleepEx
    (0x43ea668, Stub::Return(1)), // VirtualProtect
    (0x43ea670, Stub::Return(1)), // VirtualFree
    (0x43ea678, Stub::VirtualAlloc),
    (0x43ea688, Stub::Return(1)),   // TryAcquireSRWLockExclusive
    (0x43ea690, Stub::Passthrough), // AcquireSRWLockExclusive
    (0x43ea698, Stub::Passthrough), // InitializeSRWLock
    (0x43ea738, Stub::Return(0)),   // GetLastError
    (0x43ea748, Stub::HeapFree),
    (0x43ea750, Stub::WriteFile),
    (0x43ea760, Stub::Return(0xdeadbeef)), // GetProcessHeap
    (0x43ea768, Stub::HeapAlloc),
    (0x43ea780, Stub::Return(u64::MAX - 1)), // GetCurrentThread
    (0x43ea7c8, Stub::HeapSize),
    (0x43ea7d8, Stub::Passthrough), // ReleaseSRWLockExclusive
];

const CODE_STUBS: &[(u64, Stub)] = &[
    (0x4233514, Stub::Malloc),
    (0x4233550, Stub::Malloc),
    (0x4251a38, Stub::Malloc),
    (0x40ff040, Stub::Free),
    (0x42338c4, Stub::Free),
    (0x425dce4, Stub::Free),
    (0x3fa3230, Stub::Find(1)),
    (0x3fa32b0, Stub::Find(2)),
    (0x3fa3380, Stub::Find(4)),
    (0x3fa3400, Stub::Find(8)),
];

impl<'a> Shadow<'a> {
    pub fn new(memory: &'a dyn Memory, base: u64) -> Result<Self> {
        ensure!(
            base.is_multiple_of(PAGE)
                && base <= 0x8000_0000_0000 - IMAGE_END
                && (base + IMAGE_END <= STACK || base >= STUB + 0x10000),
            "CK3 模块地址无法用于隔离求值"
        );
        let engine = Engine::open()?;
        let cpu = engine.cpu();
        for (address, size, flags) in [
            (STACK, STACK_SIZE, 3),
            (TLS, 0x10000, 3),
            (ARENA, ARENA_SIZE, 3),
            (STUB, 0x10000, 7),
        ] {
            cpu.map(address, size, flags)?;
        }
        cpu.write(STOP, &[0xc3])?;
        cpu.set_register(GS_BASE, TLS)?;
        cpu.write(TLS + 0x30, &TLS.to_le_bytes())?;
        cpu.write(TLS + 0x58, &(TLS + 0x100).to_le_bytes())?;
        cpu.write(TLS + 0x100, &(TLS + 0x1000).to_le_bytes())?;
        cpu.write(TLS + 0x1010, &0x7fff_ffff_u32.to_le_bytes())?;
        let mut state = Box::new(State {
            memory,
            base,
            cpu,
            pages: HashSet::new(),
            patches: BTreeMap::new(),
            stubs: HashMap::new(),
            allocations: HashMap::new(),
            next: ARENA + 0x1000,
            tls_values: HashMap::new(),
            tls_next: 0x10,
            counter: 1_000_000,
            error: None,
        });
        state.patch(0x5d32ae8, &[0]);
        state.patch(0x5d32aec, &[0]);
        state.patch(0x5cd44ac, &[0; 4]);
        state.patch(0x5443098, &[2, 0, 0, 0, 6, 0, 0, 0]);
        state.patch(0x5c72db0, &0xdeadbeef_u64.to_le_bytes());
        for (index, &(rva, stub)) in IMPORTS.iter().enumerate() {
            let address = STUB + index as u64 * 0x10;
            cpu.write(address, &[0xc3])?;
            state.patch(rva, &address.to_le_bytes());
            state.stubs.insert(address, stub);
        }
        for &(rva, stub) in CODE_STUBS {
            state.patch(rva, &[0xc3]);
            state.stubs.insert(base + rva, stub);
        }
        let precise_time = precise_file_time();
        if let Some(address) = precise_time {
            let page = address & !(PAGE - 1);
            cpu.map(page, PAGE as usize, 5)?;
            cpu.write(address, &[0xc3])?;
            state.pages.insert(page);
            state.stubs.insert(address, Stub::FileTime);
        }
        let context: *mut c_void = (&raw mut *state).cast();
        // Unicorn holds the callback data only while the owning engine is alive.
        unsafe {
            engine.hook(UNMAPPED_HOOK, unmapped as *const c_void, context, 1, 0)?;
            engine.hook(
                CODE_HOOK,
                dispatch as *const c_void,
                context,
                STUB,
                STUB + IMPORTS.len() as u64 * 0x10,
            )?;
            for &(rva, _) in CODE_STUBS {
                engine.hook(
                    CODE_HOOK,
                    dispatch as *const c_void,
                    context,
                    base + rva,
                    base + rva,
                )?;
            }
            if let Some(address) = precise_time {
                let page = address & !(PAGE - 1);
                engine.hook(
                    CODE_HOOK,
                    dispatch as *const c_void,
                    context,
                    page,
                    page + PAGE - 1,
                )?;
            }
        }
        Ok(Self { engine, state })
    }

    fn base(&self) -> u64 {
        self.state.base
    }

    fn alloc(&mut self, size: u64) -> Result<u64> {
        self.state.alloc(size)
    }

    fn read<const N: usize>(&mut self, address: u64) -> Result<[u8; N]> {
        let mut bytes = [0; N];
        self.state.read(address, &mut bytes)?;
        Ok(bytes)
    }

    fn write(&mut self, address: u64, bytes: &[u8]) -> Result<()> {
        self.state.write(address, bytes)
    }

    fn write32(&mut self, address: u64, value: u32) -> Result<()> {
        self.write(address, &value.to_le_bytes())
    }

    fn write64(&mut self, address: u64, value: u64) -> Result<()> {
        self.write(address, &value.to_le_bytes())
    }

    fn call(&mut self, rva: u64, arguments: &[u64]) -> Result<u64> {
        ensure!(arguments.len() <= 16, "原生调用参数过多");
        let cpu = self.engine.cpu();
        cpu.set_register(RSP, STACK_TOP)?;
        cpu.write(STACK_TOP, &STOP.to_le_bytes())?;
        for (index, register) in [RCX, RDX, R8, R9].into_iter().enumerate() {
            cpu.set_register(register, arguments.get(index).copied().unwrap_or(0))?;
        }
        for (index, &value) in arguments.iter().skip(4).enumerate() {
            cpu.write(STACK_TOP + 0x28 + index as u64 * 8, &value.to_le_bytes())?;
        }
        self.run(rva, STOP, 2_000_000)?;
        cpu.register(RAX)
    }

    fn branch(
        &mut self,
        start: u64,
        end: u64,
        registers: &[(c_int, u64)],
        output: c_int,
    ) -> Result<u64> {
        let cpu = self.engine.cpu();
        cpu.set_register(RSP, STACK_TOP)?;
        for &(register, value) in registers {
            cpu.set_register(register, value)?;
        }
        self.run(start, self.base() + end, 2_000)?;
        cpu.register(output)
    }

    fn run(&mut self, rva: u64, stop: u64, limit: usize) -> Result<()> {
        ensure!(
            (CODE_START..CODE_END).contains(&rva),
            "原生求值入口超出 CK3 代码区"
        );
        self.state.error = None;
        let result = self.engine.cpu().run(self.base() + rva, stop, limit);
        if let Some(error) = self.state.error.take() {
            return Err(error).with_context(|| format!("执行 CK3 原生规则 {rva:#x}"));
        }
        result.with_context(|| format!("执行 CK3 原生规则 {rva:#x}"))
    }
}

impl State<'_> {
    fn patch(&mut self, rva: u64, value: &[u8]) {
        self.patches.insert(self.base + rva, value.to_vec());
    }

    fn ensure(&mut self, address: u64, size: usize) -> Result<()> {
        if size == 0 {
            return Ok(());
        }
        let end = address
            .checked_add(size as u64 - 1)
            .context("原生数据地址溢出")?;
        ensure!(size <= ARENA_SIZE, "原生求值单次读取超出预算");
        for page in ((address & !(PAGE - 1))..=(end & !(PAGE - 1))).step_by(PAGE as usize) {
            self.map(page)?;
        }
        Ok(())
    }

    fn map(&mut self, page: u64) -> Result<()> {
        ensure!(
            (0x10000..0x8000_0000_0000).contains(&page),
            "原生对象地址无效"
        );
        if self.pages.contains(&page)
            || [
                STACK..STACK + STACK_SIZE as u64,
                TLS..TLS + 0x10000,
                ARENA..ARENA + ARENA_SIZE as u64,
                STUB..STUB + 0x10000,
            ]
            .iter()
            .any(|range| range.contains(&page))
        {
            return Ok(());
        }
        ensure!(self.pages.len() < 16_384, "原生求值内存超过 64 MiB");
        let mut bytes = [0_u8; PAGE as usize];
        self.memory.read(page, &mut bytes)?;
        for (&address, patch) in self.patches.range(page.saturating_sub(7)..page + PAGE) {
            let start = address.max(page);
            let end = (address + patch.len() as u64).min(page + PAGE);
            if start < end {
                bytes[(start - page) as usize..(end - page) as usize]
                    .copy_from_slice(&patch[(start - address) as usize..(end - address) as usize]);
            }
        }
        let executable = (self.base + CODE_START..self.base + CODE_END).contains(&page);
        self.cpu
            .map(page, PAGE as usize, if executable { 7 } else { 3 })?;
        self.cpu.write(page, &bytes)?;
        self.pages.insert(page);
        Ok(())
    }

    fn imported_at(&self, target: u64) -> Option<u64> {
        for rva in (0x43e9000..0x43ee000).step_by(PAGE as usize) {
            let mut bytes = [0_u8; PAGE as usize];
            if self.memory.read(self.base + rva, &mut bytes).is_ok()
                && let Some(index) = bytes
                    .as_chunks::<8>()
                    .0
                    .iter()
                    .position(|&bytes| u64::from_le_bytes(bytes) == target)
            {
                return Some(rva + index as u64 * 8);
            }
        }
        None
    }

    fn read(&mut self, address: u64, bytes: &mut [u8]) -> Result<()> {
        self.ensure(address, bytes.len())?;
        self.cpu.read(address, bytes)
    }

    fn write(&mut self, address: u64, bytes: &[u8]) -> Result<()> {
        self.ensure(address, bytes.len())?;
        self.cpu.write(address, bytes)
    }

    fn alloc(&mut self, size: u64) -> Result<u64> {
        let size = size.max(1);
        let end = self
            .next
            .checked_add(size)
            .and_then(|end| end.checked_add(15))
            .context("原生临时内存长度无效")?
            & !15;
        ensure!(end <= ARENA + ARENA_SIZE as u64, "原生临时内存不足");
        let address = self.next;
        self.next = end;
        self.allocations.insert(address, size as usize);
        Ok(address)
    }

    fn stub(&mut self, address: u64) -> Result<()> {
        let &handler = self
            .stubs
            .get(&address)
            .context("原生规则进入了尚未接入的系统代码")?;
        let cpu = self.cpu;
        let result = match handler {
            Stub::Passthrough => return Ok(()),
            Stub::Return(value) => value,
            Stub::Malloc => self.alloc(cpu.register(RCX)?)?,
            Stub::Free => {
                self.allocations.remove(&cpu.register(RCX)?);
                return Ok(());
            }
            Stub::HeapAlloc => self.alloc(cpu.register(R8)?)?,
            Stub::HeapRealloc => {
                let before = cpu.register(R8)?;
                let size = cpu.register(R9)?;
                let address = self.alloc(size)?;
                if before != 0 {
                    let count = self
                        .allocations
                        .remove(&before)
                        .unwrap_or(size as usize)
                        .min(size as usize);
                    let mut bytes = vec![0; count];
                    self.read(before, &mut bytes)?;
                    self.write(address, &bytes)?;
                }
                address
            }
            Stub::HeapFree => {
                self.allocations.remove(&cpu.register(R8)?);
                1
            }
            Stub::HeapSize => self
                .allocations
                .get(&cpu.register(R8)?)
                .map_or(u64::MAX, |size| *size as u64),
            Stub::VirtualAlloc => self.alloc(cpu.register(RDX)?)?,
            Stub::TlsAlloc => {
                let slot = self.tls_next;
                self.tls_next += 1;
                self.tls_values.insert(slot, 0);
                slot
            }
            Stub::TlsFree => {
                self.tls_values.remove(&(cpu.register(RCX)? & 0xffff_ffff));
                1
            }
            Stub::TlsGet => self
                .tls_values
                .get(&(cpu.register(RCX)? & 0xffff_ffff))
                .copied()
                .unwrap_or(0),
            Stub::TlsSet => {
                self.tls_values
                    .insert(cpu.register(RCX)? & 0xffff_ffff, cpu.register(RDX)?);
                1
            }
            Stub::Counter | Stub::Frequency | Stub::FileTime => {
                let address = cpu.register(RCX)?;
                let value = match handler {
                    Stub::Counter => {
                        self.counter += 100;
                        self.counter
                    }
                    Stub::Frequency => 10_000_000,
                    _ => 133_000_000_000_000_000,
                };
                if address != 0 {
                    self.write(address, &value.to_le_bytes())?;
                }
                if matches!(handler, Stub::FileTime) {
                    return Ok(());
                }
                1
            }
            Stub::WriteFile => {
                let output = cpu.register(R9)?;
                if output != 0 {
                    self.write(output, &(cpu.register(R8)? as u32).to_le_bytes())?;
                }
                1
            }
            Stub::Find(width) => {
                let begin = cpu.register(R8)?;
                let end = cpu.register(R9)?;
                if end <= begin {
                    end
                } else {
                    let length = usize::try_from(end - begin)?;
                    ensure!(length <= ARENA_SIZE, "原生列表查找超出预算");
                    let mut needle = [0; 8];
                    self.read(cpu.register(RSP)? + 0x28, &mut needle[..width])?;
                    let mut bytes = vec![0; length];
                    self.read(begin, &mut bytes)?;
                    bytes
                        .chunks_exact(width)
                        .position(|value| value == &needle[..width])
                        .map_or(end, |index| begin + (index * width) as u64)
                }
            }
        };
        cpu.set_register(RAX, result)
    }
}

fn precise_file_time() -> Option<u64> {
    #[cfg(all(windows, target_arch = "x86_64"))]
    {
        // The CRT can cache this export via GetProcAddress instead of using a game IAT slot.
        // Only its address is used. The emulator supplies the time and never executes the export.
        let module = libloading::os::windows::Library::open_already_loaded("kernel32.dll").ok()?;
        let function = unsafe {
            module
                .get::<unsafe extern "system" fn(*mut c_void)>(b"GetSystemTimePreciseAsFileTime\0")
        }
        .ok()?;
        Some(*function as usize as u64)
    }
    #[cfg(not(all(windows, target_arch = "x86_64")))]
    None
}

unsafe extern "C" fn unmapped(
    _: *mut c_void,
    access: c_int,
    address: u64,
    size: c_int,
    _: i64,
    data: *mut c_void,
) -> bool {
    callback(data, |state| {
        if access == 21 && !(state.base + CODE_START..state.base + CODE_END).contains(&address) {
            if let Some(rva) = state.imported_at(address) {
                bail!("原生函数需要尚未接入的系统调用（导入槽 {rva:#x}）");
            }
            bail!("原生函数尝试执行游戏代码段之外的地址 {address:#x}");
        }
        state.ensure(address, usize::try_from(size)?)
    })
}

unsafe extern "C" fn dispatch(_: *mut c_void, address: u64, _: u32, data: *mut c_void) {
    callback(data, |state| state.stub(address));
}

fn callback(context: *mut c_void, run: impl FnOnce(&mut State<'_>) -> Result<()>) -> bool {
    // The callback receives the stable boxed state from Shadow::new on this worker thread.
    let state = unsafe { &mut *context.cast::<State<'_>>() };
    match catch_unwind(AssertUnwindSafe(|| run(state))) {
        Ok(Ok(())) => true,
        result => {
            state.error = Some(
                result
                    .unwrap_or_else(|_| Err(anyhow!("原生规则回调失败")))
                    .unwrap_err(),
            );
            state.cpu.stop();
            false
        }
    }
}
