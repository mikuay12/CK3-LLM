"""Zero-injection Copy-on-Write Unicorn evaluator backed by CK3 process memory.

Executes ck3.exe's native AI acceptance functions (``CalcAIAccept`` and
``CalcAIIntermediaryAccept``) inside an isolated Unicorn x86-64 sandbox without
injecting threads, code, or writes into a live game process. Memory pages
are pulled on demand through a read-only memory reader (``ProcessMemory``
when attached to a live CK3 instance, or ``BufferMemory`` in offline tests)
and all writes remain isolated inside Unicorn's private shadow pages.
"""

from pathlib import Path
import hashlib
import math
import struct

from unicorn import (
    Uc,
    UC_ARCH_X86,
    UC_MODE_64,
    UC_HOOK_CODE,
    UC_HOOK_MEM_FETCH_UNMAPPED,
    UC_HOOK_MEM_READ_UNMAPPED,
    UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_PROT_ALL,
    UcError,
)
from unicorn.x86_const import (
    UC_CPU_X86_HASWELL,
    UC_X86_REG_CR4,
    UC_X86_REG_GS_BASE,
    UC_X86_REG_R13,
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RAX,
    UC_X86_REG_RBP,
    UC_X86_REG_RBX,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RIP,
    UC_X86_REG_RSP,
)

from ck3_llm.version import SUPPORTED_EXE_SHA256
from ck3_llm.engine.inspect_network import PortableExecutable
from ck3_llm.protocol.nakama_native import DEFAULT_GAME_DIR


PAGE_SIZE = 0x1000
FIXED_POINT_SCALE = 100_000.0
DEFAULT_SCOPE_TYPE_IDS = {"character": 4}

# Verified RVAs for Crusader Kings III 1.20.1.2 (SUPPORTED_EXE_SHA256).
CALC_AI_INTERMEDIARY_ACCEPT_RVA = 0x308C640
CALC_AI_ACCEPT_RVA = 0x308C740
DB_LOOKUP_RVA = 0x3F93E90
INTERACTION_TYPE_INFO_RVA = 0x554CF70
DECISION_TYPE_INFO_RVA = 0x559AB80
ACTIVITY_TYPE_INFO_RVA = 0x55D51B0
DB_REGISTRY_LOCK_RVA = 0x5CD44AC
SCOPE_TYPE_ARRAY_FUNC_RVA = 0x37A8320
PROFILE_FLAG_RVAS = (0x5D32AE8, 0x5D32AEC)

# Verified native decision & activity evaluation RVAs.
DECISION_IS_SHOWN_RVA = 0x31135B0
DECISION_IS_VALID_RVA = 0x31136C0
DECISION_GET_COST_RVA = 0x1474800
COST_CAN_AFFORD_RVA = 0x311B560
COST_CALCULATE_RVA = 0x311D090
ACTIVITY_TYPE_IS_ON_COOLDOWN_RVA = 0x2BCBD80
ACTIVITY_TYPE_CAN_START_RVA = 0x2BCBEB0
ACTIVITY_TYPE_IS_SHOWN_OFFSET = 0x40
ACTIVITY_TYPE_COST_OFFSET = 0x1368

# Verified native building construction cost, time and validity RVAs.
BUILDING_GOLD_COST_RVA = 0x2C33390
BUILDING_PRESTIGE_COST_RVA = 0x2C33580
BUILDING_PIETY_COST_RVA = 0x2C33760
BUILDING_CONSTRUCTION_TIME_RVA = 0x2C33BD0
BUILDING_CAN_CONSTRUCT_RVA = 0x2C87590

# Verified native CTrigger::IsFulfilled (event options & triggers).
TRIGGER_IS_FULFILLED_RVA = 0x37407C0
EVENT_OPTION_TRIGGER_OFFSET = 0x080
EVENT_OPTION_UNAVAILABLE_OFFSET = 0x150

# Verified native activity join & pending interaction reply validation RVAs.
ACTIVITY_CAN_JOIN_RVA = 0x2BCB140
INTERACTION_DATA_BASE_VALID_RVA = 0x308AA50
INTERACTION_DATA_STILL_VALID_RVA = 0x308AE50
INTERACTION_BLOCK_TRIGGER_OFFSET = 0x1098
INTERACTION_AUTO_ACCEPT_TRIGGER_OFFSET = 0x2290
INTERACTION_AUTO_ACCEPT_FLAG_OFFSET = 0x2718

COST_RESOURCE_NAMES = (
    "gold",
    "prestige",
    "piety",
    "renown",
    "influence",
    "herd",
    "provisions",
    "treasury",
    "merit",
    "barter_goods",
)

# Verified native getter RVAs for world-state numeric fields.
SIEGE_REQUIRED_POINTS_RVA = 0x2526600
SIEGE_PROGRESS_FRACTION_RVA = 0x25252A0
SIEGE_DAILY_PROGRESS_RVA = 0x2527A50
CONSTRUCTION_END_DATE_RVA = 0x24763A0
CONSTRUCTION_PROGRESS_PERCENT_RVA = 0x24761C0
UNIT_LEG_COST_RVA = 0x24B2C00
UNIT_LEG_PROGRESS_RVA = 0x24B3680
UNIT_ETA_BRANCH_START_RVA = 0x24B520A
UNIT_ETA_BRANCH_END_RVA = 0x24B533E
MOVEMENT_LOCK_THRESHOLD_RVA = 0x5C7EB08
REGIMENT_CURRENT_SOLDIERS_RVA = 0xCCE339
REGIMENT_CURRENT_SOLDIERS_END_RVA = 0xCCE37F
MERC_CURRENT_SOLDIERS_RVA = 0x26274C0
MERC_MAX_SOLDIERS_RVA = 0x26275D0
COMBAT_REGIMENT_LOSSES_RVA = 0x2677801
COMBAT_REGIMENT_LOSSES_END_RVA = 0x2677812
PROVINCE_SUPPLY_LIMIT_RVA = 0x2482F30

# CCharacterInteraction struct layout (0x338 bytes) & sub-vtables.
INTERACTION_STRUCT_SIZE = 0x338
TOP_SCOPE_SIZE = 0x168
TOPSCOPE_SAVED_INIT_RVA = 0x88BC90
TOPSCOPE_MAP_VT_RVA = 0x54F22B0
TOPSCOPE_LIST_VT_0_RVA = 0x449D368
TOPSCOPE_LIST_VT_1_RVA = 0x449D3D8
TOPSCOPE_LIST_ALLOC_RVA = 0x54F22B8

OFF_INTERACTION_DEF = 0x000
OFF_PRIMARY_SCOPE = 0x008
OFF_SECONDARY_SCOPE = 0x170
OFF_ACTOR = 0x2D8
OFF_RECIPIENT = 0x2DC
OFF_SECONDARY_ACTOR = 0x2E0
OFF_SECONDARY_RECIPIENT = 0x2E4
OFF_INTERMEDIARY = 0x2E8
OFF_PUPPET = 0x2EC
OFF_TARGET_SCOPE = 0x2F0
OFF_OPTIONS_PTR = 0x300
OFF_OPTIONS_CAP = 0x308
OFF_OPTIONS_LEN = 0x30C
OFF_SPECIAL_INTERACTION = 0x330

# MSVC _Init_thread_epoch / CRT heap globals & thunks.
INIT_THREAD_EPOCH_HIGH = 0x7FFFFFFF
CRT_HEAP_RVA = 0x5C72DB0
MSVC_ISA_AVAILABLE_RVA = 0x5443098  # __isa_available (u32) + __isa_enabled (u32)
CRT_MALLOC_THUNK_RVAS = (0x4233514, 0x4233550, 0x4251A38)
CRT_FREE_THUNK_RVAS = (0x40FF040, 0x42338C4, 0x425DCE4)
# MSVC STL AVX2-compiled __std_find_trivial_{1,2,4,8} helpers (Unicorn 2.x lacks AVX2).
STD_FIND_TRIVIAL_RVAS = (
    (0x3FA3230, 1),
    (0x3FA32B0, 2),
    (0x3FA3380, 4),
    (0x3FA3400, 8),
)
INTERACTION_POPULATE_SCOPES_RVA = 0x3088D40

