mod actions;
pub mod player;
mod transport;

use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::settings::ModelSettings;
use crate::world::{EntityList, Navigation, Query, QueryKind, QuerySource};

use transport::Transport;

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    System,
    User,
    Assistant,
}

#[derive(Clone, Serialize)]
pub struct Message {
    pub role: Role,
    pub content: String,
}

pub struct Reply {
    pub content: String,
    pub status: u16,
    pub elapsed: Duration,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<Choice>,
}

#[derive(Deserialize)]
struct Choice {
    message: AssistantMessage,
}

#[derive(Deserialize)]
struct AssistantMessage {
    content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<ToolCall>,
}

#[derive(Deserialize, Serialize)]
struct ToolCall {
    id: String,
    #[serde(rename = "type")]
    kind: String,
    function: FunctionCall,
}

#[derive(Deserialize, Serialize)]
struct FunctionCall {
    name: String,
    arguments: String,
}

pub fn test_connection(settings: &ModelSettings, key: &str) -> Result<Reply> {
    complete(
        settings,
        key,
        &[
            Message {
                role: Role::System,
                content: "Reply with OK only.".into(),
            },
            Message {
                role: Role::User,
                content: "ping".into(),
            },
        ],
        Some(Duration::from_secs(20)),
        None,
    )
}

pub fn chat(settings: &ModelSettings, key: &str, messages: &[Message]) -> Result<Reply> {
    chat_in_workspace(settings, key, messages, None)
}

pub fn chat_in_workspace(
    settings: &ModelSettings,
    key: &str,
    messages: &[Message],
    world: Option<&dyn QuerySource>,
) -> Result<Reply> {
    let mut context = Vec::with_capacity(messages.len() + 1);
    if !settings.instructions.trim().is_empty() {
        context.push(Message {
            role: Role::System,
            content: settings.instructions.clone(),
        });
    }
    context.extend_from_slice(messages);
    complete(settings, key, &context, None, world)
}

fn complete(
    settings: &ModelSettings,
    key: &str,
    messages: &[Message],
    max_timeout: Option<Duration>,
    world: Option<&dyn QuerySource>,
) -> Result<Reply> {
    let client = Transport::new(settings, key, max_timeout)?;
    let mut body = client.body(json!(messages));
    if let Some(world) = world {
        body["tools"] = json!([query_schema(world.kinds())]);
    }
    let started = Instant::now();
    for round in 0..=6 {
        let remaining = client
            .timeout
            .checked_sub(started.elapsed())
            .filter(|value| !value.is_zero())
            .context("模型请求超时")?;
        let (message, status) = client.request(&body, remaining)?;
        if message.tool_calls.is_empty() {
            let content = message
                .content
                .filter(|content| !content.trim().is_empty())
                .context("模型回复中没有文本")?;
            return Ok(Reply {
                content,
                status,
                elapsed: started.elapsed(),
            });
        }
        let world = world.context("当前对话未启用世界查询")?;
        ensure!(
            round < 6 && message.tool_calls.len() <= 8,
            "模型查询次数达到本轮上限"
        );
        let messages = body["messages"]
            .as_array_mut()
            .expect("serialized message array");
        messages.push(assistant_message(&message));
        for tool in &message.tool_calls {
            let result =
                query(world, tool).unwrap_or_else(|error| json!({"error": format!("{error:#}")}));
            messages.push(tool_message(
                tool,
                result_text(&result, settings.max_query_chars),
            ));
        }
    }
    unreachable!("the last request cannot start another tool round")
}

fn assistant_message(message: &AssistantMessage) -> Value {
    if message.tool_calls.is_empty() {
        json!({"role": "assistant", "content": message.content})
    } else {
        json!({"role": "assistant", "content": message.content, "tool_calls": message.tool_calls})
    }
}

fn result_text(value: &Value, limit: usize) -> String {
    let text = value.to_string();
    let Some((end, _)) = text.char_indices().nth(limit) else {
        return text;
    };
    let excerpt = &text[..end];
    let full = json!({"truncated": true, "excerpt": excerpt}).to_string();
    if full.chars().count() <= limit {
        full
    } else {
        let tail = "…";
        excerpt
            .chars()
            .take(limit.saturating_sub(1))
            .chain(tail.chars())
            .collect()
    }
}

