use std::collections::BTreeMap;
use std::sync::OnceLock;

use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Map, Value};

use crate::session::game_protocol::Writer;

const LIMIT: usize = (1 << 20) - 60;

#[derive(Deserialize)]
pub struct Specification {
    token: u16,
    pub fields: Vec<Field>,
}

#[derive(Deserialize)]
pub struct Field {
    pub name: String,
    token: u16,
    pub kind: String,
    pub default: Option<Value>,
    pub optional: bool,
    pub choices: BTreeMap<String, i32>,
}

pub fn catalog() -> &'static BTreeMap<String, Specification> {
    static SCHEMAS: OnceLock<BTreeMap<String, Specification>> = OnceLock::new();
    SCHEMAS.get_or_init(|| {
        serde_json::from_str(include_str!("session/command_specs.json"))
            .expect("bundled CK3 command specifications")
    })
}

pub fn encode(
    name: &str,
    fields: &Map<String, Value>,
    identity: u32,
    sender: i32,
    receiver: i32,
    attribs: u8,
) -> Result<Vec<u8>> {
    let spec = catalog()
        .get(name)
        .with_context(|| format!("未迁移的游戏命令 {name}"))?;
    for key in fields.keys() {
        ensure!(
            spec.fields.iter().any(|field| &field.name == key),
            "命令包含未知字段 {key}"
        );
    }
    let mut data = Vec::new();
    for field in &spec.fields {
        let value = match fields.get(&field.name) {
            Some(value) => value,
            None if field.optional => continue,
            None => field
                .default
                .as_ref()
                .with_context(|| format!("缺少命令字段 {}", field.name))?,
        };
        let encoded = match field.kind.as_str() {
            "token_value" | "i32_enum" => {
                let key = value.as_str().context("枚举字段需要选项名称")?;
                let option = *field
                    .choices
                    .get(key)
                    .with_context(|| format!("无效的 {} 选项 {key}", field.name))?;
                if field.kind == "token_value" {
                    u16::try_from(option)?.to_le_bytes().to_vec()
                } else {
                    typed(0x0c, &option.to_le_bytes())
                }
            }
            kind => value_bytes(kind, value).with_context(|| format!("字段 {}", field.name))?,
        };
        if field.default.as_ref() == Some(value) {
            continue;
        }
        if field.kind == "blob" {
            push_word(&mut data, field.token);
            data.extend(encoded);
        } else {
            property(&mut data, field.token, encoded);
        }
        ensure!(data.len() <= LIMIT, "游戏命令超过一帧大小");
    }
    let command = Writer::command(spec.token, identity, sender, receiver, attribs, data.into());
    ensure!(command.len() <= LIMIT, "游戏命令超过一帧大小");
    Ok(command)
}

fn value_bytes(kind: &str, value: &Value) -> Result<Vec<u8>> {
    match kind {
        "i32" => Ok(typed(
            0x0c,
            &i32::try_from(value.as_i64().context("需要有符号整数")?)?.to_le_bytes(),
        )),
        "u32" => Ok(typed(0x14, &unsigned(value)?.to_le_bytes())),
        "bool" => Ok(typed(
            0x0e,
            &[u8::from(value.as_bool().context("需要布尔值")?)],
        )),
        "text" => text(value.as_str().context("需要字符串")?),
        "fixed" => {
            let scaled = (value.as_f64().context("需要有效数值")? * 100_000.0).round_ties_even();
            ensure!(
                scaled >= i64::MIN as f64 && scaled < -(i64::MIN as f64),
                "定点数超出范围"
            );
            Ok(typed(0x167, &(scaled as i64).to_le_bytes()))
        }
        "blob" => blob(value),
        "u32_list" | "i32_list" | "string_list" | "u8_list" => array(kind, value),
        "activity_intent" => intent(value),
        "rally_points" => rally_points(value),
        "scope_value" => scope(value),
        "boolean_event_target_parameters" => decision_parameters(value),
        _ => bail!("未知编码类型 {kind}"),
    }
}

fn unsigned(value: &Value) -> Result<u32> {
    Ok(u32::try_from(value.as_u64().context("需要非负整数")?)?)
}

fn text(value: &str) -> Result<Vec<u8>> {
    let length = u16::try_from(value.len()).context("字符串过长")?;
    let mut output = typed(0x0f, &length.to_le_bytes());
    output.extend(value.as_bytes());
    Ok(output)
}

fn blob(value: &Value) -> Result<Vec<u8>> {
    let object = fields(value, &["hex"])?;
    let hex = object
        .get("hex")
        .and_then(Value::as_str)
        .context("二进制字段需要 hex 字符串")?;
    let digits: Vec<_> = hex
        .bytes()
        .filter(|byte| !byte.is_ascii_whitespace())
        .collect();
    let (pairs, remainder) = digits.as_chunks::<2>();
    ensure!(
        remainder.is_empty() && pairs.len() <= LIMIT,
        "二进制字段长度无效"
    );
    let mut payload = typed(0x1fc, &(pairs.len() as u32).to_le_bytes());
    for pair in pairs {
        payload.push(u8::from_str_radix(str::from_utf8(pair)?, 16)?);
    }
    Ok(payload)
}

