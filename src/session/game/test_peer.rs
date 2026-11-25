use super::*;
use crate::host::backend::LocalBackend;
use crate::session::{
    EXE_SHA256, SessionTask,
    native::{Event, NakamaSdk, WireFormat},
};
use crate::settings::{ConnectionSettings, write_json};
use reqwest::blocking::Client;
use serde_json::{Value, json};

const SNAPSHOT: &[u8] = include_bytes!("../../../tests/fixtures/hotjoin-fixture.zip");
const SNAPSHOT_CHECKSUM: i32 = 1_508_336_786;
const CHUNK_SIZE: usize = 64;

#[test]
fn snapshot_checksum_matches_the_reference_encoder() {
    let mut checksum = Checksum::default();
    for chunk in SNAPSHOT.chunks(31) {
        checksum.feed(chunk);
    }
    assert_eq!(checksum.value(), SNAPSHOT_CHECKSUM);
}

#[test]
#[ignore = "requires the original Nakama DLL from a Windows CK3 installation"]
fn rust_client_hotjoins_receives_snapshot_and_submits_player_controls() {
    let game_dir = std::env::var_os("CK3_GAME_DIR").map_or_else(
        || PathBuf::from(r"C:\Games\Crusader Kings III"),
        PathBuf::from,
    );
    let sdk = NakamaSdk::load(&game_dir).unwrap();
    let backend = LocalBackend::start(0, None).unwrap();
    let http = Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    let base = format!("http://127.0.0.1:{}", backend.port());
    let login: Value = http
        .post(format!(
            "{base}/v2/account/authenticate/custom?username=TestHost"
        ))
        .basic_auth("ck3-local", Some(""))
        .json(&json!({"id":"test-host"}))
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap();
    let token = login["token"].as_str().unwrap();
    let created: Value = http
        .post(format!("{base}/v2/rpc/create_jomini_match"))
        .bearer_auth(token)
        .json(&json!({"name": "Rust hotjoin test"}))
        .send()
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .unwrap();
    let profile: Value = serde_json::from_str(created["payload"].as_str().unwrap()).unwrap();
    let room = profile["match_id"].as_str().unwrap().to_owned();
    let mut host = sdk
        .client("127.0.0.1", backend.port(), false, token)
        .unwrap();
    host.connect(WireFormat::Protobuf).unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    let own = 'join: loop {
        assert!(Instant::now() < deadline, "host timed out");
        for event in host.poll() {
            match event {
                Event::Connected => host.join(&room).unwrap(),
                Event::Joined { own, .. } => break 'join own,
                Event::Error { .. } | Event::JoinError { .. } => panic!("{event:?}"),
                _ => {}
            }
        }
        thread::sleep(Duration::from_millis(10));
    };

    let folder = tempfile::tempdir().unwrap();
    let path = folder.path().join("session_profile.json");
    write_json(&path, &json!({ "exe_sha256": EXE_SHA256, "host": "127.0.0.1", "port": backend.port(), "ssl": false,
        "match_id": room, "host_user_id": own.user_id,
        "handshake": { "compatibility_a_hex": "01".repeat(16), "compatibility_b_hex": "02".repeat(16),
            "compatibility_c_hex": "03".repeat(33), "handshake_flag": 1, "platform": 0x4e4d5453 },
    })).unwrap();
    let task = SessionTask::start(ConnectionSettings {
        game_dir: game_dir.to_string_lossy().into_owned(),
        profile_path: path.to_string_lossy().into_owned(),
        join_game: true,
        ..Default::default()
    })
    .unwrap();
    let mut mock = Peer {
        client: &mut host,
        room,
        own,
        counter: 0,
        bitmap_complete: false,
        acknowledged_peer: false,
        registered: false,
        ready: false,
        selected: false,
        snapshot_confirmed: false,
        left: false,
    };
    let mut snapshot_opened = false;
    let mut stopped = false;
    loop {
        assert!(Instant::now() < deadline, "hotjoin timed out");
        mock.poll();
        while let Ok(event) = task.try_recv() {
            match event {
                Update::GameSnapshot { path, machine } => {
                    assert_eq!(machine, 2);
                    assert_eq!(
                        World::open(&path).unwrap().character(12345).unwrap()["data"]["first_name"],
                        "Synthetic AI"
                    );
                    snapshot_opened = true;
                    task.select_character(12345).unwrap();
                    task.set_ready(true).unwrap();
                }
                Update::Ended(result) => {
                    result.unwrap();
                    assert!(mock.registered && mock.bitmap_complete && mock.snapshot_confirmed);
                    assert!(snapshot_opened && mock.selected && mock.ready && mock.left);
                    return;
                }
                _ => {}
            }
        }
        if mock.ready && mock.selected && !stopped {
            task.stop();
            stopped = true;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

struct Peer<'a, 'b> {
    client: &'a mut NativeClient<'b>,
    room: String,
    own: Presence,
    counter: u64,
    bitmap_complete: bool,
    acknowledged_peer: bool,
    registered: bool,
    ready: bool,
    selected: bool,
    snapshot_confirmed: bool,
    left: bool,
}

impl Peer<'_, '_> {
    fn poll(&mut self) {
        for event in self.client.poll() {
            let Event::Data {
                opcode: GAME_DATA_OPCODE,
                presence,
                payload,
                ..
            } = event
            else {
                continue;
            };
            let packet = crate::protocol::decode(&payload).unwrap();
            for message in packet.messages {
                if message.word_8 == 1 {
                    self.handle_control(&presence, message.payload);
                } else if message.word_8 == 0 {
                    self.handle_command(&presence, Incoming::decode(message.payload).unwrap());
                }
            }
        }
    }

    fn handle_control(&mut self, target: &Presence, packet: &[u8]) {
        match packet[0] {
            1 => {
                let mut assign = game_protocol::control(2, 32);
                assign[5..9].copy_from_slice(&2_u32.to_le_bytes());
                assign[13] = 1;
                self.send(target, 1, &assign);
            }
            5 => self.acknowledged_peer = true,
            2 => {
                assert!(self.acknowledged_peer);
                self.send(target, 1, &game_protocol::control(6, 16));
                self.transfer(target, ANNOUNCE, |_| {});
            }
            3 => {
                self.left = true;
                self.send(target, 1, &game_protocol::disconnect(7, 0));
            }
            _ => {}
        }
    }

    fn handle_command(&mut self, target: &Presence, command: Incoming<'_>) {
        assert_eq!(command.sender().unwrap(), 2);
        match command.kind {
            ADD_PLAYER => {
                assert_eq!(command.get("name").unwrap(), "AIObserver");
                self.registered = true;
            }
            REQUEST_TRANSFER => self.transfer(target, TRANSFER_HANDSHAKE, |body| {
                body.unsigned(0x61d, CHUNK_SIZE as u32)
            }),
            INITIALIZE => {
                for chunk in (0..SNAPSHOT.len().div_ceil(CHUNK_SIZE))
                    .rev()
                    .filter(|chunk| *chunk != 1)
                {
                    self.chunk(target, chunk);
                }
            }
            BITMAP => {
                let received = command.data().unwrap();
                let missing: Vec<_> = (0..SNAPSHOT.len().div_ceil(CHUNK_SIZE))
                    .filter(|chunk| received[*chunk / 8] & (1 << (*chunk % 8)) == 0)
                    .collect();
                self.bitmap_complete = missing.is_empty();
                for chunk in missing {
                    self.chunk(target, chunk);
                }
            }
            PLAYER_HERE => self.snapshot_confirmed = command.number("machineid").unwrap() == 2,
            SELECT_CHARACTER => self.selected = command.number("playable").unwrap() == 12345,
            READY => self.ready = command.get("status").unwrap() == true,
            _ => {}
        }
    }

    fn chunk(&mut self, target: &Presence, id: usize) {
        self.transfer(target, CHUNK, |body| {
            body.signed(0x6ca, id as i32);
            body.data(&SNAPSHOT[id * CHUNK_SIZE..((id + 1) * CHUNK_SIZE).min(SNAPSHOT.len())]);
        });
    }

    fn transfer(&mut self, target: &Presence, command: u16, fields: impl FnOnce(&mut Writer)) {
        let mut body = Writer::default();
        TransferInfo {
            filename: "hotjoin_0".into(),
            size: SNAPSHOT.len() as u32,
            checksum: SNAPSHOT_CHECKSUM,
        }
        .encode(&mut body)
        .unwrap();
        fields(&mut body);
        self.send(
            target,
            0,
            &Writer::command(command, self.counter as u32, 0, 2, 47, body),
        );
    }

    fn send(&mut self, target: &Presence, channel: u32, payload: &[u8]) {
        let envelope = Envelope {
            magic: Magic::Directed,
            recipient: &target.user_id,
            sequence: self.counter,
            source_user: &self.own.user_id,
            messages: vec![Message {
                word_4: 5,
                word_8: channel,
                payload,
            }],
        }
        .encode()
        .unwrap();
        self.client
            .send(
                &self.room,
                GAME_DATA_OPCODE,
                &envelope,
                std::slice::from_ref(target),
            )
            .unwrap();
        self.counter += 1;
    }
}