# KERNEL32.dll IAT slot RVAs in ck3.exe 1.20.1.2.
KERNEL32_IAT_RVAS = {
    "GetSystemTimeAsFileTime": 0x43EA1A0,
    "AcquireSRWLockShared": 0x43EA1C8,
    "ReleaseSRWLockShared": 0x43EA1D0,
    "InitializeCriticalSectionEx": 0x43EA240,
    "TlsFree": 0x43EA2E0,
    "HeapReAlloc": 0x43EA388,
    "TlsGetValue": 0x43EA460,
    "TlsAlloc": 0x43EA468,
    "TlsSetValue": 0x43EA470,
    "DeleteCriticalSection": 0x43EA4E8,
    "LeaveCriticalSection": 0x43EA4F0,
    "InitializeCriticalSectionAndSpinCount": 0x43EA4F8,
    "EnterCriticalSection": 0x43EA500,
    "TryEnterCriticalSection": 0x43EA508,
    "Sleep": 0x43EA540,
    "SetFilePointerEx": 0x43EA548,
    "GetCurrentThreadId": 0x43EA598,
    "QueryPerformanceCounter": 0x43EA630,
    "QueryPerformanceFrequency": 0x43EA638,
    "SleepEx": 0x43EA648,
    "VirtualProtect": 0x43EA668,
    "VirtualFree": 0x43EA670,
    "VirtualAlloc": 0x43EA678,
    "TryAcquireSRWLockExclusive": 0x43EA688,
    "AcquireSRWLockExclusive": 0x43EA690,
    "InitializeSRWLock": 0x43EA698,
    "GetLastError": 0x43EA738,
    "HeapFree": 0x43EA748,
    "WriteFile": 0x43EA750,
    "GetProcessHeap": 0x43EA760,
    "HeapAlloc": 0x43EA768,
    "GetCurrentThread": 0x43EA780,
    "HeapSize": 0x43EA7C8,
    "ReleaseSRWLockExclusive": 0x43EA7D8,
}


class BufferMemory:
    """In-memory sparse page source matching ProcessMemory's read-only API."""

    def __init__(self, base):
        self.base = int(base)
        self._regions = []

    def add_region(self, address, data):
        address = int(address)
        raw = bytes(data)
        self._regions.append((address, address + len(raw), raw))

    def read(self, address, size, fresh=False):
        del fresh
        end = address + size
        buf = bytearray(size)
        covered = False
        for r_start, r_end, raw in self._regions:
            lo = max(address, r_start)
            hi = min(end, r_end)
            if lo < hi:
                covered = True
                buf[lo - address:hi - address] = raw[lo - r_start:hi - r_start]
        if not covered:
            raise MemoryError(f"Unmapped address range 0x{address:x}..0x{end:x}")
        return bytes(buf)


def _align_up(value, alignment=PAGE_SIZE):
    return (value + alignment - 1) & ~(alignment - 1)


def _pe_image_span(pe):
    end = PAGE_SIZE
    for sec in pe.sections:
        sec_end = sec["rva"] + max(sec["virtual_size"], sec["raw_size"])
        if sec_end > end:
            end = sec_end
    return _align_up(end)


def _pick_sandbox_base(image_base, image_span):
    candidate = 0x100_0000_0000
    block_size = 0x010_0000_0000
    while not (candidate + block_size <= image_base or candidate >= image_base + image_span):
        candidate += block_size
    return candidate


def _relocation_deltas(pe, delta):
    """Build {rva: uint64_relocated_value} from PE .reloc if image_base differs."""
    if not delta:
        return {}
    pe_header = struct.unpack_from("<I", pe.data, 0x3C)[0]
    num_dirs = struct.unpack_from("<I", pe.data, pe_header + 24 + 108)[0]
    if num_dirs <= 5:
        return {}
    reloc_rva, reloc_size = struct.unpack_from("<II", pe.data, pe_header + 24 + 112 + 5 * 8)
    if not reloc_rva or not reloc_size:
        return {}
    raw = bytearray()
    # Read relocation directory directly from sections.
    for sec in pe.sections:
        if sec["rva"] <= reloc_rva < sec["rva"] + sec["raw_size"]:
            off = sec["raw_offset"] + (reloc_rva - sec["rva"])
            raw = pe.data[off:off + reloc_size]
            break
    if not raw:
        return {}
    patches = {}
    cursor = 0
    while cursor + 8 <= len(raw):
        page_rva, block_size = struct.unpack_from("<II", raw, cursor)
        if block_size < 8:
            break
        entries = (block_size - 8) // 2
        for entry in struct.unpack_from(f"<{entries}H", raw, cursor + 8):
            kind = entry >> 12
            if kind == 10:  # IMAGE_REL_BASED_DIR64
                patches[page_rva + (entry & 0xFFF)] = delta
        cursor += block_size
    return patches


