use std::net::TcpStream;
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

use ck3_llm::host::backend::LocalBackend;
use ck3_llm::session::native::{Event, NakamaSdk, WireFormat};
use reqwest::blocking::Client;
use serde_json::{Value, json};
use tungstenite::{Message, WebSocket, stream::MaybeTlsStream};

fn http() -> Client {
    Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap()
}

fn login(port: u16, identity: &str, username: &str) -> Value {
    http()
        .post(format!(
            "http://127.0.0.1:{port}/v2/account/authenticate/custom"
        ))
        .basic_auth("ck3-local", Some(""))
        .query(&[("username", username)])
        .json(&json!({"id": identity}))
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap()
}

fn rpc(port: u16, name: &str, token: &str, data: &Value) -> Value {
    let response: Value = http()
        .post(format!("http://127.0.0.1:{port}/v2/rpc/{name}"))
        .bearer_auth(token)
        .json(data)
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap();
    serde_json::from_str(response["payload"].as_str().unwrap()).unwrap()
}

type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

fn open_socket(port: u16, token: &str) -> Socket {
    let (mut socket, _) = tungstenite::connect(format!(
        "ws://127.0.0.1:{port}/ws?token={token}&format=json"
    ))
    .unwrap();
    if let MaybeTlsStream::Plain(stream) = socket.get_mut() {
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
    }
    socket
}

fn send(socket: &mut Socket, value: Value) {
    socket.send(Message::text(value.to_string())).unwrap();
}

fn receive(socket: &mut Socket) -> Value {
    let message = socket.read().unwrap();
    serde_json::from_str(message.to_text().unwrap()).unwrap()
}

#[test]
fn local_authentication_refresh_and_room_lifecycle() {
    let backend = LocalBackend::start(0, None).unwrap();
    let port = backend.port();
    let first = login(port, "local-a", "Host");
    assert_eq!(first["created"], true);
    assert_eq!(login(port, "local-a", "Another Name")["created"], false);
    let account = format!("http://127.0.0.1:{port}/v2/account");
    for invalid in [
        first["refresh_token"].as_str().unwrap().to_string(),
        format!("{}bad", first["token"].as_str().unwrap()),
    ] {
        assert_eq!(
            http()
                .get(&account)
                .bearer_auth(invalid)
                .send()
                .unwrap()
                .status(),
            401
        );
    }
    let fresh: Value = http()
        .post(format!("http://127.0.0.1:{port}/v2/session/refresh"))
        .json(&json!({"token": first["refresh_token"]}))
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap();
    let token = fresh["token"].as_str().unwrap();
    let user: Value = http()
        .get(&account)
        .bearer_auth(token)
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(user["user"]["username"], "Host");

    let mut socket = open_socket(port, token);
    send(
        &mut socket,
        json!({"cid": "create", "rpc": {"id": "create_jomini_match", "payload": "{\"name\":\"Room\"}"}}),
    );
    let created = receive(&mut socket);
    assert_eq!(created["cid"], "create");
    let payload: Value = serde_json::from_str(created["rpc"]["payload"].as_str().unwrap()).unwrap();
    let room = payload["match_id"].as_str().unwrap();
    send(
        &mut socket,
        json!({"cid": "j", "matchJoin": {"matchId": room}}),
    );
    let joined = receive(&mut socket);
    assert_eq!(joined["match"]["match_id"], room);
    assert_eq!(joined["match"]["self"]["user_id"], user["user"]["id"]);
    let listed = rpc(port, "list_matches", token, &json!({}));
    assert_eq!(listed["matches"][room]["name"], "Room");
    send(&mut socket, json!({"cid": "p", "ping": {}}));
    assert_eq!(receive(&mut socket), json!({"cid": "p", "pong": {}}));
    send(
        &mut socket,
        json!({"cid": "l", "match_leave": {"match_id": room}}),
    );
    assert_eq!(receive(&mut socket), json!({"cid": "l"}));
    assert_eq!(
        rpc(port, "list_matches", token, &json!({}))["matches"],
        json!({})
    );
    drop(backend);
}

#[test]
#[ignore = "requires the original Nakama DLL from a Windows CK3 installation"]
fn installed_nakama_clients_relay_between_protobuf_and_json() {
    let game = std::env::var_os("CK3_GAME_DIR").map_or_else(
        || PathBuf::from(r"C:\Games\Crusader Kings III"),
        PathBuf::from,
    );
    let sdk = NakamaSdk::load(&game).unwrap();
    let capture = tempfile::tempdir().unwrap();
    let backend = LocalBackend::start(0, Some(capture.path())).unwrap();
    let port = backend.port();
    let token_a = login(port, "first", "Host");
    let token_b = login(port, "second", "Player");
    let token_a = token_a["token"].as_str().unwrap();
    let token_b = token_b["token"].as_str().unwrap();
    let created = rpc(
        port,
        "create_jomini_match",
        token_a,
        &json!("{\"name\":\"Local\"}"),
    );
    let room = created["match_id"].as_str().unwrap();
    let mut clients = [
        sdk.client("127.0.0.1", port, false, token_a).unwrap(),
        sdk.client("127.0.0.1", port, false, token_b).unwrap(),
    ];
    clients[0].connect(WireFormat::Protobuf).unwrap();
    clients[1].connect(WireFormat::Json).unwrap();
    let mut joined = [false; 2];
    let deadline = Instant::now() + Duration::from_secs(8);
    while !joined.iter().all(|value| *value) {
        assert!(Instant::now() < deadline, "timed out joining the room");
        for (index, client) in clients.iter_mut().enumerate() {
            for event in client.poll() {
                match event {
                    Event::Connected => client.join(room).unwrap(),
                    Event::Joined { .. } => joined[index] = true,
                    Event::Error { .. } | Event::JoinError { .. } | Event::Disconnected { .. } => {
                        panic!("unexpected callback {event:?}")
                    }
                    _ => {}
                }
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
    clients[0].send(room, -1, b"\0from protobuf", &[]).unwrap();
    clients[1]
        .send(room, i64::MAX, b"from json\xff", &[])
        .unwrap();
    let mut received = [None, None];
    while !received.iter().all(Option::is_some) {
        assert!(Instant::now() < deadline, "timed out relaying the data");
        for (index, client) in clients.iter_mut().enumerate() {
            for event in client.poll() {
                if let Event::Data {
                    opcode, payload, ..
                } = event
                {
                    received[index] = Some((opcode, payload));
                }
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(received[0], Some((i64::MAX, b"from json\xff".to_vec())));
    assert_eq!(received[1], Some((-1, b"\0from protobuf".to_vec())));
    drop(clients);
    drop(backend);
    let log = std::fs::read_to_string(capture.path().join("events.jsonl")).unwrap();
    let captures: Vec<_> = log
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|event| event["event"] == "match_data")
        .map(|event| {
            std::fs::read(capture.path().join(event["payload_file"].as_str().unwrap())).unwrap()
        })
        .collect();
    assert_eq!(captures.len(), 2);
    assert!(captures.contains(&b"from json\xff".to_vec()));
    assert!(captures.contains(&b"\0from protobuf".to_vec()));
}
