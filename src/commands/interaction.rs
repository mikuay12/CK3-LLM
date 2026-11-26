use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use super::{
    LIMIT, Specification, bounded, fields, group, named_entries, nonempty, property, push_word,
    required, scope, text, typed, unsigned, value_bytes,
};
use std::collections::BTreeMap;
use std::sync::OnceLock;

const WAR: &str = "declare_war_interaction";
const REFERENCES: [(&str, u16); 4] = [
    ("secondary_actor", 0x2bdc),
    ("secondary_recipient", 0x2bdd),
    ("intermediary", 0x373f),
    ("puppet_or_actor", 0x3f07),
];

#[derive(serde::Deserialize)]
struct Catalog {
    names: BTreeMap<String, Option<String>>,
    payloads: BTreeMap<String, Specification>,
}

fn catalog() -> &'static Catalog {
    static CATALOG: OnceLock<Catalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(include_str!("interaction_specs.json"))
            .expect("bundled CK3 character interaction specifications")
    })
}

pub(crate) fn interaction_spec(name: &str) -> Option<(Option<&'static str>, Vec<&'static str>)> {
    let catalog = catalog();
    let special = catalog.names.get(name)?.as_deref();
    let parameters = special
        .and_then(|key| catalog.payloads.get(key))
        .map_or_else(Vec::new, |spec| {
            spec.fields
                .iter()
                .map(|field| field.name.as_str())
                .collect()
        });
    Some((special, parameters))
}

pub(crate) fn build_interaction(
    parameters: &serde_json::Map<String, Value>,
    actor: u32,
    target_type: Option<&str>,
    options: &[(String, String)],
) -> Result<Value> {
    let name = nonempty(required(parameters, "interaction")?, "interaction")?;
    let (special, _) = interaction_spec(name).context("未支持的人物互动")?;
    let character = |key: &str| -> Result<u32> {
        parameters
            .get(key)
            .map(unsigned)
            .transpose()
            .map(|id| id.unwrap_or(u32::MAX))
    };
    let recipient = character("recipient")?;
    ensure!(recipient != u32::MAX, "互动需要接收者");
    let secondary_actor = character("secondary_actor")?;
    let secondary_recipient = character("secondary_recipient")?;
    if name == "arrange_marriage_interaction" {
        ensure!(
            secondary_actor != u32::MAX && secondary_recipient != u32::MAX,
            "婚姻互动需要双方拟婚角色"
        );
    }
    let target = parameters.get("target").filter(|value| !value.is_null());
    let expected = match target_type {
        Some("title") => Some("landed_title"),
        Some("artifact") => Some("artifact"),
        Some("court_position_type") => Some("court_position_type"),
        None => None,
        Some(kind) => anyhow::bail!("暂不支持互动目标类型 {kind}"),
    };
    if let Some(expected) = expected {
        let target = target.context("互动缺少目标")?;
        ensure!(
            target["type"] == expected && target["identity"].as_u64().is_some(),
            "互动需要 {expected} 类型的目标及其编号"
        );
    }
    let end_war = matches!(
        special,
        Some(
            "end_war_attacker_victory_interaction"
                | "end_war_white_peace_interaction"
                | "end_war_attacker_defeat_interaction"
        )
    ) && target.is_some_and(|value| value["type"] == "war");
    let reference = |id| json!({"type":"char", "identity":id});
    let mut scopes = json!({"actor":reference(actor), "recipient":reference(recipient),
        "secondary_actor":reference(secondary_actor), "secondary_recipient":reference(secondary_recipient),
        "intermediary":reference(u32::MAX), "puppet_or_actor":reference(actor), "is_puppet_action":{"type":"boolean"}});
    if let Some(target) = target {
        scopes[if end_war { "war" } else { "target" }] = target.clone();
    }
    let flags = parameters
        .get("option_flags")
        .filter(|value| !value.is_null())
        .map(|value| value.as_object().context("互动选项需要具名布尔值"))
        .transpose()?;
    if let Some(flags) = flags {
        for key in flags.keys() {
            ensure!(
                options.iter().any(|(name, _)| name == key),
                "未知的互动选项 {key}"
            );
        }
    }
    let mut ordered = Vec::with_capacity(options.len());
    for (key, _) in options {
        ensure!(
            scopes.get(key).is_none() && key != "target",
            "互动选项与作用域重名 {key}"
        );
        let set = flags
            .and_then(|flags| flags.get(key))
            .map(|value| value.as_bool().context("互动选项需要布尔值"))
            .transpose()?
            .unwrap_or(false);
        ordered.push(set);
        scopes[key] = if set {
            json!({"type":"boolean", "identity":1})
        } else {
            json!({"type":"boolean"})
        };
    }
    let mut data = json!({"character_interaction":name, "actor":actor, "recipient":recipient,
        "secondary_actor":secondary_actor, "secondary_recipient":secondary_recipient, "intermediary":u32::MAX,
        "puppet_or_actor":actor, "scope":{"root":target.filter(|_| end_war).cloned().unwrap_or_else(|| json!({})),
        "seed":-1, "event_targets":scopes}, "original_scope":{"root":{}, "seed":-1}, "options":ordered});
    if let Some(target) = target.filter(|_| !end_war) {
        data["target"] = target.clone();
    }
    let payload = parameters
        .get("special_fields")
        .filter(|value| !value.is_null());
    if let Some(special) = special.filter(|key| catalog().payloads.contains_key(*key)) {
        data[special] = payload.cloned().unwrap_or_else(|| match (special, target) {
            ("revoke_title_interaction" | "diarch_revoke_title", Some(target)) => {
                json!({"title":target["identity"]})
            }
            ("grant_titles_interaction" | "grant_governorship_interaction", Some(target)) => {
                json!({"titles":[target["identity"]]})
            }
            _ => json!({}),
        });
    } else if let Some(payload) = payload {
        ensure!(
            payload.as_object().is_some_and(serde_json::Map::is_empty),
            "此互动没有特化载荷"
        );
    }
    encode(&data)?;
    Ok(data)
}