class ShadowProcessVM:
    """Lazy Copy-on-Write x86-64 Unicorn sandbox over CK3 memory and binary."""

    def __init__(self, executable=None, *, memory_source=None, verify_exe_hash=True, max_instructions=2_000_000):
        self.executable = Path(executable) if executable is not None else DEFAULT_GAME_DIR / "binaries/ck3.exe"
        self.pe = PortableExecutable(self.executable)
        if verify_exe_hash and hashlib.sha256(self.pe.data).hexdigest() != SUPPORTED_EXE_SHA256:
            raise ValueError("ShadowProcessVM offsets require the verified CK3 executable hash")
        self.memory_source = memory_source
        self.base = int(memory_source.base) if memory_source is not None else int(self.pe.image_base)
        self.image_span = _pe_image_span(self.pe)
        self.max_instructions = int(max_instructions)
        self._reloc_deltas = _relocation_deltas(self.pe, self.base - self.pe.image_base) if memory_source is not None else {}

        self.uc = Uc(UC_ARCH_X86, UC_MODE_64)
        self.uc.ctl_set_cpu_model(UC_CPU_X86_HASWELL)
        cr4 = self.uc.reg_read(UC_X86_REG_CR4)
        self.uc.reg_write(UC_X86_REG_CR4, cr4 | (1 << 18) | (1 << 9) | (1 << 10))
        self._cow_pages = set()
        self._immutable_pages = set()
        self._patched_qwords = {}
        self._patched_bytes = {}
        self._fault_error = None
        self._qpc_counter = 1_000_000

        sandbox_base = _pick_sandbox_base(self.base, self.image_span)
        self.stack_base = sandbox_base
        self.stack_size = 0x40000
        self.tls_base = sandbox_base + 0x100000
        self.tls_size = 0x10000
        self.arena_base = sandbox_base + 0x200000
        self.arena_size = 0x800000
        self.stub_base = sandbox_base + 0x1000000
        self.stub_size = 0x10000
        self.stop_addr = self.stub_base + 0xF000

        for addr, size in (
            (self.stack_base, self.stack_size),
            (self.tls_base, self.tls_size),
            (self.arena_base, self.arena_size),
            (self.stub_base, self.stub_size),
        ):
            self.uc.mem_map(addr, size, UC_PROT_ALL)
        self.uc.mem_write(self.stop_addr, b"\xC3")

        self._arena_cursor = self.arena_base + 0x1000
        self._allocations = {}
        self._tls_next_slot = 0x10
        self._tls_values = {}
        self._stub_handlers = {}

        self._init_tls()
        self._install_patches_and_stubs()
        if self.memory_source is None:
            self._preload_pe_sections()
        else:
            self.uc.hook_add(
                UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED | UC_HOOK_MEM_FETCH_UNMAPPED,
                self._on_unmapped_access,
            )

    def _init_tls(self):
        tls_array = self.tls_base + 0x100
        tls_slot0 = self.tls_base + 0x1000
        self.uc.reg_write(UC_X86_REG_GS_BASE, self.tls_base)
        # Windows x64 TEB self-pointer at gs:[0x30] (used by NtCurrentTeb/SetLastError).
        self.uc.mem_write(self.tls_base + 0x30, struct.pack("<Q", self.tls_base))
        self.uc.mem_write(self.tls_base + 0x58, struct.pack("<Q", tls_array))
        self.uc.mem_write(tls_array, struct.pack("<Q", tls_slot0))
        self.uc.mem_write(tls_slot0 + 0x10, struct.pack("<I", INIT_THREAD_EPOCH_HIGH))

    def _install_patches_and_stubs(self):
        # Clear telemetry flags and DB registry lock on shadow pages.
        for rva in PROFILE_FLAG_RVAS:
            self._patched_bytes[self.base + rva] = b"\x00"
        self._patched_bytes[self.base + DB_REGISTRY_LOCK_RVA] = b"\x00\x00\x00\x00"
        # Force MSVC CRT ISA dispatch to SSE4.2 (Unicorn 2.x does not emulate AVX2).
        self._patched_bytes[self.base + MSVC_ISA_AVAILABLE_RVA] = struct.pack("<II", 2, 0x06)
        self._patched_qwords[self.base + CRT_HEAP_RVA] = 0xDEADBEEF

        stub_handlers = {
            "GetSystemTimeAsFileTime": self._iat_get_system_time,
            "GetProcessHeap": lambda: 0xDEADBEEF,
            "HeapAlloc": self._iat_heap_alloc,
            "HeapReAlloc": self._iat_heap_realloc,
            "HeapFree": self._iat_heap_free,
            "HeapSize": self._iat_heap_size,
            "VirtualAlloc": self._iat_virtual_alloc,
            "VirtualFree": lambda: 1,
            "VirtualProtect": lambda: 1,
            "TlsAlloc": self._iat_tls_alloc,
            "TlsFree": self._iat_tls_free,
            "TlsGetValue": self._iat_tls_get,
            "TlsSetValue": self._iat_tls_set,
            "EnterCriticalSection": lambda: None,
            "LeaveCriticalSection": lambda: None,
            "InitializeCriticalSectionAndSpinCount": lambda: 1,
            "InitializeCriticalSectionEx": lambda: 1,
            "DeleteCriticalSection": lambda: None,
            "TryEnterCriticalSection": lambda: 1,
            "InitializeSRWLock": lambda: None,
            "AcquireSRWLockShared": lambda: None,
            "ReleaseSRWLockShared": lambda: None,
            "AcquireSRWLockExclusive": lambda: None,
            "ReleaseSRWLockExclusive": lambda: None,
            "TryAcquireSRWLockExclusive": lambda: 1,
            "QueryPerformanceCounter": self._iat_qpc,
            "QueryPerformanceFrequency": self._iat_qpf,
            "GetCurrentThreadId": lambda: 1,
            "GetCurrentThread": lambda: 0xFFFFFFFFFFFFFFFE,
            "Sleep": lambda: None,
            "SleepEx": lambda: 0,
            "SetFilePointerEx": lambda: 1,
            "GetLastError": lambda: 0,
            "WriteFile": self._iat_write_file,
        }

        for index, (name, iat_rva) in enumerate(KERNEL32_IAT_RVAS.items()):
            stub_addr = self.stub_base + index * 0x10
            self.uc.mem_write(stub_addr, b"\xC3")
            self._patched_qwords[self.base + iat_rva] = stub_addr
            self._stub_handlers[stub_addr] = stub_handlers[name]

        for rva in CRT_MALLOC_THUNK_RVAS:
            addr = self.base + rva
            self._patched_bytes[addr] = b"\xC3"
            self._stub_handlers[addr] = self._crt_malloc
        for rva in CRT_FREE_THUNK_RVAS:
            addr = self.base + rva
            self._patched_bytes[addr] = b"\xC3"
            self._stub_handlers[addr] = self._crt_free
        for rva, elem_size in STD_FIND_TRIVIAL_RVAS:
            addr = self.base + rva
            self._patched_bytes[addr] = b"\xC3"
            self._stub_handlers[addr] = self._make_std_find_trivial_stub(elem_size)

        if self._stub_handlers:
            self.uc.hook_add(
                UC_HOOK_CODE,
                self._on_stub_code,
                begin=self.stub_base,
                end=self.stub_base + len(KERNEL32_IAT_RVAS) * 0x10,
            )
            for addr in [
                self.base + r
                for r in (*CRT_MALLOC_THUNK_RVAS, *CRT_FREE_THUNK_RVAS, *(r for r, _ in STD_FIND_TRIVIAL_RVAS))
            ]:
                self.uc.hook_add(UC_HOOK_CODE, self._on_stub_code, begin=addr, end=addr)

    def _preload_pe_sections(self):
        for section in self.pe.sections:
            size = max(section["virtual_size"], section["raw_size"])
            mapped = _align_up(size)
            addr = self.base + section["rva"]
            self.uc.mem_map(addr, mapped, UC_PROT_ALL)
            start = section["raw_offset"]
            raw = self.pe.data[start:start + section["raw_size"]]
            if raw:
                self.uc.mem_write(addr, raw)
            for page in range(addr, addr + mapped, PAGE_SIZE):
                self._cow_pages.add(page)
                self._apply_page_patches(page)

    def _pe_page_bytes(self, page):
        rva = page - self.base
        if not (0 <= rva < self.image_span):
            return None
        buf = bytearray(PAGE_SIZE)
        in_section = False
        for sec in self.pe.sections:
            s_start = sec["rva"]
            s_end = s_start + max(sec["virtual_size"], sec["raw_size"])
            lo = max(rva, s_start)
            hi = min(rva + PAGE_SIZE, s_end)
            if lo < hi:
                in_section = True
                raw_hi = min(hi, s_start + sec["raw_size"])
                if lo < raw_hi:
                    src = sec["raw_offset"] + (lo - s_start)
                    buf[lo - rva:raw_hi - rva] = self.pe.data[src:src + (raw_hi - lo)]
        if not in_section:
            return None
        if self._reloc_deltas:
            for patch_rva, delta in self._reloc_deltas.items():
                if rva <= patch_rva <= rva + PAGE_SIZE - 8:
                    off = patch_rva - rva
                    val = struct.unpack_from("<Q", buf, off)[0] + delta
                    struct.pack_into("<Q", buf, off, val & 0xFFFFFFFFFFFFFFFF)
        return bytes(buf)

    def _apply_page_patches(self, page):
        end = page + PAGE_SIZE
        for addr, value in self._patched_qwords.items():
            if page <= addr <= end - 8:
                self.uc.mem_write(addr, struct.pack("<Q", value))
        for addr, raw in self._patched_bytes.items():
            lo = max(page, addr)
            hi = min(end, addr + len(raw))
            if lo < hi:
                self.uc.mem_write(lo, raw[lo - addr:hi - addr])

    def _is_immutable_code_page(self, page):
        rva = page - self.base
        for sec in self.pe.sections:
            if sec["name"].startswith(".text") and sec["rva"] <= rva < sec["rva"] + sec["virtual_size"]:
                return True
        return False

    def _on_unmapped_access(self, uc, access, address, size, value, user_data):
        del uc, access, size, value, user_data
        page = address & ~(PAGE_SIZE - 1)
        if page < 0x10000:
            self._fault_error = MemoryError(f"Null/low page access at 0x{address:x}")
            return False
        if page in self._cow_pages:
            return True
        data = None
        if self.memory_source is not None:
            try:
                data = self.memory_source.read(page, PAGE_SIZE, fresh=True)
            except (OSError, MemoryError, ValueError):
                data = None
        if data is None:
            data = self._pe_page_bytes(page)
        if data is None:
            self._fault_error = MemoryError(f"Unmapped read/write at 0x{address:x}")
            return False
        self.uc.mem_map(page, PAGE_SIZE, UC_PROT_ALL)
        self.uc.mem_write(page, data[:PAGE_SIZE])
        self._cow_pages.add(page)
        if self._is_immutable_code_page(page):
            self._immutable_pages.add(page)
        self._apply_page_patches(page)
        return True

    def refresh_dynamic_state(self):
        """Drop mutable CoW shadow pages so subsequent evaluations pull fresh live memory."""
        if self.memory_source is None:
            return
        for page in list(self._cow_pages - self._immutable_pages):
            self.uc.mem_unmap(page, PAGE_SIZE)
            self._cow_pages.discard(page)
        # Re-apply thread-local / DB spinlock state cleanly.
        self._init_tls()

    def alloc(self, size, zero=True):
        size = max(1, int(size))
        aligned = (size + 15) & ~15
        if self._arena_cursor + aligned > self.arena_base + self.arena_size:
            raise MemoryError("Shadow evaluator arena exhausted")
        addr = self._arena_cursor
        self._arena_cursor += aligned
        self._allocations[addr] = size
        if zero:
            self.uc.mem_write(addr, b"\x00" * aligned)
        return addr

    def free(self, addr):
        self._allocations.pop(int(addr), None)

    def _on_stub_code(self, uc, address, size, user_data):
        del uc, size, user_data
        handler = self._stub_handlers.get(address)
        if handler is None:
            return
        result = handler()
        if result is not None:
            self.uc.reg_write(UC_X86_REG_RAX, result & 0xFFFFFFFFFFFFFFFF)

    def _crt_malloc(self):
        return self.alloc(self.uc.reg_read(UC_X86_REG_RCX), zero=False)

    def _crt_free(self):
        self.free(self.uc.reg_read(UC_X86_REG_RCX))
        return None

    def _iat_heap_alloc(self):
        flags = self.uc.reg_read(UC_X86_REG_RDX)
        size = self.uc.reg_read(UC_X86_REG_R8)
        return self.alloc(size, zero=bool(flags & 0x8))

    def _iat_heap_realloc(self):
        flags = self.uc.reg_read(UC_X86_REG_RDX)
        ptr = self.uc.reg_read(UC_X86_REG_R8)
        new_size = self.uc.reg_read(UC_X86_REG_R9)
        if not ptr:
            return self.alloc(new_size, zero=bool(flags & 0x8))
        new_ptr = self.alloc(new_size, zero=bool(flags & 0x8))
        old_size = self._allocations.get(ptr, new_size)
        copy_len = min(old_size, new_size)
        if copy_len > 0:
            self.uc.mem_write(new_ptr, bytes(self.uc.mem_read(ptr, copy_len)))
        self.free(ptr)
        return new_ptr

    def _iat_heap_free(self):
        self.free(self.uc.reg_read(UC_X86_REG_R8))
        return 1

    def _iat_heap_size(self):
        ptr = self.uc.reg_read(UC_X86_REG_R8)
        return self._allocations.get(ptr, 0xFFFFFFFFFFFFFFFF)

    def _iat_virtual_alloc(self):
        size = self.uc.reg_read(UC_X86_REG_RDX)
        return self.alloc(size, zero=True)

    def _iat_tls_alloc(self):
        slot = self._tls_next_slot
        self._tls_next_slot += 1
        self._tls_values[slot] = 0
        return slot

    def _iat_tls_free(self):
        slot = self.uc.reg_read(UC_X86_REG_RCX) & 0xFFFFFFFF
        self._tls_values.pop(slot, None)
        return 1

    def _iat_tls_get(self):
        slot = self.uc.reg_read(UC_X86_REG_RCX) & 0xFFFFFFFF
        return self._tls_values.get(slot, 0)

    def _iat_tls_set(self):
        slot = self.uc.reg_read(UC_X86_REG_RCX) & 0xFFFFFFFF
        self._tls_values[slot] = self.uc.reg_read(UC_X86_REG_RDX)
        return 1

    def _iat_qpc(self):
        out = self.uc.reg_read(UC_X86_REG_RCX)
        self._qpc_counter += 100
        if out:
            self.uc.mem_write(out, struct.pack("<Q", self._qpc_counter))
        return 1

    def _iat_qpf(self):
        out = self.uc.reg_read(UC_X86_REG_RCX)
        if out:
            self.uc.mem_write(out, struct.pack("<Q", 10_000_000))
        return 1

    def _iat_get_system_time(self):
        out = self.uc.reg_read(UC_X86_REG_RCX)
        if out:
            self.ensure_mapped(out, 8)
            self.uc.mem_write(out, struct.pack("<Q", 133_000_000_000_000_000))
        return None

    def _iat_write_file(self):
        count = self.uc.reg_read(UC_X86_REG_R8) & 0xFFFFFFFF
        written_ptr = self.uc.reg_read(UC_X86_REG_R9)
        if written_ptr:
            self.ensure_mapped(written_ptr, 4)
            self.uc.mem_write(written_ptr, struct.pack("<I", count))
        return 1

    def _make_std_find_trivial_stub(self, elem_size):
        fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[elem_size]

        def _stub():
            begin = self.uc.reg_read(UC_X86_REG_R8)
            end = self.uc.reg_read(UC_X86_REG_R9)
            if end <= begin:
                return end
            rsp = self.uc.reg_read(UC_X86_REG_RSP)
            needle = struct.unpack(fmt, bytes(self.uc.mem_read(rsp + 0x28, elem_size)))[0]
            span = end - begin
            self.ensure_mapped(begin, span)
            raw = bytes(self.uc.mem_read(begin, span))
            for off in range(0, len(raw) - elem_size + 1, elem_size):
                if struct.unpack_from(fmt, raw, off)[0] == needle:
                    return begin + off
            return end

        return _stub

    def ensure_mapped(self, addr, size=1):
        """Fault in lazy CoW pages covering [addr, addr + size) before host mem_write."""
        if self.memory_source is None or size <= 0:
            return
        start_page = addr & ~(PAGE_SIZE - 1)
        end_page = (addr + size - 1) & ~(PAGE_SIZE - 1)
        for page in range(start_page, end_page + PAGE_SIZE, PAGE_SIZE):
            if page in self._cow_pages:
                continue
            if (
                self.stack_base <= page < self.stack_base + self.stack_size
                or self.tls_base <= page < self.tls_base + self.tls_size
                or self.arena_base <= page < self.arena_base + self.arena_size
                or self.stub_base <= page < self.stub_base + self.stub_size
            ):
                continue
            if not self._on_unmapped_access(self.uc, 0, page, PAGE_SIZE, 0, None):
                raise self._fault_error or MemoryError(f"Unmapped host write at 0x{addr:x}")

    def write_bytes(self, addr, data):
        self.ensure_mapped(addr, len(data))
        self.uc.mem_write(addr, data)

    def write_u16(self, addr, val):
        self.write_bytes(addr, struct.pack("<H", val & 0xFFFF))

    def write_u32(self, addr, val):
        self.write_bytes(addr, struct.pack("<I", val & 0xFFFFFFFF))

    def write_i32(self, addr, val):
        self.write_bytes(addr, struct.pack("<i", val))

    def write_u64(self, addr, val):
        self.write_bytes(addr, struct.pack("<Q", val & 0xFFFFFFFFFFFFFFFF))

    def read_i64(self, addr):
        return struct.unpack("<q", self.uc.mem_read(addr, 8))[0]

    def read_u32(self, addr):
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def read_u64(self, addr):
        return struct.unpack("<Q", self.uc.mem_read(addr, 8))[0]

    def call(self, fn_rva, rcx=0, rdx=0, r8=0, r9=0, stack_args=()):
        """Invoke a Microsoft x64 ABI function at base + fn_rva and return RAX."""
        self._fault_error = None
        rsp = self.stack_base + self.stack_size - 0x100
        self.uc.reg_write(UC_X86_REG_RSP, rsp)
        self.uc.mem_write(rsp, struct.pack("<Q", self.stop_addr))
        for index, value in enumerate(stack_args):
            self.uc.mem_write(rsp + 0x28 + index * 8, struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))
        self.uc.reg_write(UC_X86_REG_RCX, rcx)
        self.uc.reg_write(UC_X86_REG_RDX, rdx)
        self.uc.reg_write(UC_X86_REG_R8, r8)
        self.uc.reg_write(UC_X86_REG_R9, r9)
        try:
            self.uc.emu_start(self.base + fn_rva, self.stop_addr, count=self.max_instructions)
        except UcError as error:
            if self._fault_error is not None:
                raise RuntimeError(f"Shadow VM memory fault during 0x{fn_rva:x}: {self._fault_error}") from error
            raise RuntimeError(
                f"Shadow VM execution error at rip=0x{self.uc.reg_read(UC_X86_REG_RIP):x}: {error}"
            ) from error
        return self.uc.reg_read(UC_X86_REG_RAX)


