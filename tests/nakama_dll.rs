use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use ck3_llm::protocol::{self, Envelope, Magic, Message as PdxMessage};
use ck3_llm::session::native::{Event, NakamaSdk, NativeClient, WireFormat};
use serde_json::{Value, json};
use tungstenite::{Message, WebSocket};

const ROOM: &str = "00000000-0000-0000-0000-000000000001.mock";
const PLAYER: &str = "00000000-0000-0000-0000-000000000002";
const HOST: &str = "00000000-0000-0000-0000-000000000003";

#[test]
#[ignore = "requires the original Nakama DLL from a Windows CK3 installation"]
fn shipped_sdk_connects_joins_copies_dispatches_and_closes() {
    let game = std::env::var_os("CK3_GAME_DIR").map_or_else(
        || PathBuf::from(r"C:\Games\Crusader Kings III"),
        PathBuf::from,
    );
    let sdk = NakamaSdk::load(&game).unwrap();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let packet = Envelope {
        magic: Magic::Directed,
        recipient: PLAYER,
        source_user: HOST,
        sequence: 42,
        messages: vec![PdxMessage {
            word_4: 5,
            word_8: 1,
            payload: b"\x00CK3\xff",
        }],
    }
    .encode()
    .unwrap();
    let encoded_packet = STANDARD.encode(&packet);
    let echo_payload = b"\x00directed\xff";

    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        let mut peer = tungstenite::accept(stream).unwrap();
        let join = read_json(&mut peer);
        assert_eq!(
            field(
                field(&join, "match_join", "matchJoin"),
                "match_id",
                "matchId"
            ),
            ROOM
        );
        let host = json!({"user_id": HOST, "session_id": "test-host", "username": "Host"});
        let own = json!({"user_id": PLAYER, "session_id": "test-player", "username": "Player"});
        send_json(
            &mut peer,
            json!({"cid": join["cid"], "match": {
                "match_id": ROOM, "size": 2, "presences": [&host], "self": own,
            }}),
        );
        send_json(
            &mut peer,
            json!({"match_presence_event": {"match_id": ROOM, "joins": [&host], "leaves": []}}),
        );
        send_json(
            &mut peer,
            json!({"match_data": {
                "match_id": ROOM, "presence": &host, "op_code": "255", "data": encoded_packet,
            }}),
        );
        let sent = read_json(&mut peer);
        let sent = field(&sent, "match_data_send", "matchDataSend");
        let opcode = field(sent, "op_code", "opCode");
        assert_eq!(opcode.as_str(), Some(i64::MAX.to_string().as_str()));
        assert_eq!(sent["data"], STANDARD.encode(echo_payload));
        assert_eq!(
            field(&sent["presences"][0], "session_id", "sessionId"),
            "test-host"
        );
        send_json(
            &mut peer,
            json!({"match_data": {
                "match_id": ROOM, "presence": &host, "op_code": opcode, "data": sent["data"],
            }}),
        );
        assert!(matches!(peer.read().unwrap(), Message::Close(_)));
        let _ = peer.flush();
    });

    let mut client = sdk
        .client("127.0.0.1", port, false, &synthetic_token())
        .unwrap();
    client.connect(WireFormat::Json).unwrap();
    wait_for(&mut client, |_, event| {
        matches!(event, Event::Connected).then_some(())
    });
    client.join(ROOM).unwrap();

    let mut received_presence = false;
    let mut received_frame = false;
    let mut received_join = false;
    wait_for(&mut client, |client, event| {
        match event {
            Event::Joined {
                match_id,
                size,
                own,
                peers,
            } => {
                assert_eq!(match_id, ROOM);
                assert_eq!(size, 2);
                assert_eq!(own.user_id, PLAYER);
                assert_eq!(peers[0].user_id, HOST);
                client.send(ROOM, i64::MAX, echo_payload, &peers).unwrap();
                received_join = true;
            }
            Event::PresenceChanged {
                match_id,
                joins,
                leaves,
            } => {
                assert_eq!(match_id, ROOM);
                assert_eq!(joins[0].user_id, HOST);
                assert!(leaves.is_empty());
                received_presence = true;
            }
            Event::Data {
                match_id,
                presence,
                opcode: 255,
                payload,
            } => {
                assert_eq!(match_id, ROOM);
                assert_eq!(presence.user_id, HOST);
                assert_eq!(payload, packet);
                assert_eq!(protocol::decode(&payload).unwrap().sequence, 42);
                received_frame = true;
            }
            Event::Data {
                opcode: i64::MAX,
                payload,
                ..
            } => {
                assert_eq!(payload, echo_payload);
                assert!(received_join && received_presence && received_frame);
                return Some(());
            }
            _ => {}
        }
        None
    });
    drop(client);
    server.join().unwrap();
}

fn wait_for<T>(
    client: &mut NativeClient<'_>,
    mut handle: impl FnMut(&NativeClient<'_>, Event) -> Option<T>,
) -> T {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        for event in client.poll() {
            if matches!(
                event,
                Event::Error { .. } | Event::JoinError { .. } | Event::Disconnected { .. }
            ) {
                panic!("SDK event: {event:?}");
            }
            if let Some(value) = handle(client, event) {
                return value;
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
    panic!("No expected SDK event arrived");
}

fn synthetic_token() -> String {
    let expires = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 600;
    let header = URL_SAFE_NO_PAD.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
    let claims =
        URL_SAFE_NO_PAD.encode(json!({"uid": PLAYER, "usn": "Player", "exp": expires}).to_string());
    let signature = URL_SAFE_NO_PAD.encode([1; 32]);
    format!("{header}.{claims}.{signature}")
}

fn read_json(peer: &mut WebSocket<TcpStream>) -> Value {
    loop {
        match peer.read().unwrap() {
            Message::Text(data) => return serde_json::from_str(&data).unwrap(),
            Message::Ping(_) | Message::Pong(_) => peer.flush().unwrap(),
            frame => panic!("Unexpected websocket frame: {frame:?}"),
        }
    }
}

fn send_json(peer: &mut WebSocket<TcpStream>, value: Value) {
    peer.send(Message::text(value.to_string())).unwrap();
}

fn field<'a>(value: &'a Value, snake: &str, camel: &str) -> &'a Value {
    value.get(snake).or_else(|| value.get(camel)).unwrap()
}
