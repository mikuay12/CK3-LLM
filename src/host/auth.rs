use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use uuid::Uuid;

pub const SERVER_KEY: &str = "ck3-local";
pub const BASIC_AUTH: &str = "Basic Y2szLWxvY2FsOg==";

#[derive(Clone, Serialize)]
pub struct User {
    pub id: String,
    pub username: String,
}

#[derive(Deserialize, Serialize)]
struct Claims {
    uid: String,
    usn: String,
    iat: u64,
    exp: u64,
    refresh: bool,
}

#[derive(Serialize)]
pub struct Tokens {
    pub created: bool,
    pub token: String,
    pub refresh_token: String,
}

pub struct Accounts {
    secret: [u8; 32],
    users: Mutex<HashMap<String, User>>,
}

impl Accounts {
    pub fn new() -> Self {
        Self {
            secret: rand::random(),
            users: Mutex::default(),
        }
    }

    pub fn authenticate(&self, identity: &str, username: &str) -> Tokens {
        let id = Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("{SERVER_KEY}:{identity}").as_bytes(),
        )
        .to_string();
        let mut users = self.users.lock().unwrap();
        let created = !users.contains_key(&id);
        let user = users.entry(id.clone()).or_insert_with(|| User {
            id,
            username: username.to_owned(),
        });
        self.tokens(user, created)
    }

    pub fn tokens(&self, user: &User, created: bool) -> Tokens {
        Tokens {
            created,
            token: self.issue(user, false),
            refresh_token: self.issue(user, true),
        }
    }

    pub fn verify(&self, token: &str, refresh: bool) -> Option<User> {
        let (content, signature) = token.rsplit_once('.')?;
        let signature = URL_SAFE_NO_PAD.decode(signature).ok()?;
        let mut mac = self.mac();
        mac.update(content.as_bytes());
        mac.verify_slice(&signature).ok()?;
        let (_, claims) = content.split_once('.')?;
        let bytes = URL_SAFE_NO_PAD.decode(claims).ok()?;
        let claims: Claims = serde_json::from_slice(&bytes).ok()?;
        if claims.exp <= now() || claims.refresh != refresh {
            return None;
        }
        self.users.lock().unwrap().get(&claims.uid).cloned()
    }

    fn issue(&self, user: &User, refresh: bool) -> String {
        const HEADER: &str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9";
        let iat = now();
        let claims = Claims {
            uid: user.id.clone(),
            usn: user.username.clone(),
            iat,
            exp: iat + if refresh { 30 * 86400 } else { 86400 },
            refresh,
        };
        let content = format!(
            "{HEADER}.{}",
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims).unwrap())
        );
        let mut mac = self.mac();
        mac.update(content.as_bytes());
        format!(
            "{content}.{}",
            URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
        )
    }

    fn mac(&self) -> Hmac<Sha256> {
        Hmac::new_from_slice(&self.secret).expect("fixed HMAC key")
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
