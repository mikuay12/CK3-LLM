use std::collections::HashMap;

use base64::{Engine, engine::general_purpose::STANDARD};
use prost::Message;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

// Field numbers follow Nakama C++ 2.4.1. The relay treats game payloads as bytes.
#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct Envelope {
    #[prost(string, tag = "1")]
    #[serde(skip_serializing_if = "String::is_empty")]
    pub cid: String,
    #[prost(message, optional, tag = "11")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<Error>,
    #[prost(message, optional, tag = "12")]
    #[serde(rename = "match", skip_serializing_if = "Option::is_none")]
    pub room: Option<Match>,
    #[prost(message, optional, tag = "14")]
    #[serde(alias = "matchData", skip_serializing_if = "Option::is_none")]
    pub match_data: Option<MatchData>,
    #[prost(message, optional, tag = "15")]
    #[serde(alias = "matchDataSend", skip_serializing_if = "Option::is_none")]
    pub match_data_send: Option<MatchDataSend>,
    #[prost(message, optional, tag = "16")]
    #[serde(alias = "matchJoin", skip_serializing_if = "Option::is_none")]
    pub match_join: Option<MatchJoin>,
    #[prost(message, optional, tag = "17")]
    #[serde(alias = "matchLeave", skip_serializing_if = "Option::is_none")]
    pub match_leave: Option<MatchLeave>,
    #[prost(message, optional, tag = "18")]
    #[serde(alias = "matchPresenceEvent", skip_serializing_if = "Option::is_none")]
    pub match_presence_event: Option<PresenceEvent>,
    #[prost(message, optional, tag = "24")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rpc: Option<Rpc>,
    #[prost(message, optional, tag = "32")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ping: Option<Empty>,
    #[prost(message, optional, tag = "33")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pong: Option<Empty>,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct Error {
    #[prost(int32, tag = "1")]
    pub code: i32,
    #[prost(string, tag = "2")]
    pub message: String,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct Match {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
    #[prost(bool, tag = "2")]
    pub authoritative: bool,
    #[prost(int32, tag = "4")]
    pub size: i32,
    #[prost(message, repeated, tag = "5")]
    pub presences: Vec<Presence>,
    #[prost(message, optional, tag = "6")]
    #[serde(rename = "self", skip_serializing_if = "Option::is_none")]
    pub own: Option<Presence>,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct Presence {
    #[prost(string, tag = "1")]
    #[serde(alias = "userId")]
    pub user_id: String,
    #[prost(string, tag = "2")]
    #[serde(alias = "sessionId")]
    pub session_id: String,
    #[prost(string, tag = "3")]
    pub username: String,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct MatchData {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
    #[prost(message, optional, tag = "2")]
    pub presence: Option<Presence>,
    #[prost(int64, tag = "3")]
    #[serde(alias = "opCode", with = "json_i64")]
    pub op_code: i64,
    #[prost(bytes = "bytes", tag = "4")]
    #[serde(with = "json_bytes")]
    pub data: prost::bytes::Bytes,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct MatchDataSend {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
    #[prost(int64, tag = "2")]
    #[serde(alias = "opCode", with = "json_i64")]
    pub op_code: i64,
    #[prost(bytes = "bytes", tag = "3")]
    #[serde(with = "json_bytes")]
    pub data: prost::bytes::Bytes,
    #[prost(message, repeated, tag = "4")]
    pub presences: Vec<Presence>,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct MatchJoin {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
    #[prost(map = "string, string", tag = "3")]
    pub metadata: HashMap<String, String>,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct MatchLeave {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct Rpc {
    #[prost(string, tag = "1")]
    pub id: String,
    #[prost(string, tag = "2")]
    pub payload: String,
    #[prost(string, tag = "3")]
    #[serde(alias = "httpKey", skip_serializing_if = "String::is_empty")]
    pub http_key: String,
}

#[derive(Clone, Message, Serialize, Deserialize)]
#[serde(default)]
pub struct PresenceEvent {
    #[prost(string, tag = "1")]
    #[serde(alias = "matchId")]
    pub match_id: String,
    #[prost(message, repeated, tag = "2")]
    pub joins: Vec<Presence>,
    #[prost(message, repeated, tag = "3")]
    pub leaves: Vec<Presence>,
}

#[derive(Clone, Message, Serialize, Deserialize)]
pub struct Empty {}

mod json_i64 {
    use super::*;

    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Number {
        String(String),
        Signed(i64),
    }

    pub fn serialize<S: Serializer>(value: &i64, output: S) -> Result<S::Ok, S::Error> {
        output.collect_str(value)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(input: D) -> Result<i64, D::Error> {
        match Number::deserialize(input)? {
            Number::String(value) => value.parse().map_err(serde::de::Error::custom),
            Number::Signed(value) => Ok(value),
        }
    }
}

mod json_bytes {
    use super::*;

    pub fn serialize<S: Serializer>(value: &[u8], output: S) -> Result<S::Ok, S::Error> {
        output.serialize_str(&STANDARD.encode(value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        input: D,
    ) -> Result<prost::bytes::Bytes, D::Error> {
        STANDARD
            .decode(String::deserialize(input)?)
            .map(Into::into)
            .map_err(serde::de::Error::custom)
    }
}