pub(super) fn encode(value: &Value) -> Result<Vec<u8>> {
    let object = value.as_object().context("人物互动需要对象")?;
    let name = nonempty(
        required(object, "character_interaction")?,
        "character_interaction",
    )?;
    let specs = catalog();
    let payload = if name == WAR {
        Some(WAR)
    } else {
        specs
            .names
            .get(name)
            .with_context(|| format!("未支持的人物互动 {name}"))?
            .as_deref()
            .filter(|special| specs.payloads.contains_key(*special))
    };
    let mut allowed = vec![
        "character_interaction",
        "scope",
        "original_scope",
        "actor",
        "recipient",
        "options",
        "target",
    ];
    allowed.extend(REFERENCES.map(|(key, _)| key));
    allowed.extend(payload);
    let input = fields(value, &allowed)?;

    let mut body = Vec::new();
    property(&mut body, 0x28a9, text(name)?);
    property(
        &mut body,
        0x523,
        interaction_scope(required(input, "scope")?)?,
    );
    property(
        &mut body,
        0x2fe1,
        interaction_scope(required(input, "original_scope")?)?,
    );
    property(
        &mut body,
        0x28aa,
        value_bytes("u32", required(input, "actor")?)?,
    );
    property(
        &mut body,
        0x28ab,
        value_bytes("u32", required(input, "recipient")?)?,
    );
    for (name, token) in REFERENCES {
        if let Some(value) = input.get(name)
            && unsigned(value)? != u32::MAX
        {
            property(&mut body, token, value_bytes("u32", value)?);
        }
    }
    if let Some(options) = input.get("options") {
        let options = options.as_array().context("人物互动选项需要布尔数组")?;
        ensure!(options.len() <= LIMIT / 16, "人物互动选项超过一帧大小");
        if !options.is_empty() {
            let mut sparse = typed(0x0c, &(options.len() as i32).to_le_bytes());
            for (index, value) in options.iter().enumerate() {
                if value.as_bool().context("人物互动选项需要布尔值")? {
                    sparse.extend(typed(0x0c, &(index as i32).to_le_bytes()));
                    push_word(&mut sparse, 1);
                    sparse.extend(typed(0x0e, &[1]));
                }
            }
            property(&mut body, 0x3641, group(sparse));
        }
    }
    if let Some(target) = input.get("target") {
        let bytes = scope(target)?;
        if bytes.len() > 4 {
            property(&mut body, 0x6b, bytes);
        }
    }
    if let Some(payload) = payload {
        let value = required(input, payload)?;
        if payload == WAR {
            property(&mut body, 0x2cea, declaration(value)?);
        } else {
            let spec = &specs.payloads[payload];
            property(&mut body, spec.token, special_payload(value, spec)?);
        }
    }
    bounded(&body)?;
    Ok(group(body))
}

