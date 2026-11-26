use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value, json};

use super::interaction::interaction_scope;
use super::{
    LIMIT, bounded, fields, group, intent, named_entries, nonempty, property, push_word, required,
    scope, text, typed, value_bytes,
};

pub(super) fn encode(value: &Value) -> Result<Vec<u8>> {
    let plan = fields(
        value,
        &[
            "type",
            "host",
            "intent",
            "start_date",
            "guest_delay_days",
            "invite_duration",
            "options",
            "phases",
            "special_guests",
            "invitations",
            "travel_plan",
            "variables",
            "tenets",
            "doctrines",
        ],
    )?;
    let mut body = Vec::new();
    if let Some(kind) = plan.get("type").filter(|value| !value.is_null()) {
        property(&mut body, 0xe1, text(nonempty(kind, "type")?)?);
    }
    changed(&mut body, plan, "host", 0x2c27, "u32", json!(u32::MAX))?;
    property(
        &mut body,
        0x362d,
        intent(plan.get("intent").unwrap_or(&json!({})))?,
    );
    property(
        &mut body,
        0xcd5,
        value_bytes("i32", required(plan, "start_date")?)?,
    );
    for (name, token) in [("guest_delay_days", 0x397d), ("invite_duration", 0x397e)] {
        property(
            &mut body,
            token,
            value_bytes("i32", plan.get(name).unwrap_or(&json!(0)))?,
        );
    }
    named(&mut body, 0x3641, plan.get("options"), |value| {
        text(nonempty(value, "活动选项")?)
    })?;
    records(&mut body, 0x3642, list(plan, "phases")?, phase)?;
    named(&mut body, 0x3613, plan.get("special_guests"), |value| {
        value_bytes("u32", value)
    })?;
    property(
        &mut body,
        0x2e52,
        buckets(list(plan, "invitations")?, "u32")?,
    );
    if let Some(travel) = plan.get("travel_plan").filter(|value| !value.is_null()) {
        property(&mut body, 0x37b6, travel_plan(travel)?);
    }
    named(&mut body, 0x555, plan.get("variables"), scope)?;
    for (name, token) in [("tenets", 0x3a63), ("doctrines", 0x3e4e)] {
        changed(&mut body, plan, name, token, "string_list", json!([]))?;
    }
    bounded(&body)?;
    Ok(group(body))
}

fn phase(value: &Value) -> Result<Vec<u8>> {
    let phase = fields(value, &["phase", "province", "guest_subsets"])?;
    let key = nonempty(required(phase, "phase")?, "phase")?;
    let mut body = Vec::new();
    property(&mut body, 0x2971, text(key)?);
    property(
        &mut body,
        0x27d2,
        value_bytes("i32", required(phase, "province")?)?,
    );
    records(&mut body, 0x3819, list(phase, "guest_subsets")?, |value| {
        let subset = fields(value, &["key", "guests"])?;
        let key = nonempty(required(subset, "key")?, "key")?;
        let mut entry = Vec::new();
        property(&mut entry, 0xdc, text(key)?);
        changed(&mut entry, subset, "guests", 0x3307, "u32_list", json!([]))?;
        Ok(entry)
    })?;
    Ok(body)
}

pub(super) fn travel_plan(value: &Value) -> Result<Vec<u8>> {
    let plan = fields(
        value,
        &[
            "owner",
            "travel_leader",
            "companions",
            "activity",
            "task_contract",
            "options",
            "departure_date",
            "departure_location",
            "destinations",
            "path_duration",
            "max_path_duration",
            "travel_with_domicile",
            "migration",
            "can_cancel_planning",
            "waypoints",
            "modifier",
            "on_arrival",
            "on_start",
            "on_travel_planner_cancel",
            "on_arrival_destinations",
            "scope",
        ],
    )?;
    let mut body = Vec::new();
    for (name, token) in [("owner", 0x2812), ("travel_leader", 0x3787)] {
        changed(&mut body, plan, name, token, "u32", json!(u32::MAX))?;
    }
    records(&mut body, 0x3789, list(plan, "companions")?, companion)?;
    for (name, token) in [("activity", 0x2816), ("task_contract", 0x399e)] {
        changed(&mut body, plan, name, token, "u32", json!(u32::MAX))?;
    }
    changed(&mut body, plan, "options", 0x3641, "string_list", json!([]))?;
    for (name, token) in [("departure_date", 0x37c0), ("departure_location", 0x37bf)] {
        property(&mut body, token, value_bytes("i32", required(plan, name)?)?);
    }
    changed(
        &mut body,
        plan,
        "destinations",
        0x37ac,
        "i32_list",
        json!([]),
    )?;
    for (name, token) in [("path_duration", 0x3981), ("max_path_duration", 0x3982)] {
        changed(&mut body, plan, name, token, "i32", json!(-1))?;
    }
    for (name, token, default) in [
        ("travel_with_domicile", 0x3b8f, false),
        ("migration", 0x3c66, false),
        ("can_cancel_planning", 0x3b49, true),
    ] {
        changed(&mut body, plan, name, token, "bool", json!(default))?;
    }
    property(&mut body, 0x37c7, buckets(list(plan, "waypoints")?, "i32")?);
    for modifier in list(plan, "modifier")? {
        property(&mut body, 0x27a9, travel_modifier(modifier)?);
        bounded(&body)?;
    }
    for (name, token) in [
        ("on_arrival", 0x37a7),
        ("on_start", 0x4bc),
        ("on_travel_planner_cancel", 0x3a14),
    ] {
        property(
            &mut body,
            token,
            trigger(plan.get(name).unwrap_or(&json!({})))?,
        );
    }
    if let Some(mode) = plan.get("on_arrival_destinations") {
        let code: u16 = match mode
            .as_str()
            .context("on_arrival_destinations 需要字符串")?
        {
            "all" => 0x465,
            "first" => 0x277f,
            "last" => 0x2c91,
            "invalid" => 0x4fc,
            "all_but_last" => 0,
            _ => bail!("无效的 on_arrival_destinations"),
        };
        if code != 0 {
            property(&mut body, 0x3a0b, code.to_le_bytes().to_vec());
        }
    }
    property(
        &mut body,
        0x523,
        interaction_scope(plan.get("scope").unwrap_or(&json!({})))?,
    );
    bounded(&body)?;
    Ok(group(body))
}

