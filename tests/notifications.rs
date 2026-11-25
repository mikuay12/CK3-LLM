use ck3_llm::notifications::{Policy, load_history};

#[test]
fn reopens_legacy_notice_history_and_applies_existing_preferences() {
    let root = tempfile::tempdir().unwrap();
    let history = root.path().join("events.jsonl");
    std::fs::write(&history, concat!(
        "{\"event\":\"packet\",\"type\":\"ignored\"}\n",
        "{\"event\":\"game_notification\",\"group\":\"military\",\"notification_id\":\"battle_started\",\"title\":\"交战开始\"}\n",
        "{\"event\":\"game_notification\",\"group\":\"pending_events\",\"type\":\"pending_event\",\"message\":\"待处理事件\"}\n",
    )).unwrap();
    let old = root.path().join("notification_policy.json");
    std::fs::write(
        &old,
        "\u{feff}{\"version\":1,\"disabled_groups\":[\"military\"],\"disabled_types\":[]}",
    )
    .unwrap();
    let mut policy = Policy::load(&old).unwrap();
    let notices = load_history(&history).unwrap();
    assert_eq!(notices.len(), 2);
    assert!(!policy.allows(&notices[0]));
    assert!(policy.allows(&notices[1]));
    policy.disabled_types.insert("pending_event".into());
    let current = root.path().join("current.json");
    policy.save(&current).unwrap();
    assert!(!Policy::load(&current).unwrap().allows(&notices[1]));
    assert!(Policy::load(&old).unwrap().allows(&notices[1]));
}
