use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::net::{Ipv4Addr, TcpListener};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use axum::extract::{
    Path as Route, Query, State, WebSocketUpgrade,
    ws::{Message, WebSocket},
};
use axum::http::{HeaderMap, StatusCode, header::AUTHORIZATION};
use axum::response::{IntoResponse, Response};
use axum::{
    Json, Router,
    routing::{get, post},
};
use prost::Message as _;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, watch};
use uuid::Uuid;

use super::auth::{Accounts, BASIC_AUTH, User};
use super::wire::{self, Envelope, Presence};

const SOCKET_LIMIT: usize = 4 * 1024 * 1024;
const QUEUE_LIMIT: usize = 256;

pub struct LocalBackend {
    port: u16,
    #[cfg(feature = "host-bridge")]
    state: Arc<Shared>,
    stop: watch::Sender<bool>,
    thread: Option<JoinHandle<Result<()>>>,
}

impl LocalBackend {
    pub fn start(port: u16, capture: Option<&Path>) -> Result<Self> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, port))
            .with_context(|| format!("启动本地联机服务 127.0.0.1:{port}"))?;
        let port = listener.local_addr()?.port();
        listener.set_nonblocking(true)?;
        let (stop, shutdown) = watch::channel(false);
        let state = Arc::new(Shared {
            accounts: Accounts::new(),
            rooms: Mutex::default(),
            capture: capture.map(Capture::create).transpose()?.map(Mutex::new),
            shutdown: shutdown.clone(),
        });
        let router = Router::new()
            .route("/", get(health))
            .route("/healthcheck", get(health))
            .route("/v2/account", get(account))
            .route("/v2/account/authenticate/custom", post(authenticate))
            .route("/v2/session/refresh", post(refresh))
            .route("/v2/session/logout", post(|| async { Json(json!({})) }))
            .route("/v2/rpc/{name}", post(http_rpc))
            .route("/ws", get(websocket))
            .fallback(|| async {
                (
                    StatusCode::NOT_FOUND,
                    Json(json!({"code": 5, "message": "Unknown local endpoint"})),
                )
            })
            .with_state(Arc::clone(&state));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let thread = thread::Builder::new()
            .name("local-nakama".into())
            .spawn(move || {
                runtime.block_on(async move {
                    let listener = tokio::net::TcpListener::from_std(listener)?;
                    axum::serve(listener, router)
                        .with_graceful_shutdown(signaled(shutdown))
                        .await?;
                    Ok(())
                })
            })
            .context("启动本地房间线程")?;
        state.record(
            "local_server_ready",
            json!({"host": "127.0.0.1", "port": port}),
            None,
        );
        Ok(Self {
            port,
            #[cfg(feature = "host-bridge")]
            state,
            stop,
            thread: Some(thread),
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    #[cfg(feature = "host-bridge")]
    pub(crate) fn owns_joined_room(&self, id: &str, owner: &str) -> bool {
        self.state
            .rooms
            .lock()
            .unwrap()
            .get(id)
            .is_some_and(|room| {
                room.owner == owner
                    && room
                        .members
                        .values()
                        .any(|peer| peer.presence.user_id == owner)
            })
    }

    #[cfg(feature = "host-bridge")]
    pub(crate) fn record(&self, name: &str, fields: Value) {
        self.state.record(name, fields, None);
    }
}

impl Drop for LocalBackend {
    fn drop(&mut self) {
        let _ = self.stop.send(true);
        if let Some(worker) = self.thread.take() {
            let _ = worker.join();
        }
    }
}

struct Shared {
    accounts: Accounts,
    rooms: Mutex<HashMap<String, Room>>,
    capture: Option<Mutex<Capture>>,
    shutdown: watch::Receiver<bool>,
}

struct Room {
    owner: String,
    details: Value,
    members: HashMap<String, Peer>,
}

#[derive(Clone)]
struct Peer {
    presence: Presence,
    outbound: mpsc::Sender<Arc<Envelope>>,
    close: watch::Sender<bool>,
}

impl Peer {
    fn send(&self, message: Arc<Envelope>) {
        if self.outbound.try_send(message).is_err() {
            let _ = self.close.send(true);
        }
    }
}

type ApiError = (StatusCode, Json<Value>);

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "backend": "ck3-local"}))
}

#[derive(Deserialize)]
struct LoginQuery {
    username: Option<String>,
}

#[derive(Deserialize)]
struct Login {
    id: String,
}