pub(super) fn interaction_scope(value: &Value) -> Result<Vec<u8>> {
    let input = fields(value, &["root", "seed", "event_targets"])?;
    let mut body = Vec::new();
    property(
        &mut body,
        0x6ac,
        scope(input.get("root").unwrap_or(&json!({})))?,
    );
    property(
        &mut body,
        0x6f7,
        value_bytes("i32", input.get("seed").unwrap_or(&json!(-1)))?,
    );
    if let Some(values) = input.get("event_targets") {
        let entries = named_entries(values, scope)?;
        if !entries.is_empty() {
            property(&mut body, 0x6f8, group(entries));
        }
    }
    Ok(group(body))
}

fn declaration(value: &Value) -> Result<Vec<u8>> {
    let war = fields(
        value,
        &["casus_belli", "titles", "claimant", "uses_hook", "has_hook"],
    )?;
    let mut body = Vec::new();
    property(
        &mut body,
        0x289a,
        text(nonempty(required(war, "casus_belli")?, "casus_belli")?)?,
    );
    if let Some(titles) = war.get("titles") {
        let encoded = value_bytes("u32_list", titles)?;
        if encoded.len() > 4 {
            property(&mut body, 0x27d5, encoded);
        }
    }
    for (key, token, kind, default) in [
        ("claimant", 0x2daf, "u32", json!(u32::MAX)),
        ("uses_hook", 0x3cf6, "bool", json!(false)),
        ("has_hook", 0x3cf7, "bool", json!(false)),
    ] {
        property(
            &mut body,
            token,
            value_bytes(kind, war.get(key).unwrap_or(&default))?,
        );
    }
    Ok(group(body))
}

fn special_payload(value: &Value, spec: &Specification) -> Result<Vec<u8>> {
    let keys: Vec<_> = spec
        .fields
        .iter()
        .map(|field| field.name.as_str())
        .collect();
    let input = fields(value, &keys)?;
    let mut body = Vec::new();
    for field in &spec.fields {
        let value = input.get(&field.name).or(field.default.as_ref());
        let Some(value) = value.filter(|value| !value.is_null() || field.kind == "culture_traits")
        else {
            ensure!(field.optional, "缺少互动字段 {}", field.name);
            continue;
        };
        if field.kind == "culture_traits" {
            property(&mut body, field.token, culture_traits(value)?);
        } else {
            let encoded = value_bytes(&field.kind, value)?;
            if field.default.as_ref() != Some(value) {
                property(&mut body, field.token, encoded);
            }
        }
        bounded(&body)?;
    }
    Ok(group(body))
}

fn culture_traits(value: &Value) -> Result<Vec<u8>> {
    const PILLARS: [(&str, u16); 5] = [
        ("ethos", 0x352d),
        ("heritage", 0x352f),
        ("language", 0xbb),
        ("martial_custom", 0x36ad),
        ("head_determination", 0x3d57),
    ];
    let mut keys = vec!["traditions"];
    keys.extend(PILLARS.map(|(key, _)| key));
    let input = fields(value, &keys)?;
    let mut body = Vec::new();
    if let Some(values) = input.get("traditions") {
        let values = value_bytes("string_list", values)?;
        if values.len() > 4 {
            property(&mut body, 0x352e, values);
        }
    }
    for (key, token) in PILLARS {
        if let Some(value) = input.get(key).filter(|value| !value.is_null()) {
            property(&mut body, token, value_bytes("text", value)?);
        }
    }
    bounded(&body)?;
    Ok(group(body))
}
