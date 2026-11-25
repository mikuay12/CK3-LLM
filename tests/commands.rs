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
    assert_eq!(fixtures.len(), catalog().len());
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
}
