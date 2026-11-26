use std::io::{BufRead, BufReader, Read};
use std::net::{TcpListener, TcpStream};

use crate::session::ActionStatus;
use crate::world::Query;

use super::*;

struct SyntheticGame {
    wake: Arc<AtomicBool>,
    submitted: std::sync::Mutex<Option<ActionRequest>>,
}

impl QuerySource for SyntheticGame {
    fn query(&self, query: Query<'_>) -> Result<Value> {
        ensure!(
            matches!(query, Query::Character(42)),
            "unexpected query {query:?}"
        );
        Ok(json!({"data":{"identity":{"name":"Synthetic ruler"}}}))
    }
}

impl Environment for SyntheticGame {
    fn active(&self) -> bool {
        true
    }
    fn context(&mut self) -> Result<PlayerContext> {
        Ok(PlayerContext {
            character: 42,
            name: "synthetic".into(),
            date: "1066.9.15".into(),
            tick: 12,
            generation: 0,
        })
    }
    fn start_observing(&mut self) -> Result<()> {
        Ok(())
    }
    fn notices(&mut self) -> Vec<Notice> {
        if self.wake.swap(false, Ordering::AcqRel) {
            vec![Notice {
                group: "military".into(),
                kind: "military.war_started".into(),
                text: "War started".into(),
            }]
        } else {
            Vec::new()
        }
    }
    fn sample(&self) -> Result<watch::Sample> {
        anyhow::bail!("unexpected sample")
    }
    fn world(&self) -> &dyn QuerySource {
        self
    }
    fn machine(&self) -> u32 {
        2
    }
    fn submit(
        &self,
        request: ActionRequest,
        stop: Arc<AtomicBool>,
    ) -> Result<Receiver<Result<ActionReceipt>>> {
        assert!(!stop.load(Ordering::Acquire));
        match request.name.as_str() {
            "pause_game_command" => {
                assert_eq!(request.fields["player"], 2);
                assert_eq!(request.fields["value"], true);
            }
            "send_character_interaction" => {
                assert_eq!(request.fields["character_interaction"]["actor"], 42);
                assert_eq!(request.fields["character_interaction"]["recipient"], 99);
                assert_eq!(request.fields["ai_accept"], 37.5);
                assert_eq!(request.fields["ai_intermediary_accept"], 100.0);
                crate::commands::encode(&request.name, &request.fields, 4, 2, 0, request.attribs)?;
            }
            other => panic!("unexpected action {other}"),
        }
        let previous = self.submitted.lock().unwrap().replace(request.clone());
        assert!(previous.is_none_or(|previous| previous.request_id != request.request_id));
        Ok(make_receipt(request, ActionStatus::Submitted))
    }
    fn status(&self, id: String) -> Result<Receiver<Result<ActionReceipt>>> {
        let request = self.submitted.lock().unwrap().clone().unwrap();
        assert_eq!(request.request_id, id);
        Ok(make_receipt(request, ActionStatus::HostEchoed))
    }

    fn interaction(&self, fields: &serde_json::Map<String, Value>, actor: u32) -> Result<Value> {
        crate::commands::build_interaction(fields, actor, None, &[])
    }

    fn acceptance(&self, _: &PlayerContext, _: &Value) -> Result<(f64, f64)> {
        Ok((37.5, 100.0))
    }

    fn replay_interaction(
        &self,
        request: ActionRequest,
    ) -> Result<Receiver<Result<Option<ActionReceipt>>>> {
        let (reply, receiver) = mpsc::channel();
        let previous = self.submitted.lock().unwrap();
        let existing = previous
            .as_ref()
            .filter(|previous| previous.request_id == request.request_id)
            .map(|previous| {
                ensure!(
                    previous.fields["character_interaction"]
                        == request.fields["character_interaction"],
                    "不同的互动"
                );
                Ok(ActionReceipt {
                    request_id: previous.request_id.clone(),
                    action: previous.name.clone(),
                    identity: 1,
                    status: ActionStatus::Submitted,
                    effect_verification: "synthetic",
                    error: None,
                    duplicate: true,
                })
            })
            .transpose();
        reply.send(existing)?;
        Ok(receiver)
    }
}

