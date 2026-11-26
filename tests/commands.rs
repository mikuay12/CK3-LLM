use std::collections::BTreeSet;

use ck3_llm::commands::{catalog, encode};
use serde::Deserialize;
use serde_json::{Map, Value, json};

#[derive(Deserialize)]
struct Fixture {
    name: String,
    fields: Map<String, Value>,
    hex: String,
}

#[test]
fn compares_every_migrated_command_with_reference_bytes() {
    let fixtures: Vec<Fixture> =
        serde_json::from_str(include_str!("fixtures/action-vectors.json")).unwrap();
    let names: BTreeSet<_> = fixtures
        .iter()
        .map(|fixture| fixture.name.as_str())
        .collect();
    assert_eq!(names, catalog().keys().map(String::as_str).collect());
    for fixture in fixtures {
        let command = encode(&fixture.name, &fixture.fields, 23, 2, 0, 14)
            .unwrap_or_else(|error| panic!("{} {error:#}", fixture.name));
        let hex: String = command.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(hex, fixture.hex, "{}", fixture.name);
    }
}

#[test]
fn invalid_input_never_builds_a_command() {
    let malformed = json!({"machineid": 2, "status": 1});
    assert!(
        encode(
            "set_ready_command",
            malformed.as_object().unwrap(),
            0,
            2,
            0,
            14
        )
        .is_err()
    );
    assert!(encode("unknown", &Map::new(), 0, 2, 0, 14).is_err());
    assert!(encode("set_ready_command", &Map::new(), 0, 2, 0, 14).is_err());

    for (name, malformed) in [
        (
            "start_activity_command",
            json!({"data": {"start_date": 54296760, "invitations": [42]}}),
        ),
        (
            "join_activity_command",
            json!({"travel_plan_draft_created": {"departure_date": 54296760}}),
        ),
        (
            "send_character_interaction",
            json!({
                "character_interaction": {"character_interaction": "declare_war_interaction", "scope": {},
                    "original_scope": {}, "actor": 42, "recipient": 99},
                "ai_intermediary_accept": 100.0, "ai_accept": 1.0
            }),
        ),
    ] {
        assert!(encode(name, malformed.as_object().unwrap(), 0, 2, 0, 14).is_err());
    }
}
