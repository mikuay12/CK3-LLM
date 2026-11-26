use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Map, Value};

use super::game_protocol::Incoming;

const CAPACITY: usize = 2048;
const ECHO_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, PartialEq)]
pub struct ActionRequest {
    pub request_id: String,
    pub name: String,
    pub fields: Map<String, Value>,
    pub attribs: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionStatus {
    Prepared,
    Submitted,
    HostEchoed,
    DeliveryUnconfirmed,
    SendFailed,
    Disconnected,
}

impl ActionStatus {
    pub fn label(self) -> &'static str {
        match self {
            Self::Prepared => "已准备",
            Self::Submitted => "已提交",
            Self::HostEchoed => "收到房主回传",
            Self::DeliveryUnconfirmed => "等待房主回传超时",
            Self::SendFailed => "提交失败",
            Self::Disconnected => "会话已断开",
        }
    }

    fn finished(self) -> bool {
        matches!(
            self,
            Self::HostEchoed | Self::SendFailed | Self::Disconnected
        )
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ActionReceipt {
    pub request_id: String,
    pub action: String,
    pub identity: u32,
    pub status: ActionStatus,
    pub effect_verification: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub duplicate: bool,
}

struct Entry {
    request: ActionRequest,
    receipt: ActionReceipt,
    token: u16,
    sender: i32,
    expected: Value,
    sent: Option<Instant>,
}

#[derive(Default)]
pub(super) struct Journal {
    entries: HashMap<String, Entry>,
    order: VecDeque<String>,
    pending: HashMap<u32, String>,
}

impl Journal {
    pub fn replay(&self, request: &ActionRequest) -> Result<Option<ActionReceipt>> {
        self.duplicate(request, |previous| previous == request)
    }

    pub fn replay_interaction(&self, request: &ActionRequest) -> Result<Option<ActionReceipt>> {
        ensure!(
            request.name == "send_character_interaction"
                && request.fields.len() == 1
                && request.fields.contains_key("character_interaction"),
            "互动回执查询需要已验证的互动参数"
        );
        self.duplicate(request, |previous| {
            previous.name == request.name
                && previous.attribs == request.attribs
                && previous.fields["character_interaction"]
                    == request.fields["character_interaction"]
        })
    }

    fn duplicate(
        &self,
        request: &ActionRequest,
        matches: impl FnOnce(&ActionRequest) -> bool,
    ) -> Result<Option<ActionReceipt>> {
        ensure!(
            !request.request_id.trim().is_empty() && request.request_id.len() <= 256,
            "操作请求需要有效的 request_id"
        );
        let Some(entry) = self.entries.get(&request.request_id) else {
            return Ok(None);
        };
        ensure!(matches(&entry.request), "request_id 已用于不同的操作");
        let mut receipt = entry.receipt.clone();
        receipt.duplicate = true;
        Ok(Some(receipt))
    }

    pub fn prepare(&mut self, request: ActionRequest, packet: &Incoming<'_>) -> Result<()> {
        let identity = packet.identity()?;
        let sender = packet.sender()?;
        let expected = comparable(packet)?;
        ensure!(
            !self.pending.contains_key(&identity),
            "游戏命令编号仍有操作等待回传"
        );
        if self.entries.len() == CAPACITY {
            let index = self
                .order
                .iter()
                .position(|id| self.entries[id].receipt.status.finished())
                .context("当前会话中等待回传的游戏操作过多")?;
            let id = self.order.remove(index).expect("entry position");
            self.entries.remove(&id);
        }
        let id = request.request_id.clone();
        let receipt = ActionReceipt {
            request_id: id.clone(),
            action: request.name.clone(),
            identity,
            status: ActionStatus::Prepared,
            effect_verification: "model_state_query_required",
            error: None,
            duplicate: false,
        };
        self.pending.insert(identity, id.clone());
        self.order.push_back(id.clone());
        self.entries.insert(
            id,
            Entry {
                request,
                receipt,
                token: packet.kind,
                sender,
                expected,
                sent: None,
            },
        );
        Ok(())
    }

    pub fn sent(&mut self, identity: u32, result: Result<()>) -> ActionReceipt {
        let id = &self.pending[&identity];
        let entry = self.entries.get_mut(id).expect("pending entry");
        match result {
            Ok(()) => {
                entry.receipt.status = ActionStatus::Submitted;
                entry.sent = Some(Instant::now());
            }
            Err(error) => {
                entry.receipt.status = ActionStatus::SendFailed;
                entry.receipt.error = Some(format!("{error:#}"));
                self.pending.remove(&identity);
            }
        }
        entry.receipt.clone()
    }

    pub fn observe(&mut self, token: u16, bytes: &[u8]) -> Option<ActionReceipt> {
        if !self
            .pending
            .values()
            .any(|id| self.entries[id].token == token)
        {
            return None;
        }
        let packet = Incoming::decode(bytes).ok()?;
        let identity = packet.identity().ok()?;
        let id = self.pending.get(&identity)?;
        let entry = self.entries.get_mut(id)?;
        if entry.token != token
            || packet.sender().ok()? != entry.sender
            || comparable(&packet).ok()? != entry.expected
        {
            return None;
        }
        self.pending.remove(&identity);
        entry.receipt.status = ActionStatus::HostEchoed;
        Some(entry.receipt.clone())
    }

    pub fn status(&self, id: &str) -> Result<ActionReceipt> {
        Ok(self
            .entries
            .get(id)
            .context("当前会话中没有此操作请求")?
            .receipt
            .clone())
    }

    pub fn poll(&mut self) -> Vec<ActionReceipt> {
        self.entries
            .values_mut()
            .filter_map(|entry| {
                if entry.receipt.status == ActionStatus::Submitted
                    && entry
                        .sent
                        .is_some_and(|sent| sent.elapsed() >= ECHO_TIMEOUT)
                {
                    entry.receipt.status = ActionStatus::DeliveryUnconfirmed;
                    Some(entry.receipt.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn disconnect(&mut self) -> Vec<ActionReceipt> {
        self.pending
            .drain()
            .map(|(_, id)| {
                let entry = self.entries.get_mut(&id).expect("pending entry");
                entry.receipt.status = ActionStatus::Disconnected;
                entry.receipt.clone()
            })
            .collect()
    }
}

fn comparable(packet: &Incoming<'_>) -> Result<Value> {
    let mut body = packet.describe()?["fields"].take();
    if packet.name() == "start_activity_command"
        && let Some(plan) = body["data"].as_object_mut()
    {
        for key in ["tenets", "doctrines"] {
            if let Some(values) = plan.get_mut(key).and_then(Value::as_array_mut) {
                values.retain(|value| value != "");
                if values.is_empty() {
                    plan.remove(key);
                }
            }
        }
    }
    Ok(body)
}