fn tool_message(tool: &ToolCall, text: String) -> Value {
    json!({"role": "tool", "tool_call_id": tool.id, "content": text})
}

fn query_schema(kinds: &[QueryKind]) -> Value {
    json!({"type":"function", "function": {
        "name": "game_query", "description": "Query the selected CK3 world. Use summary for the game date and players. Use an object's numeric ID, or a landed title script key.",
        "parameters": {"type":"object", "properties": {"kind":{"type":"string", "enum":kinds}, "id":{"type":["string", "integer"]},
            "search":{"type":"string"}, "group":{"type":"string"}, "offset":{"type":"integer", "minimum":0},
            "limit":{"type":"integer", "minimum":1, "maximum":100}, "character":{"type":"integer", "minimum":0}}, "required":["kind"], "additionalProperties":false},
    }})
}

fn query(world: &dyn QuerySource, call: &ToolCall) -> Result<Value> {
    ensure!(
        call.kind == "function" && call.function.name == "game_query",
        "不支持的模型工具"
    );
    let arguments: Value =
        serde_json::from_str(&call.function.arguments).context("查询参数无法解析")?;
    point_query(world, &arguments)
}

fn point_query(world: &dyn QuerySource, arguments: &Value) -> Result<Value> {
    let name = arguments["kind"].as_str().context("查询类别无效")?;
    if name == "building_types" || name == "building_type" && arguments.get("id").is_none() {
        return world.query(Query::BuildingTypes {
            search: arguments["search"].as_str().unwrap_or_default(),
            offset: optional(arguments, "offset")?.unwrap_or(0),
            limit: optional(arguments, "limit")?.unwrap_or(20),
        });
    }
    if let Some(kind) = QueryKind::script_list(name) {
        return script_list(world, kind, arguments);
    }
    if let Some(kind) = QueryKind::collection_alias(name) {
        return entity_list(world, kind, arguments);
    }
    let kind: QueryKind = serde_json::from_value(name.into()).context("查询类别无效")?;
    if kind == QueryKind::Summary {
        return world.query(kind.request("")?);
    }
    if matches!(kind, QueryKind::Relations | QueryKind::Opinions) {
        return world.query(Query::People {
            kind,
            id: serde_json::from_value(arguments["id"].clone()).context("需要角色编号")?,
            other: optional(
                arguments,
                if kind == QueryKind::Relations {
                    "other"
                } else {
                    "owner"
                },
            )?,
            offset: optional(arguments, "offset")?.unwrap_or(0),
            limit: optional(arguments, "limit")?.unwrap_or(20),
        });
    }
    if kind == QueryKind::ConstructionQuote && arguments.get("building").is_some() {
        let key = reference_key(&arguments["building"]).context("需要建筑键或编号")?;
        return world.query(Query::ConstructionQuote {
            province: serde_json::from_value(arguments["id"].clone()).context("需要省份编号")?,
            building: &key,
            character: optional(arguments, "character")?,
            slot: optional(arguments, "index")?,
            treasury: arguments["use_treasury"] == true,
        });
    }
    if kind == QueryKind::DeclarationPreview && arguments.get("target").is_some() {
        let titles: Vec<u32> = optional(arguments, "titles")?.unwrap_or_default();
        return world.query(Query::Declaration {
            actor: optional(arguments, "character")?,
            target: serde_json::from_value(arguments["target"].clone())
                .context("需要目标角色编号")?,
            casus_belli: arguments["casus_belli"]
                .as_str()
                .context("需要宣战理由脚本键")?,
            titles: &titles,
            claimant: optional(arguments, "claimant")?.unwrap_or(u32::MAX),
        });
    }
    if kind == QueryKind::Route && arguments.get("origin").is_some() {
        return world.query(Query::Navigation(Navigation::Route {
            origin: serde_json::from_value(arguments["origin"].clone())
                .context("需要起点省份编号")?,
            target: serde_json::from_value(arguments["target"].clone())
                .context("需要目标省份编号")?,
            speed: arguments["speed"].as_f64(),
            unit: optional(arguments, "unit_id")?,
            include_straits: arguments["include_straits"].as_bool().unwrap_or(true),
            max_nodes: optional(arguments, "max_nodes")?.unwrap_or(20_000),
        }));
    }
    if kind == QueryKind::WarTheater {
        return world.query(Query::Navigation(Navigation::Theater {
            war: serde_json::from_value(arguments["id"].clone()).context("需要战争编号")?,
            character: optional(arguments, "character")?,
            max_armies: optional(arguments, "max_armies")?.unwrap_or(8),
            max_targets: optional(arguments, "max_targets")?.unwrap_or(8),
            max_nodes: optional(arguments, "max_nodes")?.unwrap_or(4000),
        }));
    }
    if matches!(kind, QueryKind::Borders | QueryKind::Neighbors) {
        let id = reference_key(&arguments["id"]).context("需要领主编号或头衔脚本键")?;
        let other = reference_key(&arguments["other"]);
        return world.query(Query::Navigation(Navigation::Borders {
            realm: &id,
            other: other.as_deref(),
            neighbors: kind == QueryKind::Neighbors,
            include_straits: arguments["include_straits"].as_bool().unwrap_or(false),
            offset: optional(arguments, "offset")?.unwrap_or(0),
            limit: optional(arguments, "limit")?.unwrap_or(20),
        }));
    }
    if kind.is_collection()
        && (!kind.is_entity() && kind != QueryKind::Activity || arguments.get("id").is_none())
    {
        return entity_list(world, kind, arguments);
    }
    if kind == QueryKind::ObservedCommands {
        return world.query(Query::ObservedCommands {
            name: arguments["id"].as_str().unwrap_or_default(),
            offset: arguments
                .get("offset")
                .map(|value| serde_json::from_value(value.clone()))
                .transpose()?
                .unwrap_or(0),
            limit: arguments
                .get("limit")
                .map(|value| serde_json::from_value(value.clone()))
                .transpose()?
                .unwrap_or(20),
        });
    }
    if kind.is_script() {
        let Some(id) = arguments.get("id") else {
            return script_list(world, kind, arguments);
        };
        let id = id.as_str().context("脚本定义查询需要脚本键")?;
        let character = arguments
            .get("character")
            .map(|value| serde_json::from_value(value.clone()))
            .transpose()?;
        return world.query(Query::Script {
            kind,
            id,
            character,
        });
    }
    let id = reference_key(&arguments["id"]).context("查询需要有效 ID")?;
    let character = arguments
        .get("character")
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?;
    world.query(kind.request_for(&id, character)?)
}

