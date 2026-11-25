use std::env;
use std::io::Read;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use reqwest::{Url, blocking::Client};
use serde::{Deserialize, Serialize};

use crate::settings::ModelSettings;

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
    )
}

pub fn chat(settings: &ModelSettings, key: &str, messages: &[Message]) -> Result<Reply> {
    let mut context = Vec::with_capacity(messages.len() + 1);
    if !settings.instructions.trim().is_empty() {
        context.push(Message {
            role: Role::System,
            content: settings.instructions.clone(),
        });
    }
    context.extend_from_slice(messages);
    complete(settings, key, &context, None)
}

fn complete(
    settings: &ModelSettings,
    key: &str,
    messages: &[Message],
    max_timeout: Option<Duration>,
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
    let mut request = client.post(endpoint).json(&serde_json::json!({
        "model": model,
        "messages": messages,
        "stream": false,
    }));
    if !key.trim().is_empty() {
        request = request.bearer_auth(key.trim());
    }
    let started = Instant::now();
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
    let content = parsed
        .choices
        .into_iter()
        .next()
        .and_then(|choice| choice.message.content)
        .filter(|content| !content.trim().is_empty())
        .context("模型回复中没有文本")?;
    Ok(Reply {
        content,
        status: status.as_u16(),
        elapsed: started.elapsed(),
    })
}
