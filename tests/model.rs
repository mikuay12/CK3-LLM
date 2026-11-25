use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use ck3_llm::model::{self, Message, Role};
use ck3_llm::settings::{ModelSettings, Settings, read_json};
use serde_json::Value;

#[test]
fn model_calls_pass_settings_credential_and_messages() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let endpoint = format!(
        "http://{}/v1/chat/completions",
        listener.local_addr().unwrap()
    );
    let server = thread::spawn(move || {
        for expected in ["ping", "你好"] {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = BufReader::new(&mut stream);
            let mut line = String::new();
            request.read_line(&mut line).unwrap();
            assert_eq!(line.trim_end(), "POST /v1/chat/completions HTTP/1.1");
            let mut content_length = None;
            let mut bearer = false;
            loop {
                line.clear();
                assert_ne!(request.read_line(&mut line).unwrap(), 0);
                if line.trim().is_empty() {
                    break;
                }
                let (key, value) = line.split_once(':').unwrap();
                if key.eq_ignore_ascii_case("content-length") {
                    content_length = Some(value.trim().parse::<usize>().unwrap());
                }
                if key.eq_ignore_ascii_case("authorization") {
                    bearer = value.trim() == "Bearer synthetic-key";
                }
            }
            assert!(bearer);
            let mut body = vec![0; content_length.unwrap()];
            request.read_exact(&mut body).unwrap();
            let body: Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(body["model"], "local-test");
            assert_eq!(body["stream"], false);
            assert_eq!(body["messages"][0]["role"], "system");
            assert_eq!(body["messages"][1]["content"], expected);
            if expected != "ping" {
                assert_eq!(body["messages"][0]["content"], "回复中文");
            }
            let reply = br#"{"choices":[{"message":{"content":"OK"}}]}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                reply.len()
            )
            .unwrap();
            stream.write_all(reply).unwrap();
        }
    });
    let settings = ModelSettings {
        endpoint,
        model: "local-test".into(),
        instructions: "回复中文".into(),
        request_timeout: 3.0,
        ..Default::default()
    };
    assert_eq!(
        model::test_connection(&settings, "synthetic-key")
            .unwrap()
            .status,
        200
    );
    let reply = model::chat(
        &settings,
        "synthetic-key",
        &[Message {
            role: Role::User,
            content: "你好".into(),
        }],
    )
    .unwrap();
    assert_eq!(reply.content, "OK");
    server.join().unwrap();
}

#[test]
fn imports_supported_legacy_fields_and_replaces_saved_configuration() {
    let folder = tempfile::tempdir().unwrap();
    let legacy = folder.path().join("legacy.json");
    std::fs::write(&legacy, "\u{feff}{\"model\":\"old\",\"api_key_env\":null,\"max_query_chars\":6000,\"request_timeout\":12.5}").unwrap();
    let model: ModelSettings = read_json(&legacy).unwrap();
    assert_eq!(model.model, "old");
    assert!(model.api_key_env.is_none());
    assert_eq!(model.request_timeout, 12.5);

    let destination = folder.path().join("nested").join("settings.json");
    let mut settings = Settings {
        model,
        ..Default::default()
    };
    settings.save(&destination).unwrap();
    settings.model.model = "new".into();
    settings.save(&destination).unwrap();
    assert_eq!(Settings::load(&destination).unwrap().model.model, "new");
    assert_eq!(read_json::<ModelSettings>(&legacy).unwrap().model, "old");
}
