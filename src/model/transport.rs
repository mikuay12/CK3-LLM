use std::env;
use std::io::Read;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use reqwest::{Url, blocking::Client};
use serde_json::{Map, Value, json};

use crate::settings::ModelSettings;

use super::{AssistantMessage, ChatResponse};

pub(super) struct Transport {
    client: Client,
    endpoint: Url,
    key: String,
    model: String,
    pub timeout: Duration,
    options: Map<String, Value>,
}

impl Transport {
    pub fn new(settings: &ModelSettings, key: &str, max_timeout: Option<Duration>) -> Result<Self> {
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
        let timeout =
            Duration::try_from_secs_f64(settings.request_timeout).context("请求超时无效")?;
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
        const RESERVED: [&str; 7] = [
            "model",
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream",
            "n",
        ];
        ensure!(
            settings
                .request_options
                .keys()
                .all(|key| !RESERVED.contains(&key.as_str())),
            "模型生成参数包含保留字段"
        );
        ensure!(settings.max_query_chars > 0, "单次查询字符预算需要大于零");
        let client = Client::builder()
            .timeout(timeout)
            .build()
            .context("创建模型 HTTP 客户端")?;
        Ok(Self {
            client,
            endpoint,
            timeout,
            key: key.trim().into(),
            model: model.into(),
            options: settings.request_options.clone(),
        })
    }

    pub fn body(&self, messages: Value) -> Value {
        let mut body = self.options.clone();
        body.insert("model".into(), json!(self.model));
        body.insert("messages".into(), messages);
        body.insert("stream".into(), json!(false));
        Value::Object(body)
    }

    pub fn request(&self, body: &Value, duration: Duration) -> Result<(AssistantMessage, u16)> {
        let mut request = self
            .client
            .post(self.endpoint.clone())
            .timeout(duration)
            .json(body);
        if !self.key.is_empty() {
            request = request.bearer_auth(&self.key);
        }
        let response = request
            .send()
            .map_err(reqwest::Error::without_url)
            .context("模型请求失败")?;
        let status = response.status().as_u16();
        if !response.status().is_success() {
            return Err(RequestFailure(status).into());
        }
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
        Ok((message, status))
    }
}

#[derive(Debug)]
struct RequestFailure(u16);

impl std::fmt::Display for RequestFailure {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(output, "模型服务返回 HTTP {}", self.0)
    }
}

impl std::error::Error for RequestFailure {}

pub(super) fn retryable(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<RequestFailure>()
        .is_some_and(|failure| matches!(failure.0, 408 | 409 | 429 | 500..=599))
        || error
            .downcast_ref::<reqwest::Error>()
            .is_some_and(|error| error.is_timeout() || error.is_connect() || error.is_body())
}