fn array(kind: &str, value: &Value) -> Result<Vec<u8>> {
    let entries = value.as_array().context("需要数组")?;
    ensure!(entries.len() <= LIMIT / 4, "数组超过一帧大小");
    let scalar = match kind {
        "i32_list" => "i32",
        "string_list" => "text",
        _ => "u32",
    };
    let mut data = Vec::new();
    for entry in entries {
        if kind == "u8_list" {
            ensure!(unsigned(entry)? <= 255, "数组元素超出字节范围");
        }
        data.extend(value_bytes(scalar, entry)?);
        ensure!(data.len() <= LIMIT, "数组超过一帧大小");
    }
    Ok(group(data))
}

fn intent(value: &Value) -> Result<Vec<u8>> {
    let fields = fields(value, &["type", "target"])?;
    let mut data = Vec::new();
    if let Some(kind) = fields.get("type").filter(|kind| !kind.is_null()) {
        let kind = kind
            .as_str()
            .filter(|value| !value.is_empty())
            .context("活动意图需要类型")?;
        property(&mut data, 0xe1, text(kind)?);
    }
    if let Some(target) = fields.get("target")
        && unsigned(target)? != u32::MAX
    {
        property(&mut data, 0x6b, value_bytes("u32", target)?);
    }
    Ok(group(data))
}

fn rally_points(value: &Value) -> Result<Vec<u8>> {
    let points = value.as_array().context("集结点需要数组")?;
    ensure!(points.len() <= LIMIT / 24, "集结点数量过多");
    let mut data = Vec::new();
    for point in points {
        let point = fields(point, &["province", "color"])?;
        let mut row = Vec::new();
        property(
            &mut row,
            0x27d2,
            value_bytes("i32", point.get("province").context("缺少集结省份")?)?,
        );
        property(
            &mut row,
            0x56,
            value_bytes("i32", point.get("color").context("缺少集结点颜色")?)?,
        );
        data.extend(group(row));
    }
    Ok(group(data))
}

fn scope(value: &Value) -> Result<Vec<u8>> {
    let scope = fields(value, &["type", "identity"])?;
    if scope.is_empty() {
        return Ok(group(Vec::new()));
    }
    let kind = scope
        .get("type")
        .and_then(Value::as_str)
        .context("缺少范围类型")?;
    let token: u16 = match kind {
        "char" => 0x2ea6,
        "boolean" => 0x500,
        "prov" | "province" => 0x4ff,
        "lt" | "landed_title" => 0x2ea7,
        "artifact" => 0x3594,
        "war" => 0x2b3f,
        "court_position" => 0x3688,
        "court_position_type" => 0x3c0c,
        "council_task" => 0x315f,
        _ => bail!("未知范围类型 {kind}"),
    };
    let id = scope
        .get("identity")
        .map(unsigned)
        .transpose()?
        .unwrap_or(0);
    ensure!(kind != "boolean" || id <= 1, "布尔范围编号超出范围");
    let mut data = Vec::new();
    property(&mut data, 0xe1, token.to_le_bytes().to_vec());
    if id != 0 {
        property(&mut data, 0xdb, typed(0x29c, &u64::from(id).to_le_bytes()));
    }
    Ok(group(data))
}

fn decision_parameters(value: &Value) -> Result<Vec<u8>> {
    let options = fields(value, &["event_targets"])?;
    let mut data = Vec::new();
    if let Some(targets) = options.get("event_targets") {
        let targets = targets.as_object().context("决议事件目标需要对象")?;
        let mut entries = Vec::new();
        for (name, target) in targets {
            ensure!(target["type"] == "boolean", "决议事件目标需要布尔范围");
            entries.extend(text(name)?);
            push_word(&mut entries, 1);
            entries.extend(scope(target)?);
            ensure!(entries.len() <= LIMIT, "决议事件目标超过一帧大小");
        }
        if !entries.is_empty() {
            property(&mut data, 0x6f8, group(entries));
        }
    }
    Ok(group(data))
}

fn fields<'a>(value: &'a Value, keys: &[&str]) -> Result<&'a Map<String, Value>> {
    let fields = value.as_object().context("需要 JSON 对象")?;
    ensure!(
        fields.keys().all(|key| keys.contains(&key.as_str())),
        "对象包含未知字段"
    );
    Ok(fields)
}

fn typed(token: u16, value: &[u8]) -> Vec<u8> {
    let mut output = token.to_le_bytes().to_vec();
    output.extend(value);
    output
}

fn group(body: Vec<u8>) -> Vec<u8> {
    let mut output = 3_u16.to_le_bytes().to_vec();
    output.extend(body);
    push_word(&mut output, 4);
    output
}

fn push_word(output: &mut Vec<u8>, word: u16) {
    output.extend(word.to_le_bytes());
}

fn property(output: &mut Vec<u8>, key: u16, value: Vec<u8>) {
    push_word(output, key);
    push_word(output, 1);
    output.extend(value);
}
