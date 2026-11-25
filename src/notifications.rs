use std::collections::{BTreeSet, VecDeque};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::settings::{Settings, read_json, write_json};

pub const HISTORY_LIMIT: usize = 500;

#[derive(Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    version: u32,
    pub disabled_groups: BTreeSet<String>,
    pub disabled_types: BTreeSet<String>,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            version: 1,
            disabled_groups: BTreeSet::new(),
            disabled_types: BTreeSet::new(),
        }
    }
}

impl Policy {
    pub fn path() -> Result<PathBuf> {
        Ok(Settings::path()?.with_file_name("notification_policy.json"))
    }

    pub fn load(path: &Path) -> Result<Self> {
        let policy: Self = read_json(path)?;
        ensure!(policy.version == 1, "通知设置版本不受支持");
        ensure!(
            policy
                .disabled_groups
                .iter()
                .chain(&policy.disabled_types)
                .all(|id| !id.is_empty()),
            "通知筛选项不能为空"
        );
        Ok(policy)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        write_json(path, self)
    }

    pub fn allows(&self, notice: &Notice) -> bool {
        !self.disabled_groups.contains(&notice.group) && !self.disabled_types.contains(&notice.kind)
    }
}

pub struct Notice {
    pub group: String,
    pub kind: String,
    pub text: String,
}

impl Notice {
    pub fn runtime(kind: &str, text: impl Into<String>) -> Self {
        Self {
            group: "app_session".into(),
            kind: kind.into(),
            text: text.into(),
        }
    }
}

pub fn load_history(path: &Path) -> Result<VecDeque<Notice>> {
    let file = File::open(path).with_context(|| format!("打开通知记录 {}", path.display()))?;
    let mut history = VecDeque::new();
    for line in BufReader::new(file).lines() {
        let line = line?;
        let Ok(row) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if row["event"] != "game_notification" {
            continue;
        }
        let kind = row["notification_id"]
            .as_str()
            .or(row["type"].as_str())
            .unwrap_or("notification");
        let group = row["group"].as_str().unwrap_or("game");
        let text = ["text", "title", "message", "description", "summary"]
            .into_iter()
            .filter_map(|field| row[field].as_str())
            .find(|text| !text.is_empty())
            .map_or_else(|| row.to_string(), str::to_owned);
        history.push_back(Notice {
            group: group.into(),
            kind: kind.into(),
            text,
        });
        if history.len() > HISTORY_LIMIT {
            history.pop_front();
        }
    }
    Ok(history)
}
