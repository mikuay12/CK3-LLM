use std::io::Write;

use crate::commands;
use crate::protocol::{Envelope, Magic, Message};
use crate::world::World;
use crate::world::{Query, QuerySource, store::WorldStore};

use super::super::transfer::Checksum;
use super::*;

const SNAPSHOT: &[u8] = include_bytes!("../../../tests/fixtures/hotjoin-fixture.zip");

#[test]
fn replay_verifies_fast_and_serial_transfers_and_indexes_messages() {
    let folder = tempfile::tempdir().unwrap();
    let capture = folder.path().join("source");
    fs::create_dir(&capture).unwrap();
    let log_path = capture.join("events.jsonl");
    let mut log = File::create(&log_path).unwrap();
    let mut sequence = 0;
    let mut record = |name: &str, fields: Value| {
        let command =
            commands::encode(name, fields.as_object().unwrap(), sequence as u32, 0, 2, 47).unwrap();
        let payload = Envelope {
            magic: Magic::Directed,
            recipient: "client",
            source_user: "host",
            sequence,
            messages: vec![Message {
                word_4: 5,
                word_8: 0,
                payload: &command,
            }],
        }
        .encode()
        .unwrap();
        let filename = format!("{sequence:04}-rx.bin");
        fs::write(capture.join(&filename), &payload).unwrap();
        writeln!(log, "{}", json!({"event":"match_data", "opcode":255, "match_id":"legacy",
            "presence":{"user_id":"host"}, "payload_file":filename,
            "payload_size":payload.len(), "payload_sha256":format!("{:x}", Sha256::digest(payload)),
        })).unwrap();
        sequence += 1;
    };

    record("client_ping", json!({"machineid":1, "turn":83}));
    for (filename, serial, size) in [
        ("hotjoin_fast", false, 64),
        ("hotjoin_serial", true, 37),
        ("hotjoin_legacy", false, 68),
    ] {
        let info =
            json!({"filename":filename, "size":SNAPSHOT.len(), "checksum":Checksum::of(SNAPSHOT)});
        record("filetransfer_init_command", info.clone());
        let mut meta = info.clone();
        if serial {
            meta["sizes"] = json!(SNAPSHOT.chunks(size).map(<[u8]>::len).collect::<Vec<_>>());
            meta["checksums"] = json!(SNAPSHOT.chunks(size).map(Checksum::of).collect::<Vec<_>>());
            meta["machineid"] = json!(2);
            record("filetransfer_send_chunk_metas_command", meta);
        } else if filename != "hotjoin_legacy" {
            meta["filetransfer_fast_fld_chunk_size"] = json!(size);
            record("filetransfer_fast_cmd_handshake", meta);
        }
        for (index, chunk) in SNAPSHOT.chunks(size).enumerate().rev() {
            let mut fields = info.clone();
            fields["chunk_id"] = json!(index);
            fields["data"] =
                json!({"hex": chunk.iter().map(|byte| format!("{byte:02x}")).collect::<String>()});
            let command = if serial {
                "filetransfer_send_chunk_command"
            } else {
                "filetransfer_fast_cmd_chunk"
            };
            record(command, fields.clone());
            if index == 2 {
                record(command, fields);
            }
        }
    }
    writeln!(log, "{}", json!({"event":"match_data", "opcode":255, "payload_file":"0000-rx.bin", "payload_sha256":"invalid"})).unwrap();
    drop(log);
    let replay = Replay::open(&log_path, &folder.path().join("replayed")).unwrap();
    assert_eq!(replay.files.len(), 3);
    assert_eq!(replay.summary.decode_errors, 1);
    assert_eq!(replay.summary.incomplete_transfers, 0);
    assert_eq!(replay.summary.synchronization.peer_turns[&1], 83);
    assert_eq!(
        replay.records[0].describe().unwrap()["message"]["fields"]["turn"],
        83
    );
    for file in &replay.files {
        assert_eq!(fs::read(&file.path).unwrap(), SNAPSHOT);
        assert_eq!(
            World::open(&file.path).unwrap().character(12345).unwrap()["data"]["first_name"],
            "Synthetic AI"
        );
    }
    assert!(replay.output.join("summary.json").is_file());
    let database = folder.path().join("replay.sqlite");
    for _ in 0..2 {
        replay
            .save_database(&replay.files[0].path, &database)
            .unwrap();
        let store = WorldStore::open(&database).unwrap();
        assert_eq!(
            store.query(Query::Character(12345)).unwrap()["data"]["first_name"],
            "Synthetic AI"
        );
        assert_eq!(
            store.query(Query::Title("c_fixture")).unwrap()["data"]["holder"],
            12345
        );
        assert_eq!(
            store.query(Query::Summary).unwrap()["observed_commands"],
            replay.records.len()
        );
        let commands = store
            .query(Query::ObservedCommands {
                name: "client_ping",
                offset: 0,
                limit: 5,
            })
            .unwrap();
        assert_eq!(commands["items"].as_array().unwrap().len(), 1);
        assert_eq!(commands["items"][0]["command"]["fields"]["turn"], 83);
    }
}
