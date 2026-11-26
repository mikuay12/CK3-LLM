use std::env;
use std::io::Read;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use reqwest::{Url, blocking::Client};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::settings::ModelSettings;
use crate::world::{QueryKind, QuerySource};

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
    let endpoint = Url::parse(settings.endpoint.trim()).context("模型 API 地址无效")?;
    ensure!(
        matches!(endpoint.scheme(), "http" | "https")
            && endpoint.has_host()
            && endpoint.username().is_empty()
            && endpoint.password().is_none(),
        "模型 API 地址必须是 HTTP(S) 地址"
    );
    let model = settings.model.trim();
    ensure!(!model.is_empty(), "请填写模型名称");
    let timeout = Duration::try_from_secs_f64(settings.request_timeout).context("请求超时无效")?;
    ensure!(!timeout.is_zero(), "请求超时必须大于零");
    let timeout = max_timeout.map_or(timeout, |limit| timeout.min(limit));
    let env_key = settings
        .api_key_env
        .as_deref()
        .and_then(|name| env::var(name.trim()).ok());
    let key = if key.trim().is_empty() {
        env_key.as_deref().unwrap_or_default()
    } else {
        key
    };

    let client = Client::builder()
        .timeout(timeout)
        .build()
        .context("创建模型 HTTP 客户端")?;
    let mut body = json!({
        "model": model,
        "messages": messages,
        "stream": false,
    });
    if world.is_some() {
        body["tools"] = json!([{
            "type": "function",
            "function": {
                "name": "game_query",
                "description": "Query the CK3 world source selected by the user. Use summary for live game date, tick and players, or for snapshot metadata. Query a character, landed title, or province by ID. A landed title also accepts its script key. Live results carry their game date and tick.",
                "parameters": { "type": "object", "properties": {
                    "kind": { "type": "string", "enum": ["summary", "character", "title", "province"] },
                    "id": { "type": ["string", "integer"] }
                }, "required": ["kind"], "additionalProperties": false }
            }
        }]);
    }
    let started = Instant::now();
    for round in 0..=6 {
        let remaining = timeout
            .checked_sub(started.elapsed())
            .filter(|value| !value.is_zero())
            .context("模型请求超时")?;
        let mut request = client.post(endpoint.clone()).timeout(remaining).json(&body);
        if !key.trim().is_empty() {
            request = request.bearer_auth(key.trim());
        }
        let (message, status) = request_message(request)?;
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
        messages.push(json!({"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}));
        for tool in &message.tool_calls {
            let result =
                query(world, tool).unwrap_or_else(|error| json!({"error": format!("{error:#}")}));
            let content = result.to_string();
            let excerpt: String = content.chars().take(6000).collect();
            let text = if excerpt.len() < content.len() {
                json!({"truncated": true, "excerpt": excerpt}).to_string()
            } else {
                content
            };
            messages.push(json!({"role": "tool", "tool_call_id": tool.id, "content": text}));
        }
    }
    unreachable!("the last request cannot start another tool round")
}

fn request_message(request: reqwest::blocking::RequestBuilder) -> Result<(AssistantMessage, u16)> {
    let response = request
        .send()
        .map_err(reqwest::Error::without_url)
        .context("模型请求失败")?;
    let status = response.status();
    ensure!(status.is_success(), "模型服务返回 HTTP {}", status.as_u16());
    const RESPONSE_LIMIT: u64 = 4 * 1024 * 1024;
    let mut body = Vec::new();
    response
        .take(RESPONSE_LIMIT + 1)
        .read_to_end(&mut body)
        .context("读取模型回复失败")?;
    ensure!(body.len() as u64 <= RESPONSE_LIMIT, "模型回复超过 4 MiB");
    let parsed: ChatResponse = serde_json::from_slice(&body).context("模型回复格式无效")?;
    let message = parsed
        .choices
        .into_iter()
        .next()
        .context("模型回复没有候选消息")?
        .message;
    Ok((message, status.as_u16()))
}

fn query(world: &dyn QuerySource, call: &ToolCall) -> Result<Value> {
    ensure!(
        call.kind == "function" && call.function.name == "game_query",
        "不支持的模型工具"
    );
    let arguments: Value =
        serde_json::from_str(&call.function.arguments).context("查询参数无法解析")?;
    let kind: QueryKind =
        serde_json::from_value(arguments["kind"].clone()).context("查询类别无效")?;
    if kind == QueryKind::Summary {
        return world.query(kind.request("")?);
    }
    let id = &arguments["id"];
    let id = id
        .as_str()
        .map(str::to_owned)
        .or_else(|| id.as_u64().map(|id| id.to_string()))
        .context("查询需要有效 ID")?;
    world.query(kind.request(&id)?)
}
