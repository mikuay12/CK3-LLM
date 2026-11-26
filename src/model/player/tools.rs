use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::memory::{Category, Scope, Store};
use crate::notifications::watch::Sample;
use crate::settings::{ModalGate, ModelOutput, ModelSettings};
use crate::world::live::PlayerContext;
use crate::world::{MilitaryList, Query};

use super::super::{ToolCall, actions, point_query};
use super::{Environment, POLL, Runtime, Update};

pub(super) fn schemas() -> Value {
    json!([
        function(
            "game_query",
            "查询实时战役、操作参数或操作回执。request 使用 kind 和必要的 id。先用 schema 查看查询入口。",
            json!({
                "request":{"type":"object"}
            }),
            &["request"]
        ),
        function(
            "game_action",
            "提交从动作目录选出的一个 CK3 操作，沿用 request_id 可查询或重试同一请求。",
            json!({
                "action":{"type":"string"}, "parameters":{"type":"object"}, "request_id":{"type":"string"}
            }),
            &["action", "parameters", "request_id"]
        ),
        function(
            "wait_for_update",
            "按秒等待计时或当前角色的通知。",
            json!({
                "seconds":{"type":"number", "minimum":0}, "wake_on_notification":{"type":"boolean", "default":true}
            }),
            &["seconds"]
        ),
        function(
            "memory_manage",
            "维护持久记忆和跨局经验。",
            json!({
                "operation":{"type":"string", "enum":["add", "update", "delete", "list"]},
                "scope":{"type":"string", "enum":["campaign", "experience"]},
                "category":{"type":"string", "enum":["pact", "strategy", "diplomacy", "lesson", "general"]},
                "id":{"type":"string"}, "content":{"type":"string"}
            }),
            &["operation"]
        )
    ])
}