fn companion(value: &Value) -> Result<Vec<u8>> {
    let companion = fields(
        value,
        &["character", "date", "location", "source", "travel_option"],
    )?;
    let mut body = Vec::new();
    for (name, token, kind) in [
        ("character", 0x6ec, "u32"),
        ("date", 0x6b5, "i32"),
        ("location", 0x27f6, "i32"),
    ] {
        property(
            &mut body,
            token,
            value_bytes(kind, required(companion, name)?)?,
        );
    }
    if let Some(source) = companion.get("source").filter(|value| !value.is_null()) {
        let code: u16 = match source.as_str().context("同行者的 source 需要字符串")? {
            "invalid" => 0x4fc,
            "travel_leader" => 0x3787,
            "travel_option" => 0x37d2,
            "activity" => 0x2816,
            "activity_option" => 0x37d3,
            "none" => 0,
            _ => bail!("无效的旅行同行者来源"),
        };
        if code != 0 {
            property(&mut body, 0x31bd, code.to_le_bytes().to_vec());
        }
    }
    if let Some(option) = companion
        .get("travel_option")
        .filter(|value| !value.is_null())
    {
        property(&mut body, 0x37d2, text(nonempty(option, "travel_option")?)?);
    }
    Ok(body)
}

fn travel_modifier(value: &Value) -> Result<Vec<u8>> {
    let modifier = fields(value, &["modifier", "expiration_date", "multiplier"])?;
    let mut body = Vec::new();
    property(
        &mut body,
        0x27a9,
        text(nonempty(required(modifier, "modifier")?, "modifier")?)?,
    );
    property(
        &mut body,
        0x2827,
        value_bytes("i32", required(modifier, "expiration_date")?)?,
    );
    if let Some(multiplier) = modifier.get("multiplier") {
        let encoded = value_bytes("fixed", multiplier)?;
        if encoded[2..] != 100_000i64.to_le_bytes() {
            property(&mut body, 0xcd4, encoded);
        }
    }
    Ok(group(body))
}

fn trigger(value: &Value) -> Result<Vec<u8>> {
    let trigger = fields(value, &["event", "on_action"])?;
    let mut body = Vec::new();
    for (name, token) in [("event", 0x1b8), ("on_action", 0x281a)] {
        if let Some(value) = trigger
            .get(name)
            .filter(|value| !value.is_null() && *value != "")
        {
            property(&mut body, token, value_bytes("text", value)?);
        }
    }
    Ok(group(body))
}

fn buckets(items: &[Value], kind: &str) -> Result<Vec<u8>> {
    ensure!(items.len() <= LIMIT / 8, "分组数组过长");
    let mut body = Vec::new();
    property(
        &mut body,
        0x2f,
        typed(0x0c, &(items.len() as i32).to_le_bytes()),
    );
    for (index, entry) in items.iter().enumerate() {
        let values = entry.as_array().context("分组条目需要数组")?;
        if values.is_empty() {
            continue;
        }
        let mut row = Vec::new();
        for value in values {
            row.extend(value_bytes(kind, value)?);
            bounded(&row)?;
        }
        body.extend(typed(0x0c, &(index as i32).to_le_bytes()));
        push_word(&mut body, 1);
        body.extend(group(row));
        bounded(&body)?;
    }
    Ok(group(body))
}

fn list<'a>(fields: &'a Map<String, Value>, name: &str) -> Result<&'a [Value]> {
    fields.get(name).map_or(Ok(&[]), |value| {
        value
            .as_array()
            .map(Vec::as_slice)
            .with_context(|| format!("{name} 需要数组"))
    })
}

fn records(
    body: &mut Vec<u8>,
    token: u16,
    items: &[Value],
    mut encode: impl FnMut(&Value) -> Result<Vec<u8>>,
) -> Result<()> {
    if !items.is_empty() {
        let mut entries = Vec::new();
        for item in items {
            entries.extend(group(encode(item)?));
            bounded(&entries)?;
        }
        property(body, token, group(entries));
        bounded(body)?;
    }
    Ok(())
}

fn named(
    body: &mut Vec<u8>,
    token: u16,
    items: Option<&Value>,
    encode: impl FnMut(&Value) -> Result<Vec<u8>>,
) -> Result<()> {
    if let Some(items) = items {
        let entries = named_entries(items, encode)?;
        if !entries.is_empty() {
            property(body, token, group(entries));
        }
    }
    Ok(())
}

fn changed(
    body: &mut Vec<u8>,
    fields: &Map<String, Value>,
    name: &str,
    token: u16,
    kind: &str,
    default: Value,
) -> Result<()> {
    if let Some(value) = fields.get(name).filter(|&value| value != &default) {
        property(body, token, value_bytes(kind, value)?);
        bounded(body)?;
    }
    Ok(())
}