class ShadowEvaluator:
    """High-level evaluator invoking CK3 AI acceptance logic in ShadowProcessVM."""

    def __init__(self, executable=None, *, memory_source=None, verify_exe_hash=True):
        self.vm = ShadowProcessVM(executable, memory_source=memory_source, verify_exe_hash=verify_exe_hash)
        self._scope_ids = dict(DEFAULT_SCOPE_TYPE_IDS)
        self._scope_table_loaded = False

    def _init_top_scope(self, addr, identity, scope_type="character"):
        self.vm.write_u16(addr, self._scope_type_id(scope_type))
        self.vm.write_u64(addr + 0x08, identity)
        self.vm.write_u32(addr + 0x10, identity)
        self.vm.call(TOPSCOPE_SAVED_INIT_RVA, rcx=addr + 0x18)
        # Initialize scope variable map/list headers to empty containers matching TopScope::ctor.
        self.vm.write_u64(addr + 0x100 + 0x10, self.vm.base + TOPSCOPE_MAP_VT_RVA)
        list_addr = addr + 0x118
        self.vm.write_u64(list_addr + 0x00, self.vm.base + TOPSCOPE_LIST_VT_0_RVA)
        self.vm.write_u64(list_addr + 0x08, self.vm.base + TOPSCOPE_LIST_VT_1_RVA)
        self.vm.write_u64(list_addr + 0x20, self.vm.base + TOPSCOPE_LIST_ALLOC_RVA)
        self.vm.write_i32(list_addr + 0x48, -1)

    def _scope_type_id(self, name):
        if name in self._scope_ids:
            return self._scope_ids[name]
        if not self._scope_table_loaded:
            self._load_scope_type_ids()
        if name not in self._scope_ids:
            raise ValueError(f"Unsupported or unloaded scope type {name!r}")
        return self._scope_ids[name]

    def _load_scope_type_ids(self):
        self._scope_table_loaded = True
        token_to_names = {
            0x2EA6: ("char", "character"),
            0x0500: ("boolean",),
            0x04FF: ("prov", "province"),
            0x2EA7: ("lt", "landed_title"),
            0x3594: ("artifact",),
            0x2B3F: ("war",),
            0x3688: ("court_position",),
            0x3C0C: ("court_position_type",),
            0x315F: ("council_task",),
        }
        try:
            table_ptr = self.vm.call(SCOPE_TYPE_ARRAY_FUNC_RVA)
            mem = self.vm.memory_source
            if mem is not None:
                data_ptr = struct.unpack("<Q", mem.read(table_ptr, 8))[0]
                count = struct.unpack("<I", mem.read(table_ptr + 0x0C, 4))[0]
            else:
                data_ptr = self.vm.read_u64(table_ptr)
                count = self.vm.read_u32(table_ptr + 0x0C)
            if not data_ptr or count == 0 or count > 512:
                return
            for idx in range(count):
                token = (struct.unpack("<I", mem.read(data_ptr + idx * 0x50 + 4, 4))[0]
                         if mem is not None else self.vm.read_u32(data_ptr + idx * 0x50 + 4))
                if token in token_to_names:
                    for scope_name in token_to_names[token]:
                        self._scope_ids[scope_name] = idx
        except (RuntimeError, MemoryError, OSError):
            pass

    def interaction_type_pointer(self, interaction_name):
        """Look up live CCharacterInteractionType* via CK3's database registry."""
        raw_name = interaction_name.encode("utf-8") + b"\x00"
        name_addr = self.vm.alloc(len(raw_name), zero=False)
        try:
            self.vm.write_bytes(name_addr, raw_name)
            self.vm.write_u32(self.vm.base + DB_REGISTRY_LOCK_RVA, 0)
            ptr = self.vm.call(DB_LOOKUP_RVA, rcx=self.vm.base + INTERACTION_TYPE_INFO_RVA, rdx=name_addr)
        finally:
            self.vm.free(name_addr)
        if not ptr:
            raise ValueError(f"Interaction definition {interaction_name!r} was not found in CK3 memory")
        return ptr

    def evaluate_interaction_acceptance(
        self,
        actor,
        recipient,
        interaction_name,
        *,
        secondary_actor=0xFFFFFFFF,
        secondary_recipient=0xFFFFFFFF,
        intermediary=0xFFFFFFFF,
        puppet=0xFFFFFFFF,
        target=None,
        options=(),
        refresh=True,
    ):
        """Evaluate native ai_accept and ai_intermediary_accept in ShadowProcessVM."""
        if refresh:
            self.vm.refresh_dynamic_state()

        def_ptr = self.interaction_type_pointer(interaction_name)
        interaction_ptr = self.vm.alloc(INTERACTION_STRUCT_SIZE, zero=True)
        out_ptr = self.vm.alloc(8, zero=True)
        options_ptr = 0
        try:
            end_war_target = (
                interaction_name in (
                    "end_war_attacker_victory_interaction",
                    "end_war_white_peace_interaction",
                    "end_war_attacker_defeat_interaction",
                )
                and isinstance(target, dict)
                and target.get("type") == "war"
            )
            self.vm.write_u64(interaction_ptr + OFF_INTERACTION_DEF, def_ptr)
            if end_war_target:
                self._init_top_scope(interaction_ptr + OFF_PRIMARY_SCOPE, int(target["identity"]), "war")
            else:
                self._init_top_scope(interaction_ptr + OFF_PRIMARY_SCOPE, actor)
            self._init_top_scope(interaction_ptr + OFF_SECONDARY_SCOPE, actor)
            self.vm.write_u32(interaction_ptr + OFF_ACTOR, actor)
            self.vm.write_u32(interaction_ptr + OFF_RECIPIENT, recipient)
            self.vm.write_u32(interaction_ptr + OFF_SECONDARY_ACTOR, secondary_actor)
            self.vm.write_u32(interaction_ptr + OFF_SECONDARY_RECIPIENT, secondary_recipient)
            self.vm.write_u32(interaction_ptr + OFF_INTERMEDIARY, intermediary)
            self.vm.write_u32(interaction_ptr + OFF_PUPPET, puppet)

            if target is not None and not end_war_target:
                scope_type = target["type"]
                identity = int(target["identity"])
                self.vm.write_u16(interaction_ptr + OFF_TARGET_SCOPE, self._scope_type_id(scope_type))
                self.vm.write_u64(interaction_ptr + OFF_TARGET_SCOPE + 8, identity)

            option_count = max(len(options), 32)
            flags_bytes = bytes(1 if flag else 0 for flag in options).ljust(option_count, b"\x00")
            options_ptr = self.vm.alloc(option_count, zero=False)
            self.vm.write_bytes(options_ptr, flags_bytes)
            self.vm.write_u64(interaction_ptr + OFF_OPTIONS_PTR, options_ptr)
            self.vm.write_i32(interaction_ptr + OFF_OPTIONS_CAP, option_count)
            self.vm.write_i32(interaction_ptr + OFF_OPTIONS_LEN, len(options) if options else option_count)

            if self.vm.memory_source is not None:
                try:
                    self.vm.call(INTERACTION_POPULATE_SCOPES_RVA, rcx=interaction_ptr, rdx=0)
                except (RuntimeError, MemoryError):
                    pass

            self.vm.call(CALC_AI_ACCEPT_RVA, rcx=interaction_ptr, rdx=out_ptr)
            ai_accept = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE

            self.vm.write_u64(out_ptr, 0)
            self.vm.call(CALC_AI_INTERMEDIARY_ACCEPT_RVA, rcx=interaction_ptr, rdx=out_ptr)
            ai_intermediary_accept = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
        finally:
            if options_ptr:
                self.vm.free(options_ptr)
            self.vm.free(out_ptr)
            self.vm.free(interaction_ptr)

        return {
            "ai_accept": ai_accept,
            "ai_intermediary_accept": ai_intermediary_accept,
            "source": "native_shadow_unicorn",
        }

    def evaluate_siege_progress(self, siege_ptr, *, refresh=False):
        """Evaluate native siege required points and progress percent."""
        if refresh:
            self.vm.refresh_dynamic_state()
        out_ptr = self.vm.alloc(8, zero=True)
        try:
            self.vm.call(SIEGE_REQUIRED_POINTS_RVA, rcx=siege_ptr, rdx=out_ptr)
            points_required = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
            self.vm.write_u64(out_ptr, 0)
            self.vm.call(SIEGE_PROGRESS_FRACTION_RVA, rcx=siege_ptr, rdx=out_ptr)
            fraction = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
        finally:
            self.vm.free(out_ptr)
        return {
            "points_required": points_required,
            "percent": round(fraction * 100.0, 4),
            "source": "native_shadow_unicorn",
        }

    def evaluate_construction_timing(self, construction_ptr, province_ptr, current_date=None, *, refresh=False):
        """Evaluate native construction end_date, days_remaining and progress_percent."""
        if refresh:
            self.vm.refresh_dynamic_state()
        out_ptr = self.vm.alloc(8, zero=True)
        try:
            self.vm.call(CONSTRUCTION_END_DATE_RVA, rcx=construction_ptr, rdx=out_ptr, r8=province_ptr)
            end_date = self.vm.read_u32(out_ptr)
            self.vm.write_u64(out_ptr, 0)
            self.vm.call(CONSTRUCTION_PROGRESS_PERCENT_RVA, rcx=construction_ptr, rdx=out_ptr, r8=province_ptr)
            progress_percent = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
        finally:
            self.vm.free(out_ptr)
        days_remaining = None
        if current_date is not None:
            days_remaining = max(0, (end_date - int(current_date) + 23) // 24)
        return {
            "end_date": end_date,
            "days_remaining": days_remaining,
            "progress_percent": progress_percent,
            "source": "native_shadow_unicorn",
        }

    def evaluate_unit_movement(self, unit_ptr, *, refresh=False):
        """Evaluate native unit leg cost, progress fraction, lock state and cached-speed ETA."""
        if refresh:
            self.vm.refresh_dynamic_state()
        out_ptr = self.vm.alloc(8, zero=True)
        frame_ptr = self.vm.alloc(0x300, zero=True)
        try:
            self.vm.call(UNIT_LEG_COST_RVA, rcx=unit_ptr, rdx=out_ptr)
            cost_raw = self.vm.read_i64(out_ptr)
            self.vm.write_u64(out_ptr, 0)
            self.vm.call(UNIT_LEG_PROGRESS_RVA, rcx=unit_ptr, rdx=out_ptr)
            progress_raw = self.vm.read_i64(out_ptr)
            self.vm.ensure_mapped(self.vm.base + MOVEMENT_LOCK_THRESHOLD_RVA, 8)
            lock_raw = self.vm.read_i64(self.vm.base + MOVEMENT_LOCK_THRESHOLD_RVA)
            movement_locked = progress_raw > lock_raw

            self.vm.ensure_mapped(unit_ptr + 0x190, 8)
            speed_raw = self.vm.read_i64(unit_ptr + 0x190)
            eta_days = None
            if cost_raw > 0 and speed_raw > 0:
                self.vm.write_bytes(frame_ptr + 0x2B8, struct.pack("<q", cost_raw))
                rsp = self.vm.stack_base + self.vm.stack_size - 0x100
                self.vm.uc.reg_write(UC_X86_REG_RBP, frame_ptr)
                self.vm.uc.reg_write(UC_X86_REG_RBX, unit_ptr)
                self.vm.uc.reg_write(UC_X86_REG_R13, 0)
                self.vm.uc.reg_write(UC_X86_REG_RSP, rsp)
                self.vm.uc.emu_start(
                    self.vm.base + UNIT_ETA_BRANCH_START_RVA,
                    self.vm.base + UNIT_ETA_BRANCH_END_RVA,
                    count=2000,
                )
                eta_days = self.vm.uc.reg_read(UC_X86_REG_RDX)
        finally:
            self.vm.free(frame_ptr)
            self.vm.free(out_ptr)
        return {
            "leg_movement_cost": cost_raw / FIXED_POINT_SCALE,
            "leg_progress_fraction": progress_raw / FIXED_POINT_SCALE,
            "movement_locked": movement_locked,
            "estimated_next_arrival_days": eta_days,
            "source": "native_shadow_unicorn",
        }

    def evaluate_regiment_strength(self, regiment_ptr, *, refresh=False):
        """Evaluate native CRegiment current and maximum soldier counts."""
        if refresh:
            self.vm.refresh_dynamic_state()
        self.vm.ensure_mapped(regiment_ptr, 0x130)
        maximum = self.vm.read_u32(regiment_ptr + 0x128)
        rsp = self.vm.stack_base + self.vm.stack_size - 0x100
        self.vm.uc.reg_write(UC_X86_REG_RSP, rsp)
        self.vm.uc.mem_write(rsp, struct.pack("<Q", self.vm.stop_addr))
        self.vm.uc.reg_write(UC_X86_REG_RAX, regiment_ptr)
        self.vm.uc.emu_start(
            self.vm.base + REGIMENT_CURRENT_SOLDIERS_RVA,
            self.vm.base + REGIMENT_CURRENT_SOLDIERS_END_RVA,
            count=2000,
        )
        current = self.vm.uc.reg_read(UC_X86_REG_RAX) & 0xFFFFFFFF
        return {"current": current, "maximum": maximum, "source": "native_shadow_unicorn"}

    def evaluate_mercenary_strength(self, mercenary_ptr, *, refresh=False):
        """Evaluate native CMercenaryCompany current and maximum soldier counts."""
        if refresh:
            self.vm.refresh_dynamic_state()
        current = self.vm.call(MERC_CURRENT_SOLDIERS_RVA, rcx=mercenary_ptr) & 0xFFFFFFFF
        maximum = self.vm.call(MERC_MAX_SOLDIERS_RVA, rcx=mercenary_ptr) & 0xFFFFFFFF
        return {"current": current, "maximum": maximum, "source": "native_shadow_unicorn"}

    def evaluate_siege_projection(self, siege_ptr, *, commander_id=0xFFFFFFFF, current_date=None, refresh=False):
        """Evaluate native CSiege daily progress (0x2527A50) and remaining days/date."""
        if refresh:
            self.vm.refresh_dynamic_state()
        out_ptr = self.vm.alloc(8, zero=True)
        try:
            self.vm.call(
                SIEGE_DAILY_PROGRESS_RVA,
                rcx=siege_ptr,
                rdx=out_ptr,
                r8=commander_id & 0xFFFFFFFF,
                r9=0,
                stack_args=(0,),
            )
            points_per_day = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
            self.vm.write_u64(out_ptr, 0)
            self.vm.call(SIEGE_REQUIRED_POINTS_RVA, rcx=siege_ptr, rdx=out_ptr)
            points_required = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE
            self.vm.ensure_mapped(siege_ptr + 0x3D0, 8)
            progress_points = self.vm.read_i64(siege_ptr + 0x3D0) / FIXED_POINT_SCALE
        finally:
            self.vm.free(out_ptr)
        estimated_days = None
        estimated_date = None
        if points_per_day > 0:
            remaining_points = max(0.0, points_required - progress_points)
            estimated_days = math.ceil(remaining_points / points_per_day)
            if current_date is not None:
                estimated_date = int(current_date) + estimated_days * 24
        return {
            "status": "native_shadow_unicorn",
            "points_per_day": points_per_day,
            "sample_game_days": None,
            "estimated_days_remaining": estimated_days,
            "estimated_completion_date": estimated_date,
        }

    def evaluate_combat_casualties(self, side_ptr, *, refresh=False):
        """Evaluate native CCombatResultSide total casualties via CCombatResultRegiment::GetTotalLosses."""
        if refresh:
            self.vm.refresh_dynamic_state()
        self.vm.ensure_mapped(side_ptr + 0x38, 16)
        data_ptr = self.vm.read_u64(side_ptr + 0x38)
        capacity = self.vm.read_u32(side_ptr + 0x40)
        count = struct.unpack("<i", struct.pack("<I", self.vm.read_u32(side_ptr + 0x44)))[0]
        if count < 0 or count > capacity or count > 4096 or (count and not data_ptr):
            raise ValueError("Invalid CCombatResultSide regiment array")
        total_raw = 0
        rsp = self.vm.stack_base + self.vm.stack_size - 0x100
        for index in range(count):
            reg_ptr = data_ptr + index * 0x50
            self.vm.ensure_mapped(reg_ptr + 0x28, 24)
            self.vm.uc.reg_write(UC_X86_REG_RCX, reg_ptr)
            self.vm.uc.reg_write(UC_X86_REG_RSP, rsp)
            self.vm.uc.emu_start(
                self.vm.base + COMBAT_REGIMENT_LOSSES_RVA,
                self.vm.base + COMBAT_REGIMENT_LOSSES_END_RVA,
                count=64,
            )
            losses_raw = struct.unpack("<q", struct.pack("<Q", self.vm.uc.reg_read(UC_X86_REG_RAX)))[0]
            total_raw += losses_raw
        return {
            "casualties": total_raw / FIXED_POINT_SCALE,
            "regiment_count": count,
            "source": "native_shadow_unicorn",
        }

    def evaluate_province_supply_limit(self, province_ptr, character_ptr, *, refresh=False):
        """Evaluate native CProvince::GetSupplyLimitFor (0x2482F30) for a character."""
        if refresh:
            self.vm.refresh_dynamic_state()
        raw = self.vm.call(
            PROVINCE_SUPPLY_LIMIT_RVA,
            rcx=province_ptr,
            rdx=character_ptr,
            r8=character_ptr,
            r9=0,
            stack_args=(0,),
        )
        supply_limit = struct.unpack("<i", struct.pack("<I", raw & 0xFFFFFFFF))[0]
        return {"supply_limit": supply_limit, "source": "native_shadow_unicorn"}

    def _lookup_db_entry(self, type_info_rva, name):
        raw_name = name.encode("utf-8") + b"\x00"
        name_addr = self.vm.alloc(len(raw_name), zero=False)
        try:
            self.vm.write_bytes(name_addr, raw_name)
            self.vm.write_u32(self.vm.base + DB_REGISTRY_LOCK_RVA, 0)
            return self.vm.call(DB_LOOKUP_RVA, rcx=self.vm.base + type_info_rva, rdx=name_addr)
        finally:
            self.vm.free(name_addr)

    def evaluate_decision(self, character_id, character_ptr, decision_name, *, refresh=True):
        """Evaluate native CDecision::IsShown, IsValid, CanAffordCost and CalculateCost."""
        if refresh:
            self.vm.refresh_dynamic_state()
        dec_ptr = self._lookup_db_entry(DECISION_TYPE_INFO_RVA, decision_name)
        if not dec_ptr:
            raise ValueError(f"Decision definition {decision_name!r} was not found in CK3 memory")
        shown = bool(self.vm.call(DECISION_IS_SHOWN_RVA, rcx=dec_ptr, rdx=character_ptr) & 0xFF)
        scope_ptr = self.vm.alloc(0x180, zero=True)
        cost_out = self.vm.alloc(0x60, zero=True)
        try:
            self._init_top_scope(scope_ptr, character_id)
            valid = bool(
                self.vm.call(
                    DECISION_IS_VALID_RVA,
                    rcx=dec_ptr,
                    rdx=character_ptr,
                    r8=scope_ptr,
                    r9=0,
                    stack_args=(0,),
                ) & 0xFF
            )
            cost_ptr = self.vm.call(DECISION_GET_COST_RVA, rcx=dec_ptr)
            can_afford = bool(
                self.vm.call(
                    COST_CAN_AFFORD_RVA,
                    rcx=cost_ptr,
                    rdx=scope_ptr,
                    r8=character_ptr,
                    r9=0,
                ) & 0xFF
            )
            self.vm.call(COST_CALCULATE_RVA, rcx=cost_ptr, rdx=scope_ptr, r8=cost_out)
            values = struct.unpack("<10q", self.vm.uc.mem_read(cost_out, 80))
        finally:
            self.vm.free(scope_ptr)
            self.vm.free(cost_out)
        cost = {
            "gold": values[0] / FIXED_POINT_SCALE,
            "prestige": values[1] / FIXED_POINT_SCALE,
            "piety": values[2] / FIXED_POINT_SCALE,
            "treasury": values[7] / FIXED_POINT_SCALE,
        }
        for idx, name in enumerate(COST_RESOURCE_NAMES):
            amount = values[idx] / FIXED_POINT_SCALE
            if amount != 0.0:
                cost[name] = amount
        return {
            "shown": shown,
            "valid": valid,
            "can_afford": can_afford,
            "available": shown and valid and can_afford,
            "cost": cost,
            "source": "native_shadow_unicorn",
        }

    def evaluate_activity_type(self, character_id, character_ptr, activity_type_name, *, check_cooldown=True, refresh=True):
        """Evaluate native CActivityType is_shown (+0x40), CanStart (0x2BCBEB0) and base CCost (+0x1368)."""
        if refresh:
            self.vm.refresh_dynamic_state()
        act_ptr = self._lookup_db_entry(ACTIVITY_TYPE_INFO_RVA, activity_type_name)
        if not act_ptr:
            raise ValueError(f"Activity type {activity_type_name!r} was not found in CK3 memory")
        scope_ptr = self.vm.alloc(0x180, zero=True)
        cost_out = self.vm.alloc(0x60, zero=True)
        try:
            self._init_top_scope(scope_ptr, character_id)
            self.vm.ensure_mapped(act_ptr + ACTIVITY_TYPE_IS_SHOWN_OFFSET + 0x4C, 4)
            shown_cnt = self.vm.read_u32(act_ptr + ACTIVITY_TYPE_IS_SHOWN_OFFSET + 0x4C)
            shown = True if shown_cnt == 0 else bool(
                self.vm.call(
                    TRIGGER_IS_FULFILLED_RVA,
                    rcx=act_ptr + ACTIVITY_TYPE_IS_SHOWN_OFFSET,
                    rdx=scope_ptr,
                ) & 0xFF
            )
            can_start = bool(
                self.vm.call(
                    ACTIVITY_TYPE_CAN_START_RVA,
                    rcx=act_ptr,
                    rdx=scope_ptr,
                    r8=0,
                    r9=1 if check_cooldown else 0,
                ) & 0xFF
            )
            cost_ptr = act_ptr + ACTIVITY_TYPE_COST_OFFSET
            can_afford = bool(
                self.vm.call(
                    COST_CAN_AFFORD_RVA,
                    rcx=cost_ptr,
                    rdx=scope_ptr,
                    r8=character_ptr,
                    r9=0,
                ) & 0xFF
            )
            self.vm.call(COST_CALCULATE_RVA, rcx=cost_ptr, rdx=scope_ptr, r8=cost_out)
            values = struct.unpack("<10q", self.vm.uc.mem_read(cost_out, 80))
        finally:
            self.vm.free(scope_ptr)
            self.vm.free(cost_out)
        cost = {
            "gold": values[0] / FIXED_POINT_SCALE,
            "prestige": values[1] / FIXED_POINT_SCALE,
            "piety": values[2] / FIXED_POINT_SCALE,
            "treasury": values[7] / FIXED_POINT_SCALE,
        }
        for idx, name in enumerate(COST_RESOURCE_NAMES):
            amount = values[idx] / FIXED_POINT_SCALE
            if amount != 0.0:
                cost[name] = amount
        return {
            "shown": shown,
            "available": shown and can_start and can_afford,
            "can_start": can_start,
            "can_afford": can_afford,
            "cost": cost,
            "source": "native_shadow_unicorn",
        }

    def evaluate_construction_quote(
        self,
        character_id,
        province_id,
        province_ptr,
        building_ptr,
        *,
        slot_index=None,
        use_treasury=False,
        refresh=True,
    ):
        """Evaluate native building gold/prestige/piety cost, construction days and validity."""
        if refresh:
            self.vm.refresh_dynamic_state()
        self.vm.ensure_mapped(building_ptr + 0x58, 4)
        self.vm.ensure_mapped(building_ptr + 0x6F510, 32)
        base_days = struct.unpack("<i", struct.pack("<I", self.vm.read_u32(building_ptr + 0x58)))[0]
        flags = self.vm.read_u64(building_ptr + 0x6F510)
        base_gold = self.vm.read_i64(building_ptr + 0x6F518)
        base_prestige = self.vm.read_i64(building_ptr + 0x6F520)
        base_piety = self.vm.read_i64(building_ptr + 0x6F528)
        out_ptr = self.vm.alloc(16, zero=True)
        try:
            self.vm.write_u64(out_ptr, 0)
            self.vm.call(
                BUILDING_GOLD_COST_RVA,
                rcx=out_ptr,
                rdx=province_ptr,
                r8=base_gold & 0xFFFFFFFFFFFFFFFF,
                r9=flags,
                stack_args=(3 if use_treasury else 0, 0),
            )
            gold_or_treasury = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE

            self.vm.write_u64(out_ptr, 0)
            self.vm.call(
                BUILDING_PRESTIGE_COST_RVA,
                rcx=out_ptr,
                rdx=province_ptr,
                r8=base_prestige & 0xFFFFFFFFFFFFFFFF,
                r9=flags,
                stack_args=(0,),
            )
            prestige = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE

            self.vm.write_u64(out_ptr, 0)
            self.vm.call(
                BUILDING_PIETY_COST_RVA,
                rcx=out_ptr,
                rdx=province_ptr,
                r8=base_piety & 0xFFFFFFFFFFFFFFFF,
                r9=flags,
                stack_args=(0,),
            )
            piety = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE

            self.vm.write_u64(out_ptr, 0)
            self.vm.call(
                BUILDING_CONSTRUCTION_TIME_RVA,
                rcx=out_ptr,
                rdx=province_ptr,
                r8=(base_days * 100000) & 0xFFFFFFFFFFFFFFFF,
                r9=flags,
                stack_args=(0,),
            )
            construction_days = self.vm.read_i64(out_ptr) / FIXED_POINT_SCALE

            can_construct = None
            can_construct_ignoring_cost = None
            if slot_index is not None:
                can_construct = bool(
                    self.vm.call(
                        BUILDING_CAN_CONSTRUCT_RVA,
                        rcx=character_id & 0xFFFFFFFF,
                        rdx=province_id & 0xFFFFFFFF,
                        r8=building_ptr,
                        r9=slot_index & 0xFFFFFFFF,
                        stack_args=(1, 0),
                    ) & 0xFF
                )
                can_construct_ignoring_cost = bool(
                    self.vm.call(
                        BUILDING_CAN_CONSTRUCT_RVA,
                        rcx=character_id & 0xFFFFFFFF,
                        rdx=province_id & 0xFFFFFFFF,
                        r8=building_ptr,
                        r9=slot_index & 0xFFFFFFFF,
                        stack_args=(0, 0),
                    ) & 0xFF
                )
        finally:
            self.vm.free(out_ptr)
        return {
            "gold_or_treasury": gold_or_treasury,
            "prestige": prestige,
            "piety": piety,
            "construction_days": construction_days,
            "can_construct": can_construct,
            "can_construct_ignoring_cost": can_construct_ignoring_cost,
            "source": "native_shadow_unicorn",
        }

    def evaluate_event_options(self, event_data_ptr, *, refresh=True):
        """Evaluate native CEventOption trigger and show_as_unavailable against CEventData's CTopScope."""
        if refresh:
            self.vm.refresh_dynamic_state()
        self.vm.ensure_mapped(event_data_ptr + 0x1B0, 8)
        event_def = self.vm.read_u64(event_data_ptr + 0x1B0)
        if not event_def:
            return []
        self.vm.ensure_mapped(event_def + 0x1A0, 16)
        opt_arr = self.vm.read_u64(event_def + 0x1A0)
        opt_count = self.vm.read_u32(event_def + 0x1AC)
        if opt_count == 0 or opt_count > 64 or not opt_arr:
            return []
        self.vm.ensure_mapped(opt_arr, opt_count * 8)
        results = []
        for idx in range(opt_count):
            opt_ptr = self.vm.read_u64(opt_arr + idx * 8)
            if not opt_ptr:
                results.append({"index": idx, "available": None, "shown": None,
                                "show_as_unavailable": None, "availability": "requires_game_evaluation"})
                continue
            self.vm.ensure_mapped(opt_ptr + EVENT_OPTION_TRIGGER_OFFSET + 0x4C, 4)
            self.vm.ensure_mapped(opt_ptr + EVENT_OPTION_UNAVAILABLE_OFFSET + 0x4C, 4)
            trig_cnt = self.vm.read_u32(opt_ptr + EVENT_OPTION_TRIGGER_OFFSET + 0x4C)
            unav_cnt = self.vm.read_u32(opt_ptr + EVENT_OPTION_UNAVAILABLE_OFFSET + 0x4C)
            available = True if trig_cnt == 0 else bool(
                self.vm.call(TRIGGER_IS_FULFILLED_RVA, rcx=opt_ptr + EVENT_OPTION_TRIGGER_OFFSET, rdx=event_data_ptr) & 0xFF
            )
            show_as_unavailable = False if unav_cnt == 0 else bool(
                self.vm.call(TRIGGER_IS_FULFILLED_RVA, rcx=opt_ptr + EVENT_OPTION_UNAVAILABLE_OFFSET, rdx=event_data_ptr) & 0xFF
            )
            results.append({
                "index": idx,
                "available": available,
                "show_as_unavailable": show_as_unavailable,
                "shown": available or show_as_unavailable,
                "availability": "native_shadow_unicorn",
            })
        return results

    def evaluate_activity_join(self, activity_ptr, character_id, *, refresh=True):
        """Evaluate native CActivity::CanJoin (0x2BCB140) for a character."""
        if refresh:
            self.vm.refresh_dynamic_state()
        can_join = bool(
            self.vm.call(
                ACTIVITY_CAN_JOIN_RVA,
                rcx=activity_ptr,
                rdx=character_id & 0xFFFFFFFF,
                r8=0,
                r9=0,
            ) & 0xFF
        )
        return {"can_join": can_join, "source": "native_shadow_unicorn"}

    def evaluate_pending_interaction_replies(self, pending_ptr, *, refresh=True):
        """Evaluate native reply button availability (accept/reject/block/acknowledge) per 0x29751C0."""
        if refresh:
            self.vm.refresh_dynamic_state()
        data_ptr = pending_ptr + 0x18
        self.vm.ensure_mapped(data_ptr, 0x2E8)
        def_ptr = self.vm.read_u64(data_ptr)
        actor = self.vm.read_u32(data_ptr + 0x2D8)
        recipient = self.vm.read_u32(data_ptr + 0x2DC)
        base_ok = bool(self.vm.call(INTERACTION_DATA_BASE_VALID_RVA, rcx=data_ptr) & 0xFF)
        still_ok = bool(
            self.vm.call(INTERACTION_DATA_STILL_VALID_RVA, rcx=data_ptr, rdx=0, r8=0, r9=0) & 0xFF
        ) if base_ok else False
        accept_ok = base_ok and still_ok
        if accept_ok and actor != recipient and def_ptr:
            self.vm.ensure_mapped(def_ptr + INTERACTION_AUTO_ACCEPT_TRIGGER_OFFSET, 8)
            auto_trig = self.vm.read_u64(def_ptr + INTERACTION_AUTO_ACCEPT_TRIGGER_OFFSET)
            if auto_trig:
                auto_accept = bool(
                    self.vm.call(TRIGGER_IS_FULFILLED_RVA, rcx=auto_trig, rdx=data_ptr + 8) & 0xFF
                )
            else:
                self.vm.ensure_mapped(def_ptr + INTERACTION_AUTO_ACCEPT_FLAG_OFFSET, 1)
                auto_accept = bool(self.vm.uc.mem_read(def_ptr + INTERACTION_AUTO_ACCEPT_FLAG_OFFSET, 1)[0])
            reject_ok = not auto_accept
            block_ok = reject_ok and bool(
                self.vm.call(
                    TRIGGER_IS_FULFILLED_RVA,
                    rcx=def_ptr + INTERACTION_BLOCK_TRIGGER_OFFSET,
                    rdx=data_ptr + 8,
                ) & 0xFF
            )
        else:
            reject_ok = False
            block_ok = False
        return {
            "accept": accept_ok,
            "reject": reject_ok,
            "block": block_ok,
            "acknowledge": True,
            "source": "native_shadow_unicorn",
        }