async fn authenticate(
    State(state): State<Arc<Shared>>,
    Query(query): Query<LoginQuery>,
    headers: HeaderMap,
    Json(input): Json<Login>,
) -> Result<Json<super::auth::Tokens>, ApiError> {
    if header(&headers) != BASIC_AUTH {
        return Err(auth_error());
    }
    if input.id.is_empty() || input.id.contains('\0') {
        return Err(request_error("Custom local identity is required"));
    }
    let username = query
        .username
        .as_deref()
        .filter(|name| !name.is_empty())
        .unwrap_or("LocalPlayer");
    if username.contains('\0') {
        return Err(request_error("Local username contains NUL"));
    }
    let tokens = state.accounts.authenticate(&input.id, username);
    state.record(
        "local_user_authenticated",
        json!({"created": tokens.created}),
        None,
    );
    Ok(Json(tokens))
}

#[derive(Deserialize)]
struct Token {
    token: String,
}

async fn refresh(
    State(state): State<Arc<Shared>>,
    Json(token): Json<Token>,
) -> Result<Json<Value>, ApiError> {
    let user = state
        .accounts
        .verify(&token.token, true)
        .ok_or_else(auth_error)?;
    let tokens = state.accounts.tokens(&user, false);
    Ok(Json(
        json!({"token": tokens.token, "refresh_token": tokens.refresh_token}),
    ))
}

async fn account(
    State(state): State<Arc<Shared>>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let user = state.authorized(&headers)?;
    Ok(Json(json!({"user": user, "wallet": "{}"})))
}

async fn http_rpc(
    State(state): State<Arc<Shared>>,
    Route(name): Route<String>,
    headers: HeaderMap,
    Json(input): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let user = state.authorized(&headers)?;
    let payload = input.get("payload").unwrap_or(&input);
    let payload = match payload {
        Value::String(raw) => parse_payload(raw)?,
        other => other.clone(),
    };
    let result = state.rpc(&name, payload, &user).map_err(request_error)?;
    Ok(Json(json!({"payload": result.to_string()})))
}

fn parse_payload(raw: &str) -> Result<Value, ApiError> {
    if raw.is_empty() {
        return Ok(json!({}));
    }
    serde_json::from_str(raw).map_err(|_| request_error("Invalid local RPC payload"))
}

fn header(headers: &HeaderMap) -> &str {
    headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
}

fn auth_error() -> ApiError {
    (
        StatusCode::UNAUTHORIZED,
        Json(json!({"code": 16, "message": "Invalid local session"})),
    )
}

fn request_error(message: &str) -> ApiError {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"code": 3, "message": message})),
    )
}

impl Shared {
    fn authorized(&self, headers: &HeaderMap) -> Result<User, ApiError> {
        let token = header(headers)
            .strip_prefix("Bearer ")
            .ok_or_else(auth_error)?;
        self.accounts.verify(token, false).ok_or_else(auth_error)
    }

    fn rpc(&self, name: &str, payload: Value, user: &User) -> Result<Value, &'static str> {
        self.record(
            "rpc",
            json!({"name": name, "user_id": user.id, "data": payload}),
            None,
        );
        let mut rooms = self.rooms.lock().unwrap();
        match name {
            "create_jomini_match" => {
                let match_id = format!("{}.local", Uuid::new_v4());
                rooms.insert(
                    match_id.clone(),
                    Room {
                        owner: user.id.clone(),
                        details: payload,
                        members: HashMap::new(),
                    },
                );
                Ok(json!({"match_id": match_id}))
            }
            "list_matches" => {
                let entries: serde_json::Map<_, _> = rooms
                    .iter()
                    .filter(|(_, room)| !room.members.is_empty())
                    .map(|(id, room)| {
                        let mut fields = room.details.as_object().cloned().unwrap_or_default();
                        fields.insert("match_id".into(), json!(id));
                        fields.insert("host_id".into(), json!(room.owner));
                        fields.insert("size".into(), json!(room.members.len()));
                        fields.insert("data".into(), room.details.clone());
                        (id.clone(), Value::Object(fields))
                    })
                    .collect();
                Ok(json!({"matches": entries}))
            }
            _ => Err("Unsupported local RPC"),
        }
    }

    fn record(&self, name: &str, fields: Value, payload: Option<&[u8]>) {
        if let Some(capture) = &self.capture {
            let _ = capture.lock().unwrap().write(name, fields, payload);
        }
    }
}

#[derive(Deserialize)]
struct SocketQuery {
    token: String,
    format: Option<String>,
}

