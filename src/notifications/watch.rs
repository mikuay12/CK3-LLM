use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::{Value, json};

use crate::world::live::LiveWorld;

use super::Notice;

#[derive(Clone, Copy)]
pub enum Target {
    Character(u32),
    Machine(u32),
    LocalPlayer,
}

pub(crate) struct Sample {
    pub generation: u64,
    pub character: u32,
    pub date: String,
    pub state: Value,
    pub pending: Vec<Pending>,
}

pub(crate) struct Pending {
    pub group: &'static str,
    pub kind: &'static str,
    pub id: u32,
    pub stage: u8,
    pub text: String,
}

#[derive(Default)]
pub(crate) struct Military {
    pub units: BTreeMap<u32, Motion>,
    pub combats: BTreeMap<u32, Value>,
    pub reports: BTreeMap<u32, Value>,
    pub sieges: BTreeMap<u32, Value>,
}

pub(crate) struct Motion {
    pub moving: bool,
    pub retreating: bool,
    pub province: Option<u32>,
    pub destination: Option<u32>,
}

pub struct Task {
    game: Arc<LiveWorld>,
    updates: Receiver<Result<Vec<Notice>, String>>,
    stop: Sender<()>,
}

impl Task {
    pub fn start(game: Arc<LiveWorld>, target: Target) -> Result<Self> {
        let (stop, stopped) = mpsc::channel();
        let (send, updates) = mpsc::channel();
        let reader = game.clone();
        thread::Builder::new()
            .name("game-notifications".into())
            .spawn(move || {
                let mut history = History::default();
                let mut last_error = None;
                let mut connected = false;
                loop {
                    let progress = match reader.sample(target) {
                        Ok(sample) => {
                            let character = sample.character;
                            let generation = sample.generation;
                            let date = sample.date.clone();
                            let mut notices = history.update(sample);
                            if let Ok(military) =
                                reader.military(character, generation, &history.seen_reports)
                            {
                                notices.extend(history.update_military(character, &date, military));
                            }
                            if let Ok(messages) = reader.message_sample(character, generation) {
                                notices.extend(history.update_messages(character, &date, messages));
                            }
                            let recovered = last_error.take().is_some();
                            let send = !connected || recovered || !notices.is_empty();
                            connected = true;
                            send.then_some(Ok(notices))
                        }
                        Err(error) => {
                            let error = format!("{error:#}");
                            let changed = last_error.as_ref() != Some(&error);
                            last_error = Some(error.clone());
                            changed.then_some(Err(error))
                        }
                    };
                    if let Some(progress) = progress
                        && send.send(progress).is_err()
                    {
                        break;
                    }
                    if !reader.is_running()
                        || stopped.recv_timeout(Duration::from_secs(1))
                            != Err(RecvTimeoutError::Timeout)
                    {
                        break;
                    }
                }
            })
            .context("打开实时通知线程")?;
        Ok(Self {
            game,
            updates,
            stop,
        })
    }

    pub fn attached_to(&self, game: &Arc<LiveWorld>) -> bool {
        Arc::ptr_eq(&self.game, game)
    }

    pub fn try_recv(&self) -> Result<Result<Vec<Notice>, String>, TryRecvError> {
        self.updates.try_recv()
    }
}

impl Drop for Task {
    fn drop(&mut self) {
        let _ = self.stop.send(());
    }
}

