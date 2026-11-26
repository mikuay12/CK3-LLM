use std::collections::BTreeMap;
use std::sync::OnceLock;

use anyhow::{Context, Result, ensure};
use serde_json::{Map, Value, json};

use crate::session::ActionRequest;

fn catalog() -> &'static BTreeMap<String, Value> {
    static ACTIONS: OnceLock<BTreeMap<String, Value>> = OnceLock::new();
    ACTIONS.get_or_init(|| {
        serde_json::from_str(include_str!("actions.json")).expect("bundled gameplay actions")
    })
}

pub(super) fn definition(name: &str) -> Result<Value> {
    let value = catalog()
        .get(name)
        .with_context(|| format!("未知的游戏操作 {name}"))?
        .clone();
    Ok(value)
}

pub(super) fn list(request: &Value) -> Result<Value> {
    let search = request["search"]
        .as_str()
        .unwrap_or_default()
        .to_lowercase();
    let start = request
        .get("offset")
        .map(|value| value.as_u64().context("offset 需要非负整数"))
        .transpose()?
        .unwrap_or(0);
    let limit = request
        .get("limit")
        .map(|value| value.as_u64().context("limit 需要正整数"))
        .transpose()?
        .unwrap_or(20);
    ensure!(
        (1..=100).contains(&limit) && start <= u32::MAX.into(),
        "分页范围无效"
    );
    let matching: Vec<_> = catalog()
        .iter()
        .filter(|(name, definition)| {
            name.contains(&search)
                || definition["label"]
                    .as_str()
                    .unwrap_or_default()
                    .to_lowercase()
                    .contains(&search)
        })
        .collect();
    let end = start + limit;
    let items: Vec<_> = matching.iter().skip(start as usize).take(limit as usize).map(|(name, definition)| {
        json!({"id": name, "label": definition["label"], "availability":definition["availability"]})
    }).collect();
    Ok(
        json!({"count":matching.len(), "offset":start, "next_offset": (end < matching.len() as u64).then_some(end), "items":items}),
    )
}

pub(super) fn prepare(
    name: &str,
    mut parameters: Map<String, Value>,
    request_id: String,
    actor: u32,
    machine: u32,
) -> Result<ActionRequest> {
    let definition = catalog()
        .get(name)
        .with_context(|| format!("未知的游戏操作 {name}"))?;
    let specs = definition["parameters"]
        .as_object()
        .expect("action parameters");
    for key in parameters.keys() {
        ensure!(specs.contains_key(key), "{name} 不接受参数 {key}");
    }
    if name == "pause_game_command" {
        controlled(&mut parameters, "player", machine)?;
    }
    for (key, spec) in specs {
        ensure!(
            spec["required"] != true || parameters.contains_key(key),
            "{name} 缺少参数 {key}"
        );
    }
    let (name, parameters, attribs) = match name {
        "send_interaction" => {
            controlled(&mut parameters, "character", actor)?;
            (name, parameters, 14)
        }
        "declare_war" => (
            "send_character_interaction",
            declare_war(parameters, actor)?,
            14,
        ),
        "start_activity_command" => {
            controlled(&mut parameters, "host", actor)?;
            ensure!(
                parameters["phases"]
                    .as_array()
                    .is_some_and(|phases| !phases.is_empty()),
                "活动至少需要一个阶段"
            );
            (
                name,
                Map::from_iter([("data".into(), Value::Object(parameters))]),
                14,
            )
        }
        "set_activity_intent_command" => {
            controlled(&mut parameters, "character", actor)?;
            let mut intent = Map::new();
            intent.insert(
                "type".into(),
                parameters.remove("intent").expect("required intent"),
            );
            if let Some(target) = parameters.remove("target") {
                intent.insert("target".into(), target);
            }
            parameters.insert("intent".into(), Value::Object(intent));
            (name, parameters, 14)
        }
        _ => (
            name,
            parameters,
            definition["header"]["attribs"]
                .as_u64()
                .expect("action attributes") as u8,
        ),
    };
    Ok(ActionRequest {
        name: name.into(),
        fields: parameters,
        attribs,
        request_id,
    })
}

fn controlled(parameters: &mut Map<String, Value>, field: &str, id: u32) -> Result<()> {
    let entry = parameters.entry(field.to_owned()).or_insert(id.into());
    ensure!(
        entry.as_u64() == Some(u64::from(id)),
        "{field} 需要使用当前控制角色或玩家的编号 {id}"
    );
    Ok(())
}

fn declare_war(mut parameters: Map<String, Value>, actor: u32) -> Result<Map<String, Value>> {
    controlled(&mut parameters, "character", actor)?;
    let recipient = parameters
        .get("target")
        .and_then(Value::as_u64)
        .and_then(|id| u32::try_from(id).ok())
        .context("target 需要角色编号")?;
    let boolean = |key: &str| {
        parameters
            .get(key)
            .map(|value| value.as_bool().with_context(|| format!("{key} 需要布尔值")))
            .transpose()
            .map(|value| value.unwrap_or(false))
    };
    let uses_hook = boolean("uses_hook")?;
    let has_hook = boolean("has_hook")?;
    let character = |id| json!({"type":"char", "identity":id});
    let target = json!({"type":"boolean", "identity":1});
    let targets = json!({
        "actor":character(actor), "recipient":character(recipient), "secondary_actor":character(u32::MAX),
        "secondary_recipient":character(u32::MAX), "intermediary":character(u32::MAX), "puppet_or_actor":character(actor),
        "is_puppet_action":{"type":"boolean"}, "hook":if uses_hook { target.clone() } else { json!({"type":"boolean"}) },
        "target":target,
    });
    let native = json!({
        "character_interaction": {
            "character_interaction":"declare_war_interaction", "actor":actor, "recipient":recipient,
            "puppet_or_actor":actor, "scope":{"root":{}, "seed":-1, "event_targets":targets},
            "original_scope":{"root":{}, "seed":-1}, "options":[uses_hook], "target":target,
            "declare_war_interaction": {
                "casus_belli":parameters["casus_belli"], "titles":parameters.get("titles").cloned().unwrap_or_else(|| json!([])),
                "claimant":parameters.get("claimant").cloned().unwrap_or_else(|| json!(u32::MAX)), "uses_hook":uses_hook, "has_hook":has_hook,
            },
        },
        "ai_intermediary_accept":100.0, "ai_accept":1.0,
    });
    Ok(native.as_object().expect("war command object").clone())
}
