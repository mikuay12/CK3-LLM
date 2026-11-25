//! Bindings to the C exports of the Nakama SDK shipped with CK3 on Windows x64.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::Path;
use std::ptr::{self, NonNull};
use std::rc::{Rc, Weak};

use anyhow::{Context, Result, ensure};
use libloading::{Library, os::windows};

use super::validate_token;

pub const SUPPORTED_SDK: &str = "2.4.1";

type Handle = *mut c_void;

#[repr(C)]
struct ClientParameters {
    server_key: *const c_char,
    host: *const c_char,
    port: i32,
    ssl: bool,
}

#[repr(C)]
struct RtParameters {
    host: *const c_char,
    port: i32,
    ssl: bool,
}

#[repr(C)]
struct Bytes {
    data: *const u8,
    size: u32,
}

#[repr(C)]
struct NativePresence {
    user_id: *const c_char,
    session_id: *const c_char,
    username: *const c_char,
    persistent: bool,
    status: *const c_char,
}

#[repr(C)]
struct NativeMatch {
    match_id: *const c_char,
    authoritative: bool,
    label: *const c_char,
    size: i32,
    presences: *const NativePresence,
    presences_count: u16,
    self_presence: NativePresence,
}

#[repr(C)]
struct MatchData {
    match_id: *const c_char,
    presence: NativePresence,
    opcode: i64,
    data: Bytes,
}

#[repr(C)]
struct RtError {
    code: c_int,
    message: *const c_char,
    context: Handle,
}

#[repr(C)]
struct DisconnectInfo {
    code: u16,
    reason: *const c_char,
    remote: bool,
}

#[repr(C)]
struct PresenceEvent {
    match_id: *const c_char,
    joins: *const NativePresence,
    joins_count: u16,
    leaves: *const NativePresence,
    leaves_count: u16,
}

type ConnectCallback = unsafe extern "C" fn(Handle);
type DisconnectCallback = unsafe extern "C" fn(Handle, *const DisconnectInfo);
type ErrorCallback = unsafe extern "C" fn(Handle, *const RtError);
type JoinCallback = unsafe extern "C" fn(Handle, Handle, *const NativeMatch);
type JoinErrorCallback = unsafe extern "C" fn(Handle, Handle, *const RtError);
type DataCallback = unsafe extern "C" fn(Handle, *const MatchData);
type PresenceCallback = unsafe extern "C" fn(Handle, *const PresenceEvent);
type LogCallback = unsafe extern "C" fn(c_int, *const c_char, *const c_char);

struct Exports {
    create_client: unsafe extern "C" fn(*const ClientParameters) -> Handle,
    destroy_client: unsafe extern "C" fn(Handle),
    create_rt: unsafe extern "C" fn(Handle, *const RtParameters) -> Handle,
    restore_session: unsafe extern "C" fn(*const c_char) -> Handle,
    destroy_session: unsafe extern "C" fn(Handle),
    connect: unsafe extern "C" fn(Handle, Handle, bool, c_int),
    tick: unsafe extern "C" fn(Handle),
    disconnect: unsafe extern "C" fn(Handle),
    destroy_rt: unsafe extern "C" fn(Handle),
    set_connect: unsafe extern "C" fn(Handle, ConnectCallback),
    set_disconnect: unsafe extern "C" fn(Handle, DisconnectCallback),
    set_error: unsafe extern "C" fn(Handle, ErrorCallback),
    set_data: unsafe extern "C" fn(Handle, DataCallback),
    set_presence: unsafe extern "C" fn(Handle, PresenceCallback),
    join: unsafe extern "C" fn(
        Handle,
        *const c_char,
        Handle,
        Handle,
        JoinCallback,
        JoinErrorCallback,
    ),
    send:
        unsafe extern "C" fn(Handle, *const c_char, i64, *const Bytes, *const NativePresence, u16),
    map_create: unsafe extern "C" fn() -> Handle,
    map_destroy: unsafe extern "C" fn(Handle),
}

pub struct NakamaSdk {
    api: Exports,
    _library: Library,
}