async fn websocket(
    State(state): State<Arc<Shared>>,
    Query(query): Query<SocketQuery>,
    upgrade: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    let user = state
        .accounts
        .verify(&query.token, false)
        .ok_or_else(auth_error)?;
    let binary = query.format.as_deref() == Some("protobuf");
    Ok(upgrade
        .max_message_size(SOCKET_LIMIT)
        .max_frame_size(SOCKET_LIMIT)
        .on_upgrade(move |socket| serve_socket(socket, state, user, binary))
        .into_response())
}

async fn serve_socket(mut socket: WebSocket, state: Arc<Shared>, user: User, binary: bool) {
    let (outbound, mut outgoing) = mpsc::channel(QUEUE_LIMIT);
    let (close, disconnect) = watch::channel(false);
    let peer = Peer {
        presence: Presence {
            user_id: user.id.clone(),
            username: user.username.clone(),
            session_id: Uuid::new_v4().to_string(),
        },
        outbound,
        close,
    };
    state.record(
        "socket_connected",
        json!({"user_id": user.id, "session_id": peer.presence.session_id,
        "protocol": if binary { "protobuf" } else { "json" }}),
        None,
    );
    let mut joined = HashSet::new();
    let shutdown = signaled(state.shutdown.clone());
    let disconnect = signaled(disconnect);
    tokio::pin!(shutdown, disconnect);
    loop {
        tokio::select! {
            biased;
            () = &mut shutdown => break,
            () = &mut disconnect => break,
            Some(message) = outgoing.recv() => {
                let message = if binary { Message::Binary(message.encode_to_vec().into()) }
                    else { match serde_json::to_string(message.as_ref()) { Ok(text) => Message::Text(text.into()), Err(_) => break } };
                if !matches!(tokio::time::timeout(Duration::from_secs(3), socket.send(message)).await, Ok(Ok(()))) { break; }
            }
            incoming = socket.recv() => {
                let result = match incoming {
                    Some(Ok(Message::Binary(bytes))) if binary => Envelope::decode(bytes).ok(),
                    Some(Ok(Message::Text(text))) if !binary => serde_json::from_str(&text).ok(),
                    Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
                    _ => break,
                };
                if let Some(message) = result { handle(&state, &peer, &user, &mut joined, message); }
                else { peer.send(error(String::new(), 3, "Invalid local realtime message")); }
            }
        }
    }
    for match_id in &joined {
        leave(&state, &peer, match_id);
    }
    let _ = tokio::time::timeout(
        Duration::from_millis(200),
        socket.send(Message::Close(None)),
    )
    .await;
    state.record(
        "socket_closed",
        json!({"session_id": peer.presence.session_id}),
        None,
    );
}

async fn signaled(mut receiver: watch::Receiver<bool>) {
    let _ = receiver.wait_for(|value| *value).await;
}

fn handle(
    state: &Shared,
    peer: &Peer,
    user: &User,
    joined: &mut HashSet<String>,
    message: Envelope,
) {
    let cid = message.cid;
    if let Some(mut rpc) = message.rpc {
        let result = parse_payload(&rpc.payload)
            .map_err(|_| "Invalid local RPC payload")
            .and_then(|payload| state.rpc(&rpc.id, payload, user));
        peer.send(match result {
            Ok(result) => {
                rpc.payload = result.to_string();
                Arc::new(Envelope {
                    cid,
                    rpc: Some(rpc),
                    ..Default::default()
                })
            }
            Err(message) => error(cid, 3, message),
        });
    } else if let Some(request) = message.match_join {
        if join(state, peer, cid, &request.match_id) {
            joined.insert(request.match_id);
        }
    } else if let Some(data) = message.match_data_send {
        relay(state, peer, cid, data);
    } else if let Some(request) = message.match_leave {
        if joined.remove(&request.match_id) {
            leave(state, peer, &request.match_id);
        }
        peer.send(Arc::new(Envelope {
            cid,
            ..Default::default()
        }));
    } else if message.ping.is_some() {
        peer.send(Arc::new(Envelope {
            cid,
            pong: Some(wire::Empty {}),
            ..Default::default()
        }));
    } else {
        peer.send(error(cid, 3, "Unsupported local realtime operation"));
    }
}

