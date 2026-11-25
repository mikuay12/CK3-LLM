use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

use ck3_llm::model::{self, Message, Role};
use ck3_llm::settings::{ModelSettings, Settings, read_json};
use ck3_llm::world::World;
use serde_json::{Value, json};

fn read_request(stream: &mut TcpStream) -> Value {
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut request = BufReader::new(stream);
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
    serde_json::from_slice(&body).unwrap()
}

fn reply(stream: &mut TcpStream, message: Value) {
    let reply = json!({"choices": [{"message": message}]}).to_string();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
        reply.len()
    )
    .unwrap();
}

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
            let body = read_request(&mut stream);
            assert_eq!(body["model"], "local-test");
            assert_eq!(body["stream"], false);
            assert_eq!(body["messages"][0]["role"], "system");
            assert_eq!(body["messages"][1]["content"], expected);
            if expected != "ping" {
                assert_eq!(body["messages"][0]["content"], "回复中文");
            }
            reply(&mut stream, json!({"content": "OK"}));
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
fn model_queries_the_opened_snapshot_through_a_chat_tool() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let endpoint = format!(
        "http://{}/v1/chat/completions",
        listener.local_addr().unwrap()
    );
    let server = thread::spawn(move || {
        let (mut first, _) = listener.accept().unwrap();
        let body = read_request(&mut first);
        assert_eq!(body["tools"][0]["function"]["name"], "game_query");
        reply(
            &mut first,
            json!({"content": null, "tool_calls": [{
                "id": "test-1", "type": "function", "function": {
                    "name": "game_query", "arguments": "{\"kind\":\"character\",\"id\":12345}"
                }
            }]}),
        );
        drop(first);
        let (mut second, _) = listener.accept().unwrap();
        let body = read_request(&mut second);
        let message = body["messages"].as_array().unwrap().last().unwrap();
        assert_eq!(message["role"], "tool");
        assert_eq!(message["tool_call_id"], "test-1");
        let result: Value = serde_json::from_str(message["content"].as_str().unwrap()).unwrap();
        assert_eq!(result["data"]["first_name"], "Synthetic AI");
        reply(&mut second, json!({"content":"Synthetic AI"}));
    });
    let world = World::from_bytes(include_bytes!("fixtures/hotjoin-fixture.zip").to_vec()).unwrap();
    let settings = ModelSettings {
        endpoint,
        model: "local-test".into(),
        request_timeout: 5.0,
        ..Default::default()
    };
    let result = model::chat_in_workspace(
        &settings,
        "synthetic-key",
        &[Message {
            role: Role::User,
            content: "12345 是谁？".into(),
        }],
        Some(&world),
    )
    .unwrap();
    assert_eq!(result.content, "Synthetic AI");
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