impl NakamaSdk {
    pub fn load(game_dir: &Path) -> Result<Self> {
        let path = game_dir.join("binaries").join("nakama-cpp.dll");
        let path = path
            .canonicalize()
            .with_context(|| format!("找不到 {}", path.display()))?;
        // CK3's sibling DLLs are searched alongside the selected SDK.
        let flags =
            windows::LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | windows::LOAD_LIBRARY_SEARCH_DEFAULT_DIRS;
        let library = Library::from(
            unsafe { windows::Library::load_with_flags(&path, flags) }
                .with_context(|| format!("加载 {} 失败", path.display()))?,
        );

        // Each signature follows the checked C ABI, including bool widths and callback arguments.
        let api = unsafe {
            let get_version: unsafe extern "C" fn() -> *const c_char =
                symbol(&library, c"getNakamaSdkVersion")?;
            let version = copy_text(get_version());
            ensure!(
                version == SUPPORTED_SDK,
                "SDK 版本为 {version}，需要 {SUPPORTED_SDK}"
            );
            let initialize_log: unsafe extern "C" fn(LogCallback, c_int) =
                symbol(&library, c"NLogger_init")?;
            initialize_log(discard_log, 4);
            Exports {
                create_client: symbol(&library, c"createDefaultNakamaClient")?,
                destroy_client: symbol(&library, c"destroyNakamaClient")?,
                create_rt: symbol(&library, c"NClient_createRtClientEx")?,
                restore_session: symbol(&library, c"restoreNakamaSession")?,
                destroy_session: symbol(&library, c"NSession_destroy")?,
                connect: symbol(&library, c"NRtClient_connect")?,
                tick: symbol(&library, c"NRtClient_tick")?,
                disconnect: symbol(&library, c"NRtClient_disconnect")?,
                destroy_rt: symbol(&library, c"NRtClient_destroy")?,
                set_connect: symbol(&library, c"NRtClient_setConnectCallback")?,
                set_disconnect: symbol(&library, c"NRtClient_setDisconnectCallback")?,
                set_error: symbol(&library, c"NRtClient_setErrorCallback")?,
                set_data: symbol(&library, c"NRtClient_setMatchDataCallback")?,
                set_presence: symbol(&library, c"NRtClient_setMatchPresenceCallback")?,
                join: symbol(&library, c"NRtClient_joinMatch")?,
                send: symbol(&library, c"NRtClient_sendMatchData")?,
                map_create: symbol(&library, c"NStringMap_create")?,
                map_destroy: symbol(&library, c"NStringMap_destroy")?,
            }
        };
        Ok(Self {
            api,
            _library: library,
        })
    }

    pub fn client(
        &self,
        host: &str,
        port: u16,
        ssl: bool,
        token: &str,
    ) -> Result<NativeClient<'_>> {
        ensure!(!host.is_empty() && port != 0, "Nakama 主机或端口无效");
        validate_token(token)?;
        let host = CString::new(host).context("Nakama 主机名包含空字节")?;
        let token = CString::new(token).context("会话令牌包含空字节")?;
        let mut client = NativeClient {
            sdk: self,
            rest: None,
            rt: None,
            session: None,
            events: Rc::default(),
            started: false,
            connected: false,
            rooms: HashSet::new(),
        };
        let params = ClientParameters {
            server_key: c"".as_ptr(),
            host: host.as_ptr(),
            port: port.into(),
            ssl,
        };
        let rt_params = RtParameters {
            host: host.as_ptr(),
            port: port.into(),
            ssl,
        };

        // The SDK copies the configuration and owns its handles until the matching destroy calls.
        unsafe {
            let session = NonNull::new((self.api.restore_session)(token.as_ptr()))
                .context("SDK 无法恢复会话")?;
            client.session = Some(session);
            let rest =
                NonNull::new((self.api.create_client)(&params)).context("SDK 无法创建客户端")?;
            client.rest = Some(rest);
            let rt = NonNull::new((self.api.create_rt)(rest.as_ptr(), &rt_params))
                .context("SDK 无法创建实时客户端")?;
            client.rt = Some(rt);
            CALLBACKS.with_borrow_mut(|map| {
                map.insert(rt.as_ptr() as usize, Rc::downgrade(&client.events))
            });
            (self.api.set_connect)(rt.as_ptr(), on_connect);
            (self.api.set_disconnect)(rt.as_ptr(), on_disconnect);
            (self.api.set_error)(rt.as_ptr(), on_error);
            (self.api.set_data)(rt.as_ptr(), on_data);
            (self.api.set_presence)(rt.as_ptr(), on_presence);
        }
        Ok(client)
    }
}

unsafe fn symbol<T: Copy>(library: &Library, name: &CStr) -> Result<T> {
    // The caller supplies the function pointer type from the SDK ABI.
    Ok(*unsafe { library.get(name.to_bytes_with_nul()) }
        .with_context(|| format!("Nakama SDK 缺少 {}", name.to_string_lossy()))?)
}

