use ck3_llm::world::World;

#[test]
fn reads_reference_hotjoin_archive_and_indexes_world_records() {
    let raw = include_bytes!("fixtures/hotjoin-fixture.zip").to_vec();
    let world = World::from_bytes(raw).unwrap();
    let summary = world.summary().unwrap();
    assert_eq!(summary["meta_data"]["version"], "1.20.1.2");
    assert_eq!(summary["date"], 51571296);
    assert_eq!(summary["containers"][0]["entry"], "hotjoin_0");
    assert_eq!(summary["record_counts"]["living"], 1);
    let character = world.character(12345).unwrap();
    assert_eq!(character["data"]["first_name"], "Synthetic AI");
    assert_eq!(character["data"]["skill"][2], 6);
    assert_eq!(world.title("c_fixture").unwrap()["data"]["holder"], 12345);
    assert_eq!(world.title("1").unwrap(), world.title("c_fixture").unwrap());
    assert_eq!(world.province(1).unwrap()["data"]["fort_level"], 2);
    assert!(world.character(23).is_err());
}

#[test]
fn refuses_a_truncated_binary_world() {
    assert!(World::from_bytes(vec![0x88, 0, 1, 0, 3, 0]).is_err());
}