type PendingKey = (&'static str, u32, u8);

#[derive(Default)]
struct History {
    subject: Option<(u64, u32)>,
    previous: BTreeMap<&'static str, Value>,
    wars: BTreeSet<u32>,
    pending: HashSet<PendingKey>,
    military: Military,
    seen_reports: BTreeSet<u32>,
    seen_messages: HashSet<String>,
    visible_alerts: HashSet<String>,
}

const FIELDS: [(&str, &str); 13] = [
    ("character_died", "角色去世"),
    ("titles_changed", "直属头衔变化"),
    ("primary_title_changed", "主头衔变化"),
    ("capital_changed", "首都变化"),
    ("government_changed", "政体变化"),
    ("liege_changed", "领主变化"),
    ("succession_changed", "继承顺位变化"),
    ("laws_changed", "法律变化"),
    ("family_changed", "家庭变化"),
    ("traits_changed", "特质变化"),
    ("faith_changed", "信仰变化"),
    ("culture_changed", "文化变化"),
    ("stress_level_changed", "压力等级变化"),
];

impl History {
    fn update_messages(&mut self, character: u32, date: &str, items: Vec<Value>) -> Vec<Notice> {
        let mut notices = Vec::new();
        let mut alerts = HashSet::new();
        for item in items {
            let Some(key) = item["type"].as_str() else {
                continue;
            };
            let id = item["id"].to_string();
            let (group, kind) = if item["source"] == "native_important_action" {
                alerts.insert(id.clone());
                if self.visible_alerts.contains(&id) {
                    continue;
                }
                ("important_actions", format!("action.{key}"))
            } else {
                if !self.seen_messages.insert(id) {
                    continue;
                }
                ("messages", format!("message.{key}"))
            };
            let label = item["title"]
                .as_str()
                .filter(|text| !text.is_empty())
                .unwrap_or(key);
            let label: String = label.chars().take(300).collect();
            notices.push(Notice {
                group: group.into(),
                kind,
                text: format!("{date} · 人物 {character} · {label}"),
            });
        }
        self.visible_alerts = alerts;
        notices
    }

    fn update_military(
        &mut self,
        character: u32,
        date: &str,
        mut current: Military,
    ) -> Vec<Notice> {
        let mut notices = Vec::new();
        let mut push = |kind: &str, date: &str, text: String| {
            notices.push(Notice {
                group: "military".into(),
                kind: format!("military.{kind}"),
                text: format!("{date} · 人物 {character} · {text}"),
            })
        };
        for (&id, motion) in &current.units {
            if let Some(before) = self.military.units.get(&id) {
                let place = json!(motion.province);
                let destination = json!(motion.destination);
                let change = if before.retreating != motion.retreating {
                    Some(if motion.retreating {
                        ("retreat_started", "开始撤退")
                    } else {
                        ("retreat_ended", "撤退结束")
                    })
                } else if before.moving != motion.moving {
                    Some(if motion.moving {
                        ("movement_started", "开始行军")
                    } else {
                        ("movement_stopped", "行军结束")
                    })
                } else {
                    None
                };
                if let Some((kind, label)) = change {
                    let suffix = if motion.moving {
                        format!("，目标 {destination}")
                    } else {
                        String::new()
                    };
                    push(
                        kind,
                        date,
                        format!("单位 {id} {label}，省份 {place}{suffix}"),
                    );
                }
            }
        }
        for (&id, combat) in &current.combats {
            let province = &combat["province_id"];
            let phase = combat["phase"].as_str().unwrap_or("unknown");
            match self.military.combats.get(&id) {
                None => push(
                    "battle_started",
                    date,
                    format!("交战 {id} 开始，省份 {province}"),
                ),
                Some(before) if before["phase"] != phase => push(
                    "battle_phase_changed",
                    date,
                    format!("交战 {id} 进入 {phase} 阶段"),
                ),
                _ => {}
            }
        }
        for (&id, report) in &current.reports {
            if self.seen_reports.insert(id) {
                let winner = match report["winning_side"].as_str() {
                    Some("attacker") => "进攻方获胜",
                    Some("defender") => "防守方获胜",
                    _ => "胜负待确认",
                };
                push(
                    "battle_result",
                    report["end_date"].as_str().unwrap_or(date),
                    format!("战报 {id}，省份 {}，{winner}", report["province_id"]),
                );
            }
        }
        for (&id, siege) in &current.sieges {
            let province = &siege["province_id"];
            let side = if siege["side"] == "attacker" {
                "进攻方"
            } else {
                "防守方"
            };
            match self.military.sieges.get(&id) {
                None => push(
                    "siege_started",
                    date,
                    format!("围城 {id} 开始，省份 {province}，你方为{side}"),
                ),
                Some(before)
                    if siege["effects"]["breach"].as_u64()
                        > before["effects"]["breach"].as_u64() =>
                {
                    push(
                        "siege_breach",
                        date,
                        format!("围城 {id} 出现城墙缺口，省份 {province}"),
                    )
                }
                _ => {}
            }
        }
        for (&id, previous) in &self.military.sieges {
            if !current.sieges.contains_key(&id) {
                push(
                    "siege_ended",
                    date,
                    format!("围城 {id} 已结束，省份 {}", previous["province_id"]),
                );
            }
        }
        current.reports.clear();
        self.military = current;
        notices
    }

    fn update(&mut self, sample: Sample) -> Vec<Notice> {
        let subject = (sample.generation, sample.character);
        let same_world = self.subject == Some(subject);
        if !same_world {
            *self = Self::default();
            self.subject = Some(subject);
        }
        let make = |group: &str, kind: &str, text: String| Notice {
            group: group.to_owned(),
            kind: kind.to_owned(),
            text: format!("{} · 人物 {} · {text}", sample.date, sample.character),
        };
        let fields = character_fields(&sample.state);
        let wars: BTreeSet<u32> = sample.state["military"]["war_ids"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|id| id.as_u64().and_then(|id| u32::try_from(id).ok()))
            .collect();
        let mut notices = Vec::new();
        if same_world {
            for (kind, label) in FIELDS {
                let before = &self.previous[kind];
                let after = &fields[kind];
                if kind == "character_died" {
                    if before == true && after == false {
                        notices.push(make("personal_state", "state.character_died", label.into()));
                    }
                } else if before != after {
                    notices.push(make(
                        "personal_state",
                        &format!("state.{kind}"),
                        format!("{label}  {before} → {after}"),
                    ));
                }
            }
            if let (Some(before), Some(after)) =
                (self.previous["debt"].as_bool(), fields["debt"].as_bool())
                && before != after
            {
                let (kind, text) = if after {
                    ("state.debt_entered", "陷入债务")
                } else {
                    ("state.debt_cleared", "还清债务")
                };
                notices.push(make("personal_state", kind, text.into()));
            }
            for war in wars.difference(&self.wars) {
                notices.push(make(
                    "military",
                    "military.war_started",
                    format!("加入战争 {war}"),
                ));
            }
            for war in self.wars.difference(&wars) {
                notices.push(make(
                    "military",
                    "military.war_ended",
                    format!("退出战争 {war}"),
                ));
            }
        }
        let mut pending = HashSet::new();
        for item in sample.pending {
            let key = (item.kind, item.id, item.stage);
            if pending.insert(key) && !self.pending.contains(&key) {
                notices.push(make(item.group, &format!("state.{}", item.kind), item.text));
            }
        }
        self.previous = fields;
        self.wars = wars;
        self.pending = pending;
        notices
    }
}

fn sorted(value: &Value) -> Value {
    let mut entries = value.as_array().cloned().unwrap_or_default();
    entries.sort_unstable_by_key(Value::to_string);
    Value::Array(entries)
}

fn character_fields(data: &Value) -> BTreeMap<&'static str, Value> {
    let identity = &data["identity"];
    let domain = &data["domain"];
    let resources = &data["resources"];
    let mut family = data["family"].clone();
    if let Some(fields) = family.as_object_mut() {
        for key in ["child_ids", "spouse_ids"] {
            fields.insert(key.into(), sorted(fields.get(key).unwrap_or(&Value::Null)));
        }
    }
    BTreeMap::from([
        ("character_died", identity["alive"].clone()),
        ("titles_changed", sorted(&domain["title_ids"])),
        (
            "primary_title_changed",
            domain["primary_title"]["id"].clone(),
        ),
        ("capital_changed", domain["capital_title_id"].clone()),
        ("government_changed", domain["government"].clone()),
        ("liege_changed", domain["liege_id"].clone()),
        (
            "succession_changed",
            json!(
                domain["succession_ids"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
            ),
        ),
        ("laws_changed", sorted(&domain["laws"])),
        ("family_changed", family),
        ("traits_changed", sorted(&data["traits"])),
        ("faith_changed", identity["faith"].clone()),
        ("culture_changed", identity["culture"].clone()),
        (
            "debt",
            json!(resources["gold"].as_f64().map(|gold| gold < 0.0)),
        ),
        (
            "stress_level_changed",
            json!(
                resources["stress"]
                    .as_i64()
                    .map(|stress| (stress / 100).clamp(0, 3))
            ),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notifications_deduplicate_pending_items_and_reset_on_world_change() {
        let sample = |generation, capital, titles, gold, wars| Sample {
            generation,
            character: 42,
            date: "1066.9.15".into(),
            state: json!({"identity": {"alive":true}, "domain": {"capital_title_id": capital, "title_ids": titles},
                "resources": {"gold":gold}, "military": {"war_ids":wars}}),
            pending: vec![Pending {
                group: "pending_events",
                kind: "pending_event",
                id: 5,
                stage: 0,
                text: "事件 5".into(),
            }],
        };
        let mut history = History::default();
        let first = history.update(sample(0, 123, [123, 124], 5, vec![]));
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].kind, "state.pending_event");
        let updated = history.update(sample(0, 124, [124, 123], -5, vec![12]));
        let kinds: Vec<_> = updated.iter().map(|notice| notice.kind.as_str()).collect();
        assert_eq!(
            kinds,
            [
                "state.capital_changed",
                "state.debt_entered",
                "military.war_started"
            ]
        );
        let reloaded = history.update(sample(1, 123, [123, 124], 5, vec![]));
        assert_eq!(reloaded.len(), 1);
        assert_eq!(reloaded[0].kind, "state.pending_event");
    }
}