#[derive(Clone, Copy)]
pub enum WireFormat {
    Protobuf,
    Json,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Presence {
    pub user_id: String,
    pub session_id: String,
    pub username: String,
    pub persistent: bool,
    pub status: String,
}

#[derive(Debug)]
pub enum Event {
    Connected,
    Disconnected {
        code: u16,
        reason: String,
        remote: bool,
    },
    Error {
        code: i32,
        message: String,
    },
    JoinError {
        code: i32,
        message: String,
    },
    Joined {
        match_id: String,
        size: i32,
        own: Presence,
        peers: Vec<Presence>,
    },
    Data {
        match_id: String,
        presence: Presence,
        opcode: i64,
        payload: Vec<u8>,
    },
    PresenceChanged {
        match_id: String,
        joins: Vec<Presence>,
        leaves: Vec<Presence>,
    },
}

// Rc keeps the realtime client and its callback queue on the creating thread.
pub struct NativeClient<'sdk> {
    sdk: &'sdk NakamaSdk,
    rest: Option<NonNull<c_void>>,
    rt: Option<NonNull<c_void>>,
    session: Option<NonNull<c_void>>,
    events: Rc<RefCell<Vec<Event>>>,
    started: bool,
    connected: bool,
    rooms: HashSet<String>,
}