fn function(name: &str, description: &str, properties: Value, required: &[&str]) -> Value {
    json!({"type":"function", "function":{"name":name, "description":description,
        "parameters":{"type":"object", "properties":properties, "required":required, "additionalProperties":false}
    }})
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct QueryArguments {
    request: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ActionArguments {
    action: String,
    parameters: Map<String, Value>,
    request_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WaitArguments {
    seconds: f64,
    #[serde(default = "enabled")]
    wake_on_notification: bool,
}

fn enabled() -> bool {
    true
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MemoryArguments {
    operation: String,
    scope: Option<Scope>,
    category: Option<Category>,
    id: Option<String>,
    content: Option<String>,
}

pub(super) fn dispatch(
    runtime: &mut Runtime,
    game: &mut impl Environment,
    player: &PlayerContext,
    tool: &ToolCall,
) -> Result<Value> {
    ensure!(tool.kind == "function", "模型工具类型无效");
    match tool.function.name.as_str() {
        "game_query" => {
            let argument: QueryArguments = serde_json::from_str(&tool.function.arguments)?;
            query(runtime, game, player, &argument.request)
        }
        "game_action" => {
            let argument: ActionArguments = serde_json::from_str(&tool.function.arguments)?;
            if runtime.settings.modal_gate == ModalGate::Strict
                && !matches!(
                    argument.action.as_str(),
                    "select_event_option"
                        | "reply_character_interaction"
                        | "join_activity_command"
                        | "decline_activity_invite_command"
                )
            {
                check_pending(game, player)?;
            }
            let mut request = actions::prepare(
                &argument.action,
                argument.parameters,
                argument.request_id,
                player.character,
                game.machine(),
            )?;
            if request.name == "send_interaction" {
                let data = game.interaction(&request.fields, player.character)?;
                request.name = "send_character_interaction".into();
                request.fields = Map::from_iter([("character_interaction".into(), data)]);
                let response = game.replay_interaction(request.clone())?;
                if let Some(receipt) = receive(runtime, game, &response)? {
                    return Ok(serde_json::to_value(receipt)?);
                }
                let (accept, intermediary) =
                    game.acceptance(player, &request.fields["character_interaction"])?;
                request.fields.insert("ai_accept".into(), accept.into());
                request
                    .fields
                    .insert("ai_intermediary_accept".into(), intermediary.into());
            }
            ensure!(
                !runtime.stopped(game) && game.context()?.same_character(player),
                "控制角色已变化，请重新查询"
            );
            let reply = game.submit(request, runtime.stop.clone())?;
            let receipt = receive(runtime, game, &reply)?;
            Ok(serde_json::to_value(receipt)?)
        }
        "wait_for_update" => {
            let argument: WaitArguments = serde_json::from_str(&tool.function.arguments)?;
            let delay =
                Duration::try_from_secs_f64(argument.seconds).context("等待时间需要是非负数")?;
            if runtime.settings.modal_gate != ModalGate::Off {
                check_pending(game, player)?;
            }
            let reason = runtime.wait(game, delay, argument.wake_on_notification);
            Ok(json!({"reason":reason, "updates":runtime.take_updates()}))
        }
        "memory_manage" => {
            let argument: MemoryArguments = serde_json::from_str(&tool.function.arguments)?;
            let result = memory(argument)?;
            if result["operation"] != "list" {
                let _ = runtime.output.send(Update::MemoryChanged);
            }
            Ok(result)
        }
        name => bail!("未知的模型工具 {name}"),
    }
}

fn receive<T>(
    runtime: &Runtime,
    game: &impl Environment,
    response: &Receiver<Result<T>>,
) -> Result<T> {
    let start = Instant::now();
    let timeout = runtime.client.timeout;
    while start.elapsed() < timeout && !runtime.stopped(game) {
        match response.recv_timeout(POLL.min(timeout.saturating_sub(start.elapsed()))) {
            Ok(result) => return result,
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => bail!("操作回执通道已断开"),
        }
    }
    bail!("未收到操作回执，可按 request_id 查询提交状态")
}

fn check_pending(game: &impl Environment, player: &PlayerContext) -> Result<()> {
    let sample = checked_sample(game, player)?;
    ensure!(
        sample.pending.is_empty(),
        "请先处理角色的待选事件、人物请求或活动邀请"
    );
    Ok(())
}

fn checked_sample(game: &impl Environment, player: &PlayerContext) -> Result<Sample> {
    let sample = game.sample()?;
    ensure!(
        sample.character == player.character && sample.generation == player.generation,
        "控制角色已变化"
    );
    Ok(sample)
}

fn query(
    runtime: &mut Runtime,
    game: &mut impl Environment,
    player: &PlayerContext,
    request: &Value,
) -> Result<Value> {
    ensure!(request.is_object(), "查询需要 request 对象");
    if let Some(limit) = request.get("max_chars") {
        let limit = limit.as_u64().context("max_chars 需要正整数")?;
        ensure!(
            limit > 0 && limit <= runtime.settings.max_query_chars as u64,
            "max_chars 超过配置的单次查询预算"
        );
    }
    let command = request["command"].as_str().unwrap_or("query");
    let kind = match command {
        "query" => request["kind"].as_str().context("缺少查询类别")?,
        "observe" | "schema" | "notifications" | "observation_status" => command,
        _ => bail!("未知的观察请求 {command}"),
    };
    let result = match kind {
        "schema" => {
            let mut kinds = serde_json::to_value(game.world().kinds())?
                .as_array()
                .cloned()
                .expect("query kinds");
            kinds.extend(
                [
                    "observe",
                    "observation_status",
                    "actions",
                    "action",
                    "action_status",
                    "events",
                    "event_options",
                    "incoming_interactions",
                    "activity_invitations",
                    "notifications",
                    "wars",
                    "armies",
                    "combats",
                    "combat_results",
                    "sieges",
                ]
                .map(Value::from),
            );
            for name in [
                "interactions",
                "decisions",
                "activity_types",
                "council_task_types",
                "court_position_types",
                "court_position_tasks",
                "scheme_countermeasures",
                "laws",
                "men_at_arms_types",
                "notification_types",
                "event_definitions",
                "casus_belli_types",
                "building_types",
                "council_tasks",
                "factions",
                "schemes",
                "court_positions",
                "holy_orders",
                "secrets",
                "task_contracts",
                "contracts",
                "mercenary_companies",
                "activities",
            ] {
                kinds.push(name.into());
            }
            json!({"kinds":kinds, "session":player, "max_chars":runtime.settings.max_query_chars})
        }
        "observation_status" => serde_json::to_value(player)?,
        "actions" => actions::list(request)?,
        "action" => actions::definition(request["id"].as_str().context("操作查询需要名称")?)?,
        "action_status" => {
            let id = request["id"]
                .as_str()
                .context("操作状态查询需要 request_id")?;
            let response = game.status(id.into())?;
            serde_json::to_value(receive(runtime, game, &response)?)?
        }
        "observe" => {
            let sample = checked_sample(game, player)?;
            json!({"date":sample.date, "character":sample.state})
        }
        "events" => pending(game, player, "pending_event", "event")?,
        "incoming_interactions" => {
            pending(game, player, "pending_interaction", "incoming_interaction")?
        }
        "activity_invitations" => {
            pending(game, player, "activity_invitation", "activity_invitation")?
        }
        "event" | "event_options" | "incoming_interaction" | "activity_invitation" => {
            let id = request["id"]
                .as_u64()
                .and_then(|id| u32::try_from(id).ok())
                .context("待处理事务需要数字 ID")?;
            let point = if kind == "event_options" {
                "event"
            } else {
                kind
            };
            let result = point_query(
                game.world(),
                &json!({"kind":point, "id":id, "character":player.character}),
            )?;
            ensure!(game.context()?.same_character(player), "控制角色已变化");
            if kind == "event_options" {
                let mut options = result["data"]["options"]
                    .as_array()
                    .context("事件未返回选项")?
                    .clone();
                let count = options.len();
                let offset = request
                    .get("offset")
                    .map(|value| serde_json::from_value::<usize>(value.clone()))
                    .transpose()?
                    .unwrap_or(0);
                let limit = request
                    .get("limit")
                    .map(|value| serde_json::from_value::<usize>(value.clone()))
                    .transpose()?
                    .unwrap_or(20);
                ensure!((1..=64).contains(&limit), "事件选项单页条数须为 1 到 64");
                options = options.into_iter().skip(offset).take(limit).collect();
                for option in &mut options {
                    option["action"] = json!({"action":"select_event_option", "parameters":{"data":id, "option":option["index"]}});
                    if request.get("fields").is_none() {
                        option
                            .as_object_mut()
                            .expect("event option object")
                            .remove("script");
                    }
                }
                json!({"event_id":id, "count":count, "offset":offset, "items":options})
            } else {
                result
            }
        }
        "notifications" => {
            json!({"items":runtime.updates.iter().filter(|item| item["event"] == "game_notification").collect::<Vec<_>>()})
        }
        "wars" | "armies" | "combats" | "combat_results" | "sieges" => {
            let kind: MilitaryList = serde_json::from_value(kind.into())?;
            let character = request
                .get("character")
                .map(|value| serde_json::from_value::<u32>(value.clone()))
                .transpose()?
                .unwrap_or(player.character);
            let offset = request
                .get("offset")
                .map(|value| serde_json::from_value::<usize>(value.clone()))
                .transpose()?
                .unwrap_or(0);
            let limit = request
                .get("limit")
                .map(|value| serde_json::from_value::<usize>(value.clone()))
                .transpose()?
                .unwrap_or(20);
            game.world().query(Query::Military {
                kind,
                character,
                offset,
                limit,
            })?
        }
        "character" if request.get("id").is_none() => point_query(
            game.world(),
            &json!({"kind":"character", "id":player.character}),
        )?,
        "decision"
        | "activity_type"
        | "activity"
        | "war_theater"
        | "construction_quote"
        | "declaration_preview"
            if request.get("character").is_none() =>
        {
            let mut request = request.clone();
            request["character"] = player.character.into();
            point_query(game.world(), &request)?
        }
        "realm" | "neighbors" | "borders" | "relations" | "opinions"
            if request.get("id").is_none() =>
        {
            let mut request = request.clone();
            request["id"] = player.character.into();
            point_query(game.world(), &request)?
        }
        "council_tasks" | "factions" | "schemes" | "court_positions" | "secrets"
        | "task_contracts" | "alerts" | "player_messages" | "contracts"
            if request.get("character").is_none() && request["all"] != true =>
        {
            let mut request = request.clone();
            request["character"] = player.character.into();
            point_query(game.world(), &request)?
        }
        _ => point_query(game.world(), request)?,
    };
    let result = select_fields(result, request.get("fields"))?;
    if let Some(max) = request["max_chars"].as_u64() {
        let limit = max as usize;
        let encoded = result.to_string();
        if encoded.chars().count() > limit {
            return Ok(
                json!({"truncated":true, "excerpt":truncate(&encoded, limit.saturating_sub(40))}),
            );
        }
    }
    Ok(result)
}

fn pending(
    game: &impl Environment,
    player: &PlayerContext,
    group: &str,
    kind: &str,
) -> Result<Value> {
    let sample = checked_sample(game, player)?;
    let items = sample.pending.into_iter().filter(|item| item.kind == group);
    let encode = |item: crate::notifications::watch::Pending| {
        json!({
            "id":item.id, "character_id":sample.character, "kind":item.kind, "stage":item.stage, "summary":item.text,
            "details_query":{"kind":kind, "id":item.id},
        })
    };
    let items: Vec<_> = items.map(encode).collect();
    Ok(json!({"date":sample.date, "count":items.len(), "items":items}))
}

fn select_fields(result: Value, fields: Option<&Value>) -> Result<Value> {
    let Some(fields) = fields else {
        return Ok(result);
    };
    let fields = fields.as_array().context("fields 需要数组")?;
    let target = result
        .get("data")
        .or_else(|| result.get("character"))
        .unwrap_or(&result);
    let mut selected = Map::new();
    for field in fields {
        let field = field.as_str().context("字段路径需要字符串")?;
        let value = field.split('.').try_fold(target, |value, key| {
            if value.is_array() {
                value.get(key.parse::<usize>().ok()?)
            } else {
                value.get(key)
            }
        });
        selected.insert(field.into(), value.cloned().unwrap_or(Value::Null));
    }
    Ok(json!({"date":result["date"], "tick":result["tick"], "data":selected}))
}

fn memory(arguments: MemoryArguments) -> Result<Value> {
    let mut store = Store::open()?;
    let operation = arguments.operation.as_str();
    let output = match operation {
        "list" => {
            let items: Vec<_> = store
                .items
                .iter()
                .filter(|entry| {
                    arguments.scope.is_none_or(|scope| entry.scope == scope)
                        && arguments
                            .category
                            .is_none_or(|category| entry.category == category)
                })
                .collect();
            return Ok(json!({"operation":operation, "items":items}));
        }
        "add" => {
            let content = arguments.content.as_deref().context("添加记忆需要内容")?;
            let saved = store.remember(
                arguments.scope.unwrap_or_default(),
                arguments.category.unwrap_or_default(),
                content,
                "llm",
            )?;
            json!({"ok":true, "operation":operation, "item":saved})
        }
        "update" | "delete" => {
            let id = arguments.id.as_deref().context("操作需要记忆 ID")?;
            let index = store
                .items
                .iter()
                .position(|entry| entry.id == id)
                .context("找不到这条记忆")?;
            if operation == "delete" {
                store.items.remove(index);
                json!({"ok":true, "operation":operation, "id":id})
            } else {
                let entry = &mut store.items[index];
                if let Some(scope) = arguments.scope {
                    entry.scope = scope;
                }
                if let Some(category) = arguments.category {
                    entry.category = category;
                }
                if let Some(content) = arguments.content {
                    entry.content = content;
                }
                store.save()?;
                return Ok(json!({"ok":true, "operation":operation, "item":store.items[index]}));
            }
        }
        _ => bail!("未知的记忆操作 {operation}"),
    };
    store.save()?;
    Ok(output)
}

pub(super) fn truncate(text: &str, max: usize) -> String {
    if max == 0 {
        return String::new();
    }
    if text.char_indices().nth(max).is_some() {
        text.chars().take(max - 1).chain(['…']).collect()
    } else {
        text.into()
    }
}

pub(super) fn format_result(result: &Value, settings: &ModelSettings) -> String {
    match settings.response_format {
        ModelOutput::Json => super::super::result_text(result, settings.max_query_chars),
        ModelOutput::CompactText => {
            let text = match result.as_object() {
                Some(fields) => fields
                    .iter()
                    .map(|(key, value)| {
                        format!(
                            "{key} {}",
                            value
                                .as_str()
                                .map_or_else(|| value.to_string(), str::to_owned)
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n"),
                None => result.to_string(),
            };
            truncate(&text, settings.max_query_chars)
        }
    }
}
