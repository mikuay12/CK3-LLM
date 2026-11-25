use std::env;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct ConnectionSettings {
    pub game_dir: String,
    pub profile_path: String,
    pub username: String,
    pub identity: String,
}

impl Default for ConnectionSettings {
    fn default() -> Self {
        Self {
            game_dir: r"C:\Games\Crusader Kings III".into(),
            profile_path: String::new(),
            username: "AIObserver".into(),
            identity: "AIObserver".into(),
        }
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct ModelSettings {
    pub endpoint: String,
    pub model: String,
    pub api_key_env: Option<String>,
    pub instructions: String,
    pub request_timeout: f64,
}

impl Default for ModelSettings {
    fn default() -> Self {
        Self {
            endpoint: "https://api.openai.com/v1/chat/completions".into(),
            model: "gpt-4o-mini".into(),
            api_key_env: Some("CK3_MODEL_API_KEY".into()),
            instructions: String::new(),
            request_timeout: 45.0,
        }
    }
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default)]
pub struct Settings {
    pub connection: ConnectionSettings,
    pub model: ModelSettings,
}

impl Settings {
    pub fn path() -> Result<PathBuf> {
        let appdata = env::var_os("APPDATA").context("未找到 Windows 的 APPDATA 目录")?;
        Ok(PathBuf::from(appdata).join("CK3-LLM").join("settings.json"))
    }

    pub fn load(path: &Path) -> Result<Self> {
        let file = match fs::read_to_string(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(error) => {
                return Err(error).with_context(|| format!("读取配置 {}", path.display()));
            }
        };
        parse_json(&file).with_context(|| format!("解析配置 {}", path.display()))
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        let parent = path.parent().context("配置文件需要父目录")?;
        fs::create_dir_all(parent)?;
        let mut file = tempfile::NamedTempFile::new_in(parent)?;
        serde_json::to_writer_pretty(&mut file, self)?;
        file.write_all(b"\n")?;
        File::sync_all(file.as_file())?;
        file.persist(path)
            .with_context(|| format!("保存配置 {}", path.display()))?;
        Ok(())
    }
}

pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let file = fs::read_to_string(path).with_context(|| format!("读取 {}", path.display()))?;
    parse_json(&file).with_context(|| format!("解析 {}", path.display()))
}

fn parse_json<T: DeserializeOwned>(text: &str) -> Result<T> {
    Ok(serde_json::from_str(
        text.strip_prefix('\u{feff}').unwrap_or(text),
    )?)
}