fn reference_key(value: &Value) -> Option<String> {
    value
        .as_str()
        .map(str::to_owned)
        .or_else(|| value.as_u64().map(|id| id.to_string()))
}

fn optional<T: serde::de::DeserializeOwned>(request: &Value, key: &str) -> Result<Option<T>> {
    request
        .get(key)
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()
        .with_context(|| format!("查询参数 {key} 无效"))
}

fn entity_list(world: &dyn QuerySource, kind: QueryKind, request: &Value) -> Result<Value> {
    let machine = request["machineid"].as_i64().map(|id| id.to_string());
    let search = request["search"]
        .as_str()
        .or(machine.as_deref())
        .unwrap_or_default();
    let character = request
        .get("character")
        .or_else(|| request.get("holder"))
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?;
    let offset = request
        .get("offset")
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?
        .unwrap_or(0);
    let limit = request
        .get("limit")
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?
        .unwrap_or(20);
    world.query(Query::Entities(EntityList {
        kind,
        character,
        search,
        offset,
        limit,
        include_inactive: request["include_invalidated"].as_bool().unwrap_or(false)
            || request["completed"] == true,
    }))
}

fn script_list(world: &dyn QuerySource, kind: QueryKind, arguments: &Value) -> Result<Value> {
    let offset = arguments
        .get("offset")
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?
        .unwrap_or(0);
    let limit = arguments
        .get("limit")
        .map(|value| serde_json::from_value(value.clone()))
        .transpose()?
        .unwrap_or(20);
    world.query(Query::ScriptList {
        kind,
        search: arguments["search"].as_str().unwrap_or_default(),
        group: arguments["group"].as_str(),
        offset,
        limit,
    })
}