fn join(state: &Shared, peer: &Peer, cid: String, match_id: &str) -> bool {
    let mut rooms = state.rooms.lock().unwrap();
    let Some(room) = rooms.get_mut(match_id) else {
        peer.send(error(cid, 4, "Local room not found"));
        return false;
    };
    let own = &peer.presence;
    let existing = room
        .members
        .insert(own.session_id.clone(), peer.clone())
        .is_some();
    let others = room
        .members
        .values()
        .filter(|member| member.presence.session_id != own.session_id);
    let size = room.members.len() as i32;
    let response = wire::Match {
        match_id: match_id.into(),
        authoritative: false,
        size,
        presences: others.clone().map(|peer| peer.presence.clone()).collect(),
        own: Some(own.clone()),
    };
    peer.send(Arc::new(Envelope {
        cid,
        room: Some(response),
        ..Default::default()
    }));
    if !existing {
        let notice = presence_event(match_id, vec![own.clone()], Vec::new());
        for other in others {
            other.send(Arc::clone(&notice));
        }
    }
    drop(rooms);
    state.record(
        "room_joined",
        json!({"match_id": match_id, "size": size, "presence": own}),
        None,
    );
    true
}

fn leave(state: &Shared, peer: &Peer, match_id: &str) {
    let mut rooms = state.rooms.lock().unwrap();
    let Some(room) = rooms.get_mut(match_id) else {
        return;
    };
    if room.members.remove(&peer.presence.session_id).is_none() {
        return;
    }
    if room.members.is_empty() {
        rooms.remove(match_id);
    } else {
        let notice = presence_event(match_id, Vec::new(), vec![peer.presence.clone()]);
        for other in room.members.values() {
            other.send(Arc::clone(&notice));
        }
    }
    drop(rooms);
    state.record(
        "room_left",
        json!({"match_id": match_id, "presence": peer.presence}),
        None,
    );
}

fn relay(state: &Shared, peer: &Peer, cid: String, data: wire::MatchDataSend) {
    let rooms = state.rooms.lock().unwrap();
    let Some(room) = rooms
        .get(&data.match_id)
        .filter(|room| room.members.contains_key(&peer.presence.session_id))
    else {
        peer.send(error(cid, 3, "Cannot send outside a joined local room"));
        return;
    };
    let mut recipients = Vec::new();
    let message = Arc::new(Envelope {
        match_data: Some(wire::MatchData {
            match_id: data.match_id.clone(),
            presence: Some(peer.presence.clone()),
            op_code: data.op_code,
            data: data.data.clone(),
        }),
        ..Default::default()
    });
    for other in room.members.values() {
        let to = &other.presence;
        if to.session_id != peer.presence.session_id
            && (data.presences.is_empty()
                || data.presences.iter().any(|target| {
                    target.user_id == to.user_id
                        && (target.session_id.is_empty() || target.session_id == to.session_id)
                }))
        {
            other.send(Arc::clone(&message));
            recipients.push(to.clone());
        }
    }
    drop(rooms);
    state.record(
        "match_data",
        json!({"match_id": data.match_id, "opcode": data.op_code,
        "source": peer.presence, "recipients": recipients}),
        Some(&data.data),
    );
}

fn error(cid: String, code: i32, message: &str) -> Arc<Envelope> {
    Arc::new(Envelope {
        cid,
        error: Some(wire::Error {
            code,
            message: message.into(),
        }),
        ..Default::default()
    })
}

fn presence_event(match_id: &str, joins: Vec<Presence>, leaves: Vec<Presence>) -> Arc<Envelope> {
    Arc::new(Envelope {
        match_presence_event: Some(wire::PresenceEvent {
            match_id: match_id.into(),
            joins,
            leaves,
        }),
        ..Default::default()
    })
}

struct Capture {
    dir: PathBuf,
    log: File,
    index: u64,
}

impl Capture {
    fn create(dir: &Path) -> Result<Self> {
        fs::create_dir_all(dir)?;
        let log = File::options()
            .create(true)
            .append(true)
            .open(dir.join("events.jsonl"))?;
        Ok(Self {
            dir: dir.into(),
            log,
            index: 0,
        })
    }

    fn write(&mut self, name: &str, fields: Value, payload: Option<&[u8]>) -> Result<()> {
        let time = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let mut fields = fields.as_object().cloned().unwrap_or_default();
        fields.insert("event".into(), json!(name));
        fields.insert("time".into(), json!(time));
        if let Some(payload) = payload {
            self.index += 1;
            let filename = format!("{:06}.bin", self.index);
            fs::write(self.dir.join(&filename), payload)?;
            fields.insert("payload_file".into(), json!(filename));
            fields.insert("payload_size".into(), json!(payload.len()));
            fields.insert(
                "payload_sha256".into(),
                json!(format!("{:x}", Sha256::digest(payload))),
            );
        }
        serde_json::to_writer(&mut self.log, &fields)?;
        self.log.write_all(b"\n")?;
        Ok(())
    }
}