impl NativeClient<'_> {
    pub fn connect(&mut self, format: WireFormat) -> Result<()> {
        ensure!(!self.started, "请创建新的客户端再重新连接");
        self.started = true;
        let session = self.session.expect("initialized Nakama session").as_ptr();
        let format = c_int::from(matches!(format, WireFormat::Json));
        unsafe { (self.sdk.api.connect)(self.rt(), session, false, format) };
        Ok(())
    }

    pub fn join(&self, match_id: &str) -> Result<()> {
        ensure!(self.connected, "连接 Nakama 后才能加入房间");
        ensure!(!match_id.is_empty(), "房间 ID 不能为空");
        let id = CString::new(match_id).context("房间 ID 含空字节")?;
        let api = &self.sdk.api;
        unsafe {
            let metadata = NonNull::new((api.map_create)()).context("SDK 无法创建入房参数")?;
            (api.join)(
                self.rt(),
                id.as_ptr(),
                metadata.as_ptr(),
                ptr::null_mut(),
                on_join,
                on_join_error,
            );
            (api.map_destroy)(metadata.as_ptr());
        }
        Ok(())
    }

    pub fn send(
        &self,
        match_id: &str,
        opcode: i64,
        payload: &[u8],
        recipients: &[Presence],
    ) -> Result<()> {
        ensure!(
            self.connected && self.rooms.contains(match_id),
            "加入房间后才能发送数据"
        );
        let id = CString::new(match_id)?;
        let size = u32::try_from(payload.len()).context("数据超过 SDK 长度上限")?;
        let count = u16::try_from(recipients.len()).context("接收方人数超过 SDK 上限")?;
        let strings = recipients
            .iter()
            .map(|target| {
                Ok((
                    CString::new(target.user_id.as_str())?,
                    CString::new(target.session_id.as_str())?,
                    CString::new(target.username.as_str())?,
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        let targets: Vec<_> = strings
            .iter()
            .map(|(user_id, session_id, username)| NativePresence {
                user_id: user_id.as_ptr(),
                session_id: session_id.as_ptr(),
                username: username.as_ptr(),
                persistent: false,
                status: c"".as_ptr(),
            })
            .collect();
        let data = Bytes {
            data: payload.first().map_or(&0_u8, |byte| byte),
            size,
        };
        let targets_ptr = targets.first().map_or(ptr::null(), ptr::from_ref);
        // The SDK copies both the payload and directed recipient list during this call.
        unsafe { (self.sdk.api.send)(self.rt(), id.as_ptr(), opcode, &data, targets_ptr, count) };
        Ok(())
    }

    pub fn poll(&mut self) -> Vec<Event> {
        unsafe { (self.sdk.api.tick)(self.rt()) };
        let events = self.events.take();
        for event in &events {
            match event {
                Event::Connected => self.connected = true,
                Event::Disconnected { .. } => {
                    self.connected = false;
                    self.rooms.clear();
                }
                Event::Joined { match_id, .. } => {
                    self.rooms.insert(match_id.clone());
                }
                _ => {}
            }
        }
        events
    }

    fn rt(&self) -> Handle {
        self.rt
            .expect("initialized Nakama realtime client")
            .as_ptr()
    }
}

impl Drop for NativeClient<'_> {
    fn drop(&mut self) {
        let api = &self.sdk.api;
        unsafe {
            if let Some(rt) = self.rt {
                if self.started {
                    (api.disconnect)(rt.as_ptr());
                }
                (api.destroy_rt)(rt.as_ptr());
                let _ = CALLBACKS
                    .try_with(|callbacks| callbacks.borrow_mut().remove(&(rt.as_ptr() as usize)));
            }
            if let Some(rest) = self.rest {
                (api.destroy_client)(rest.as_ptr());
            }
            if let Some(session) = self.session {
                (api.destroy_session)(session.as_ptr());
            }
        }
    }
}

type CallbackQueues = HashMap<usize, Weak<RefCell<Vec<Event>>>>;
thread_local! {
    static CALLBACKS: RefCell<CallbackQueues> = RefCell::default();
}

fn emit(client: Handle, event: Event) {
    let _ = CALLBACKS.try_with(|callbacks| {
        let queue = callbacks
            .try_borrow()
            .ok()
            .and_then(|map| map.get(&(client as usize))?.upgrade());
        if let Some(queue) = queue
            && let Ok(mut events) = queue.try_borrow_mut()
        {
            events.push(event);
        }
    });
}

unsafe extern "C" fn discard_log(_: c_int, _: *const c_char, _: *const c_char) {
    // SDK transport logs can contain authenticated WebSocket URLs.
}

unsafe extern "C" fn on_connect(client: Handle) {
    emit(client, Event::Connected);
}

unsafe extern "C" fn on_disconnect(client: Handle, info: *const DisconnectInfo) {
    if let Some(info) = unsafe { info.as_ref() } {
        emit(
            client,
            Event::Disconnected {
                code: info.code,
                reason: unsafe { copy_text(info.reason) },
                remote: info.remote,
            },
        );
    }
}

unsafe extern "C" fn on_error(client: Handle, error: *const RtError) {
    if let Some(error) = unsafe { error.as_ref() } {
        emit(
            client,
            Event::Error {
                code: error.code,
                message: unsafe { copy_text(error.message) },
            },
        );
    }
}

unsafe extern "C" fn on_join_error(client: Handle, _: Handle, error: *const RtError) {
    if let Some(error) = unsafe { error.as_ref() } {
        emit(
            client,
            Event::JoinError {
                code: error.code,
                message: unsafe { copy_text(error.message) },
            },
        );
    }
}

unsafe extern "C" fn on_join(client: Handle, _: Handle, room: *const NativeMatch) {
    if let Some(room) = unsafe { room.as_ref() } {
        let presences = unsafe { borrowed_items(room.presences, room.presences_count as usize) };
        emit(
            client,
            Event::Joined {
                match_id: unsafe { copy_text(room.match_id) },
                size: room.size,
                own: unsafe { copy_presence(&room.self_presence) },
                peers: presences
                    .iter()
                    .map(|presence| unsafe { copy_presence(presence) })
                    .collect(),
            },
        );
    }
}

unsafe extern "C" fn on_data(client: Handle, packet: *const MatchData) {
    if let Some(packet) = unsafe { packet.as_ref() } {
        emit(
            client,
            Event::Data {
                match_id: unsafe { copy_text(packet.match_id) },
                presence: unsafe { copy_presence(&packet.presence) },
                opcode: packet.opcode,
                payload: unsafe { borrowed_items(packet.data.data, packet.data.size as usize) }
                    .to_vec(),
            },
        );
    }
}

unsafe extern "C" fn on_presence(client: Handle, event: *const PresenceEvent) {
    if let Some(event) = unsafe { event.as_ref() } {
        let joins = unsafe { borrowed_items(event.joins, event.joins_count as usize) };
        let leaves = unsafe { borrowed_items(event.leaves, event.leaves_count as usize) };
        emit(
            client,
            Event::PresenceChanged {
                match_id: unsafe { copy_text(event.match_id) },
                joins: joins
                    .iter()
                    .map(|value| unsafe { copy_presence(value) })
                    .collect(),
                leaves: leaves
                    .iter()
                    .map(|value| unsafe { copy_presence(value) })
                    .collect(),
            },
        );
    }
}

unsafe fn copy_text(pointer: *const c_char) -> String {
    if pointer.is_null() {
        return String::new();
    }
    // The SDK owns a terminated string for the duration of this callback or SDK query.
    unsafe { CStr::from_ptr(pointer) }
        .to_string_lossy()
        .into_owned()
}

unsafe fn copy_presence(source: &NativePresence) -> Presence {
    Presence {
        user_id: unsafe { copy_text(source.user_id) },
        session_id: unsafe { copy_text(source.session_id) },
        username: unsafe { copy_text(source.username) },
        persistent: source.persistent,
        status: unsafe { copy_text(source.status) },
    }
}

unsafe fn borrowed_items<'a, T>(pointer: *const T, count: usize) -> &'a [T] {
    if pointer.is_null() || count == 0 {
        return &[];
    }
    // The caller must keep the SDK's array within the lifetime of the callback.
    unsafe { std::slice::from_raw_parts(pointer, count) }
}