fn make_receipt(request: ActionRequest, status: ActionStatus) -> Receiver<Result<ActionReceipt>> {
    let (send, response) = mpsc::channel();
    send.send(Ok(ActionReceipt {
        request_id: request.request_id,
        action: request.name,
        identity: 3,
        status,
        effect_verification: "model_state_query_required",
        error: None,
        duplicate: false,
    }))
    .unwrap();
    response
}

fn request(stream: &mut TcpStream) -> Value {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut length = None;
    loop {
        let mut line = String::new();
        assert_ne!(reader.read_line(&mut line).unwrap(), 0);
        if line.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':')
            && name.eq_ignore_ascii_case("content-length")
        {
            length = Some(value.trim().parse().unwrap());
        }
    }
    let mut data = vec![0; length.unwrap()];
    reader.read_exact(&mut data).unwrap();
    serde_json::from_slice(&data).unwrap()
}

fn reply(stream: &mut TcpStream, tool: Option<(&str, Value)>) {
    let message = match tool {
        Some((name, args)) => {
            json!({"content":null,"tool_calls":[{"type":"function","id":"stub","function":{"name":name,"arguments":args.to_string()}}]})
        }
        None => json!({"content":"done"}),
    };
    let body = json!({"choices":[{"message":message}]}).to_string();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .unwrap();
}

#[test]
fn automated_player_queries_submits_checks_receipt_and_wakes_on_notice() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let settings = ModelSettings {
        endpoint: format!(
            "http://{}/v1/chat/completions",
            listener.local_addr().unwrap()
        ),
        request_timeout: 5.0,
        ..Default::default()
    };
    let stop = Arc::new(AtomicBool::new(false));
    let wake = Arc::new(AtomicBool::new(false));
    let server_stop = stop.clone();
    let server_wake = wake.clone();
    let server = thread::spawn(move || {
        let started = Instant::now();
        let interaction = json!({"action":"send_interaction","parameters":{"interaction":"gift_interaction","recipient":99},"request_id":"gift"});
        for (index, (previous, tool)) in [
            ("controlled_character", Some(("game_query", json!({"request":{"kind":"character"}})))),
            ("Synthetic ruler", Some(("game_action", json!({"action":"pause_game_command","parameters":{"value":true},"request_id":"pause"})))),
            ("submitted", Some(("game_query", json!({"request":{"kind":"action_status","id":"pause"}})))),
            ("host_echoed", Some(("game_action", interaction.clone()))),
            ("submitted", Some(("game_action", interaction))),
            ("duplicate", Some(("wait_for_update", json!({"seconds":30,"wake_on_notification":true})))),
            ("War started", None),
        ].into_iter().enumerate() {
            let mut stream = loop {
                assert!(started.elapsed() < Duration::from_secs(10), "missing request {index}");
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(10)),
                    Err(error) => panic!("{error}"),
                }
            };
            stream.set_nonblocking(false).unwrap();
            let received = request(&mut stream);
            let message = &received["messages"].as_array().unwrap().last().unwrap()["content"];
            assert!(message.as_str().unwrap().contains(previous), "missing {previous} in {message}");
            if index == 5 { server_wake.store(true, Ordering::Release); }
            if tool.is_none() { server_stop.store(true, Ordering::Release); }
            reply(&mut stream, tool);
        }
    });
    let temporary = tempfile::tempdir().unwrap();
    let (_inbox, guidance) = mpsc::channel();
    let (output, _events) = mpsc::channel();
    let client = Transport::new(&settings, "", None).unwrap();
    let mut runtime = Runtime {
        settings,
        client,
        stop,
        idle: Duration::from_millis(10),
        schema: tools::schemas(),
        goal: "Test goal".into(),
        guidance,
        exchanges: VecDeque::new(),
        updates: VecDeque::new(),
        player: None,
        last_wait: None,
        log: File::create(temporary.path().join("session.jsonl")).unwrap(),
        output,
    };
    let mut game = SyntheticGame {
        wake,
        submitted: Default::default(),
    };
    runtime.run(&mut game).unwrap();
    server.join().unwrap();
    assert!(game.submitted.lock().unwrap().is_some());
}
