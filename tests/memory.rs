use ck3_llm::memory::{Category, Scope, Store};

#[test]
fn imports_legacy_memory_merges_and_excludes_disabled_items() {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("llm_memory.json");
    let legacy = directory.path().join("legacy.json");
    std::fs::write(&legacy, r#"{"version":1,"items":[{"id":"sample","scope":"campaign","category":"strategy","content":"Collect allies","updated_at":"2026-11-25 00:00:00"},{"id":"hidden","scope":"experience","category":"lesson","content":"Private note","enabled":false}]}"#).unwrap();
    let mut store = Store::load(destination.clone()).unwrap();
    assert_eq!(store.import(&legacy).unwrap(), 2);
    assert_eq!(store.import(&legacy).unwrap(), 0);
    store
        .add(Scope::Campaign, Category::Pact, "Protect  the host")
        .unwrap();
    let prompt = store.prompt(1500);
    assert!(prompt.contains("Protect the host"));
    assert!(prompt.contains("Collect allies"));
    assert!(!prompt.contains("Private note"));
    store.save().unwrap();
    assert_eq!(Store::load(destination).unwrap().items.len(), 3);
}

#[test]
fn saving_checks_for_an_intervening_writer() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("llm_memory.json");
    let mut first = Store::load(path.clone()).unwrap();
    let mut stale = Store::load(path.clone()).unwrap();
    first
        .add(Scope::Campaign, Category::Pact, "Keep the alliance")
        .unwrap();
    first.save().unwrap();
    stale
        .add(Scope::Experience, Category::Lesson, "Try again")
        .unwrap();
    assert!(stale.save().is_err());
    assert_eq!(
        Store::load(path).unwrap().items[0].content,
        "Keep the alliance"
    );
}
