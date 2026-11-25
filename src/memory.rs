use std::collections::HashSet;
use std::fs::{self, File};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::settings::{Settings, read_json, write_json};

#[derive(Clone, Copy, Debug, Default, Eq, Ord, PartialEq, PartialOrd, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    #[default]
    Campaign,
    Experience,
}

impl Scope {
    pub const ALL: [Self; 2] = [Self::Campaign, Self::Experience];

    pub fn label(self) -> &'static str {
        match self {
            Self::Campaign => "战役记忆",
            Self::Experience => "跨局经验",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, Ord, PartialEq, PartialOrd, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Category {
    Pact,
    Strategy,
    Diplomacy,
    Lesson,
    #[default]
    General,
}

impl Category {
    pub const ALL: [Self; 5] = [
        Self::Pact,
        Self::Strategy,
        Self::Diplomacy,
        Self::Lesson,
        Self::General,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Pact => "房主约定",
            Self::Strategy => "战略计划",
            Self::Diplomacy => "外交情报",
            Self::Lesson => "游玩经验",
            Self::General => "通用备忘",
        }
    }
}

#[derive(Clone, Deserialize, Serialize)]
pub struct Entry {
    pub id: String,
    pub scope: Scope,
    pub category: Category,
    pub content: String,
    #[serde(default = "enabled")]
    pub enabled: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

fn enabled() -> bool {
    true
}

#[derive(Deserialize, Serialize)]
struct Book {
    version: u32,
    items: Vec<Entry>,
}

pub struct Store {
    path: PathBuf,
    last_read: Option<Vec<u8>>,
    pub items: Vec<Entry>,
}

impl Store {
    pub fn open() -> Result<Self> {
        let settings = Settings::path()?;
        Self::load(settings.with_file_name("llm_memory.json"))
    }

    pub fn load(path: PathBuf) -> Result<Self> {
        let last_read = read_optional(&path)?;
        let items = match &last_read {
            Some(bytes) => {
                let book: Book = serde_json::from_slice(
                    bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(bytes),
                )
                .context("读取记忆文件")?;
                verify_book(book)?
            }
            None => Vec::new(),
        };
        Ok(Self {
            path,
            last_read,
            items,
        })
    }

    pub fn add(&mut self, scope: Scope, category: Category, content: &str) -> Result<()> {
        let content = normalized(content)?;
        if let Some(entry) = self
            .items
            .iter_mut()
            .find(|entry| entry.scope == scope && entry.content == content)
        {
            entry.category = category;
            entry.enabled = true;
            return Ok(());
        }
        let mut extra = Map::new();
        extra.insert("source".into(), Value::String("player".into()));
        self.items.push(Entry {
            id: Uuid::new_v4().simple().to_string(),
            scope,
            category,
            content,
            enabled: true,
            extra,
        });
        Ok(())
    }

    pub fn import(&mut self, path: &Path) -> Result<usize> {
        let incoming = verify_book(read_json(path)?)?;
        let mut ids: HashSet<_> = self.items.iter().map(|entry| entry.id.clone()).collect();
        let before = self.items.len();
        self.items.extend(
            incoming
                .into_iter()
                .filter(|entry| ids.insert(entry.id.clone())),
        );
        Ok(self.items.len() - before)
    }

    pub fn save(&mut self) -> Result<()> {
        let parent = self.path.parent().context("记忆文件需要父目录")?;
        fs::create_dir_all(parent)?;
        let lock = File::options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.path.with_extension("lock"))?;
        lock.lock()?;
        ensure!(
            read_optional(&self.path)? == self.last_read,
            "记忆文件已从外部更新，请重新读取后再保存"
        );
        let mut items = self.items.clone();
        for entry in &mut items {
            entry.content = normalized(&entry.content)?;
        }
        write_json(
            &self.path,
            &Book {
                version: 1,
                items: items.clone(),
            },
        )?;
        self.last_read = read_optional(&self.path)?;
        self.items = items;
        Ok(())
    }

    pub fn prompt(&self, max_chars: usize) -> String {
        let mut entries: Vec<_> = self
            .items
            .iter()
            .filter(|entry| entry.enabled && !entry.content.trim().is_empty())
            .collect();
        entries.sort_by_key(|entry| (entry.scope, entry.category));
        let text = entries
            .into_iter()
            .map(|entry| {
                format!(
                    "- {} / {}　{}\n",
                    entry.scope.label(),
                    entry.category.label(),
                    entry.content.trim()
                )
            })
            .collect::<String>();
        text.chars().take(max_chars).collect()
    }
}

fn verify_book(book: Book) -> Result<Vec<Entry>> {
    ensure!(book.version == 1, "记忆文件版本不受支持");
    let mut ids = HashSet::new();
    for entry in &book.items {
        ensure!(
            !entry.id.is_empty() && ids.insert(&entry.id),
            "记忆文件包含无效或重复 ID"
        );
        ensure!(!entry.content.trim().is_empty(), "记忆内容为空");
    }
    Ok(book.items)
}

fn normalized(content: &str) -> Result<String> {
    let text = content.split_whitespace().collect::<Vec<_>>().join(" ");
    ensure!(!text.is_empty(), "请填写记忆内容");
    if text.chars().count() > 200 {
        return Ok(text.chars().take(199).chain(['…']).collect());
    }
    Ok(text)
}

fn read_optional(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("读取 {}", path.display())),
    }
}
