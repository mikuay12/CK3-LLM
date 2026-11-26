use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value, json};

use super::{Query, QueryKind, QuerySource};
use crate::commands::interaction_spec;

mod map;
pub(crate) use map::Map as InstalledMap;

const KINDS: &[QueryKind] = &[
    QueryKind::Interaction,
    QueryKind::Decision,
    QueryKind::ActivityType,
    QueryKind::CouncilTaskType,
    QueryKind::CourtPositionType,
    QueryKind::CourtPositionTask,
    QueryKind::SchemeCountermeasure,
    QueryKind::Law,
    QueryKind::MenAtArmsType,
    QueryKind::NotificationType,
    QueryKind::EventDefinition,
    QueryKind::ProvinceMap,
    QueryKind::CasusBelli,
];

impl QueryKind {
    pub fn is_script(self) -> bool {
        self != Self::ProvinceMap && KINDS.contains(&self)
    }

    pub(crate) fn script_list(name: &str) -> Option<Self> {
        Some(match name {
            "interactions" => Self::Interaction,
            "decisions" => Self::Decision,
            "activity_types" => Self::ActivityType,
            "council_task_types" => Self::CouncilTaskType,
            "court_position_types" => Self::CourtPositionType,
            "court_position_tasks" => Self::CourtPositionTask,
            "scheme_countermeasures" => Self::SchemeCountermeasure,
            "laws" => Self::Law,
            "men_at_arms_types" => Self::MenAtArmsType,
            "notification_types" => Self::NotificationType,
            "event_definitions" => Self::EventDefinition,
            "casus_belli_types" => Self::CasusBelli,
            _ => return None,
        })
    }
}

#[derive(Default)]
struct Index {
    entries: BTreeMap<String, Entry>,
    errors: Vec<String>,
}

/// Definitions are indexed from the user's installed copy of CK3.
pub struct Catalog {
    game: PathBuf,
    indexes: Mutex<HashMap<QueryKind, Arc<Index>>>,
    labels: OnceLock<HashMap<String, String>>,
    map: OnceLock<Result<InstalledMap>>,
}

struct Entry {
    path: Arc<Path>,
    line: usize,
    body: String,
}

pub(crate) struct InteractionOptions {
    pub target_type: Option<String>,
    pub options: Vec<(String, String)>,
}

impl Catalog {
    pub fn open(installation: &Path) -> Result<Self> {
        let game = installation.join("game");
        ensure!(game.join("common").is_dir(), "游戏安装目录缺少 game/common");
        Ok(Self {
            game,
            indexes: Mutex::default(),
            labels: OnceLock::new(),
            map: OnceLock::new(),
        })
    }

    pub(crate) fn map(&self) -> Result<&InstalledMap> {
        self.map
            .get_or_init(|| InstalledMap::open(&self.game))
            .as_ref()
            .map_err(|error| anyhow::anyhow!("读取安装地图 · {error:#}"))
    }

    fn index(&self, kind: QueryKind) -> Result<Arc<Index>> {
        let mut indexes = self
            .indexes
            .lock()
            .map_err(|_| anyhow::anyhow!("游戏目录读取中断"))?;
        if let Some(index) = indexes.get(&kind) {
            return Ok(index.clone());
        }
        let roots: &[(&str, &str)] = match kind {
            QueryKind::Interaction => &[("common/character_interactions", "")],
            QueryKind::Decision => &[("common/decisions", "")],
            QueryKind::ActivityType => &[("common/activities/activity_types", "")],
            QueryKind::CouncilTaskType => &[("common/council_tasks", "")],
            QueryKind::CourtPositionType => &[("common/court_positions/types", "")],
            QueryKind::CourtPositionTask => &[("common/court_positions/tasks", "")],
            QueryKind::SchemeCountermeasure => &[("common/schemes/scheme_countermeasures", "")],
            QueryKind::Law => &[("common/laws", "")],
            QueryKind::MenAtArmsType => &[("common/men_at_arms_types", "")],
            QueryKind::NotificationType => &[
                ("common/messages", "message."),
                ("common/important_actions", "action."),
                ("common/suggestions", "suggestion."),
            ],
            QueryKind::EventDefinition => &[("events", "")],
            QueryKind::CasusBelli => &[("common/casus_belli_types", "")],
            _ => bail!("查询类别没有安装目录"),
        };
        let mut index = Index::default();
        for (root, prefix) in roots {
            for path in files(&self.game.join(root), "txt")? {
                let stem = path
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_ascii_lowercase();
                if kind == QueryKind::Interaction
                    && (stem.contains("debug") || stem.contains("test"))
                {
                    continue;
                }
                let source = match fs::read_to_string(&path) {
                    Ok(source) => source,
                    Err(error) => {
                        index.errors.push(format!("{} · {error}", path.display()));
                        continue;
                    }
                };
                let path: Arc<Path> = Arc::from(path);
                let mut last = 0;
                let mut line = 1;
                let definitions = match fields(&source) {
                    Ok(definitions) => definitions,
                    Err(error) => {
                        index.errors.push(format!("{} · {error:#}", path.display()));
                        continue;
                    }
                };
                for field in definitions {
                    if !field.block
                        || field.key.starts_with(['@', '$'])
                        || field.key.starts_with("debug_")
                    {
                        continue;
                    }
                    if kind == QueryKind::Interaction && interaction_spec(field.key).is_none() {
                        continue;
                    }
                    let offset = field.script.as_ptr() as usize - source.as_ptr() as usize;
                    line += source.as_bytes()[last..offset]
                        .iter()
                        .filter(|&&byte| byte == b'\n')
                        .count();
                    last = offset;
                    index.entries.insert(
                        format!("{prefix}{}", field.key),
                        Entry {
                            path: path.clone(),
                            line,
                            body: field.value.into(),
                        },
                    );
                }
            }
        }
        let index = Arc::new(index);
        indexes.insert(kind, index.clone());
        Ok(index)
    }

    pub(crate) fn text(&self, key: &str) -> Option<String> {
        let labels = self.labels.get_or_init(|| {
            let mut labels = HashMap::new();
            for language in ["english", "simp_chinese"] {
                let root = self.game.join("localization").join(language);
                for path in files(&root, "yml").unwrap_or_default() {
                    if let Ok(text) = fs::read_to_string(path) {
                        labels.extend(text.lines().filter_map(label));
                    }
                }
            }
            labels
        });
        let mut text = labels.get(key)?.clone();
        for _ in 0..4 {
            let mut expanded = String::new();
            let mut rest = text.as_str();
            while let Some((prefix, suffix)) = rest.split_once('$') {
                expanded.push_str(prefix);
                let Some((reference, tail)) = suffix.split_once('$') else {
                    rest = &rest[prefix.len()..];
                    break;
                };
                if let Some(label) = labels.get(reference) {
                    expanded.push_str(label);
                } else {
                    expanded.push('$');
                    expanded.push_str(reference);
                    expanded.push('$');
                }
                rest = tail;
            }
            expanded.push_str(rest);
            if expanded == text {
                break;
            }
            text = expanded;
        }
        Some(text)
    }

    fn localized(&self, key: &str) -> Value {
        let text = self.text(key);
        let resolved = text.as_ref().is_some_and(|text| !text.contains(['[', '$']));
        json!({"key":key, "text":text, "resolved":resolved})
    }

    pub(crate) fn render_localization(&self, value: &mut Value, context: &Value) {
        let Some(text) = value["text"].as_str() else {
            return;
        };
        let mut rendered = String::new();
        let mut unresolved = Vec::new();
        let mut tail = text;
        while let Some((prefix, expression)) = tail.split_once('[') {
            rendered.push_str(prefix);
            let Some((expression, remaining)) = expression.split_once(']') else {
                tail = &tail[prefix.len()..];
                break;
            };
            let resolved = expression.rsplit_once('.').and_then(|(name, function)| {
                let key = name.strip_prefix("scope:").unwrap_or(name);
                let scope = if matches!(key, "ROOT" | "ROOT.Char" | "root" | "root.char") {
                    &context["root"]
                } else {
                    context["targets"].get(key).or_else(|| {
                        key.strip_suffix(".Char")
                            .and_then(|key| context["targets"].get(key))
                    })?
                };
                match (scope["type"].as_str()?, function) {
                    (
                        "char",
                        "GetName" | "GetNameNoTooltip" | "GetFirstName" | "GetFirstNameNoTooltip",
                    ) => scope["character"]["name"].as_str().map(str::to_owned),
                    ("lt", "GetName" | "GetNameNoTooltip") => {
                        self.text(scope["title"]["key"].as_str()?)
                    }
                    _ => None,
                }
            });
            if let Some(resolved) = resolved {
                rendered.push_str(&resolved);
            } else {
                if !unresolved.contains(&expression) {
                    unresolved.push(expression);
                }
                rendered.push('[');
                rendered.push_str(expression);
                rendered.push(']');
            }
            tail = remaining;
        }
        rendered.push_str(tail);
        let resolved = !rendered.contains(['[', '$']);
        let unresolved = json!(unresolved);
        value["text"] = rendered.into();
        value["resolved"] = resolved.into();
        if !resolved {
            value["unresolved_expressions"] = unresolved;
        }
    }

    pub(crate) fn interaction_options(&self, name: &str) -> Result<InteractionOptions> {
        let index = self.index(QueryKind::Interaction)?;
        let entry = index
            .entries
            .get(name)
            .context("游戏安装目录没有此互动定义")?;
        let fields = fields(&entry.body)?;
        Ok(InteractionOptions {
            target_type: scalar(&fields, "target_type").map(str::to_owned),
            options: send_options(&fields)?,
        })
    }

    fn detail(&self, kind: QueryKind, id: &str) -> Result<Value> {
        let index = self.index(kind)?;
        let entry = index
            .entries
            .get(id)
            .with_context(|| format!("游戏安装目录找不到 {id}"))?;
        let body = fields(&entry.body)?;
        let key = id
            .split_once('.')
            .filter(|_| kind == QueryKind::NotificationType)
            .map_or(id, |(_, key)| key);
        let caption = scalar(&body, "name")
            .or_else(|| scalar(&body, "title"))
            .unwrap_or(key);
        let desc_key = format!("{key}_desc");
        let description = match body.iter().find(|field| field.key == "desc") {
            Some(field) if field.block => json!({"script":field.value, "resolved":false}),
            Some(field) => self.localized(unquote(field.value)),
            None => self.localized(&desc_key),
        };
        let properties: Map<String, Value> = body
            .iter()
            .filter(|field| !field.block)
            .map(|field| (field.key.to_owned(), unquote(field.value).into()))
            .collect();
        let conditions: Vec<_> = body
            .iter()
            .filter(|field| {
                ["is_", "can_", "valid_", "should_"]
                    .iter()
                    .any(|prefix| field.key.starts_with(prefix))
                    || matches!(field.key, "trigger" | "potential_county" | "ai_accept")
            })
            .map(|field| field.script)
            .collect();
        let effects: Vec<_> = body
            .iter()
            .filter(|field| {
                field.key.starts_with("on_")
                    || matches!(field.key, "effect" | "immediate" | "modifier")
            })
            .map(|field| field.script)
            .collect();
        let costs: Vec<_> = body
            .iter()
            .filter(|field| field.key.contains("cost") || field.key == "salary")
            .map(|field| field.script)
            .collect();
        let mut value = json!({"id":id, "name":self.localized(caption), "description":description,
            "group":group(kind, &body), "conditions":conditions, "effects":effects, "cost_script":costs,
            "properties":properties, "availability":"requires_game_evaluation",
            "source":{"kind":"installed_game_scripts", "file":entry.path.as_ref(), "line":entry.line}});
        if kind == QueryKind::Interaction {
            let (_, parameters) = interaction_spec(id).context("未支持的人物互动")?;
            value["target_type"] = json!(scalar(&body, "target_type"));
            value["special_interaction"] = json!(scalar(&body, "special_interaction"));
            value["special_parameters"] = json!(parameters);
            value["send_options"] = json!(send_options(&body)?.iter().enumerate().map(|(index, (flag, key))|
                json!({"index":index, "flag":flag, "localization_key":key, "label":self.text(key)})).collect::<Vec<_>>());
        }
        if kind == QueryKind::ActivityType {
            value["phases"] = json!(self.choices(&body, "phases")?);
            let options = block(&body, "options")
                .map(fields)
                .transpose()?
                .unwrap_or_default();
            value["options"] = json!(options.iter().filter(|item| item.block).map(|item| {
                Ok(json!({"id":item.key, "name":self.localized(item.key), "choices":self.children(item.value)?}))
            }).collect::<Result<Vec<_>>>()?);
            for key in ["host_intents", "guest_intents", "special_guests"] {
                value[key] = json!(block(&body, key));
            }
        }
        if kind == QueryKind::EventDefinition {
            value["title"] = self.localized(caption);
            value["options"] = json!(body.iter().filter(|field| field.block && field.key == "option")
                .enumerate().map(|(index, option)| {
                    let members = fields(option.value)?;
                    let effects: Vec<_> = members.iter().filter(|field| !matches!(field.key,
                        "name" | "trigger" | "show_as_unavailable" | "is_valid" | "ai_chance" | "trait" | "skill" | "flavor"))
                        .take(12).map(|field| field.script.split_whitespace().collect::<Vec<_>>().join(" "))
                        .map(|text| text.chars().take(240).collect::<String>()).collect();
                    Ok(json!({"index":index, "label":scalar(&members, "name").map(|key| self.localized(key)),
                        "conditional":block(&members,"trigger").is_some(), "effect_summary":effects,
                        "script":option.value.trim()}))
                }).collect::<Result<Vec<_>>>()?);
        }
        if let Some((action, parameter)) = match kind {
            QueryKind::Decision => Some(("execute_decision", "decision")),
            QueryKind::ActivityType => Some(("start_activity_command", "type")),
            QueryKind::CouncilTaskType => Some(("change_council_task", "task_type")),
            QueryKind::CourtPositionType => Some(("give_court_position", "court_position")),
            QueryKind::CourtPositionTask => Some(("start_court_position_task", "task_type")),
            QueryKind::SchemeCountermeasure => {
                Some(("set_countermeasure", "scheme_countermeasure_type"))
            }
            QueryKind::Law => Some(("add_law", "law")),
            _ => None,
        } {
            value["action"] = json!({"action":action, "parameters":{(parameter):id}});
        }
        Ok(value)
    }

    fn choices(&self, body: &[Field<'_>], key: &str) -> Result<Vec<Value>> {
        self.children(block(body, key).unwrap_or_default())
    }

    fn children(&self, script: &str) -> Result<Vec<Value>> {
        Ok(fields(script)?.into_iter().filter(|field| field.block).map(|field| {
            json!({"id":field.key, "name":self.localized(field.key), "script":field.value.trim()})
        }).collect())
    }

    fn list(
        &self,
        kind: QueryKind,
        search: &str,
        group_name: Option<&str>,
        offset: usize,
        limit: usize,
    ) -> Result<Value> {
        ensure!((1..=100).contains(&limit), "目录单页条数须为 1 到 100");
        let search = search.to_lowercase();
        let index = self.index(kind)?;
        let mut items = Vec::new();
        let mut count = 0;
        for (id, entry) in &index.entries {
            let fields = fields(&entry.body)?;
            let group = group(kind, &fields);
            if group_name.is_some() && group_name != group {
                continue;
            }
            let key = scalar(&fields, "name")
                .or_else(|| scalar(&fields, "title"))
                .unwrap_or(id);
            let name = self.text(key);
            if !search.is_empty()
                && !id.to_lowercase().contains(&search)
                && !name
                    .as_ref()
                    .is_some_and(|name| name.to_lowercase().contains(&search))
            {
                continue;
            }
            if count >= offset && items.len() < limit {
                items.push(json!({"id":id, "label":name, "group":group,
                    "target_type":scalar(&fields, "target_type"), "details_query":{"kind":kind, "id":id}}));
            }
            count += 1;
        }
        let next = offset.saturating_add(items.len());
        Ok(
            json!({"count":count, "offset":offset, "next_offset":(next < count).then_some(next), "items":items,
                "definition_errors":index.errors}),
        )
    }
}

impl QuerySource for Catalog {
    fn query(&self, request: Query<'_>) -> Result<Value> {
        match request {
            Query::Script { kind, id, .. } => self.detail(kind, id),
            Query::ScriptList {
                kind,
                search,
                group,
                offset,
                limit,
            } => self.list(kind, search, group, offset, limit),
            Query::ProvinceMap(id) => self.map()?.province(id),
            _ => bail!("请选择游戏脚本查询"),
        }
    }

    fn kinds(&self) -> &'static [QueryKind] {
        KINDS
    }
}

fn send_options(fields_in: &[Field<'_>]) -> Result<Vec<(String, String)>> {
    let mut options: Vec<(String, String)> = Vec::new();
    for field in fields_in
        .iter()
        .filter(|field| field.key == "send_option" && field.block)
    {
        let members = fields(field.value)?;
        let flag = scalar(&members, "flag").context("互动选项缺少脚本标记")?;
        ensure!(
            !options.iter().any(|(key, _)| key == flag),
            "互动选项标记重复 {flag}"
        );
        let localization = scalar(&members, "localization").unwrap_or(flag);
        options.push((flag.into(), localization.into()));
    }
    Ok(options)
}

fn group<'a>(kind: QueryKind, fields: &[Field<'a>]) -> Option<&'a str> {
    match kind {
        QueryKind::Interaction => scalar(fields, "category"),
        QueryKind::CouncilTaskType => scalar(fields, "position"),
        QueryKind::CourtPositionType => scalar(fields, "skill"),
        QueryKind::CourtPositionTask => {
            block(fields, "court_position_types").and_then(|body| body.split_whitespace().next())
        }
        QueryKind::Law => scalar(fields, "law_group_type"),
        QueryKind::MenAtArmsType => scalar(fields, "type"),
        QueryKind::NotificationType => scalar(fields, "message_filter_type"),
        _ => None,
    }
}

fn files(root: &Path, extension: &str) -> Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    let mut dirs = vec![root.to_path_buf()];
    while let Some(dir) = dirs.pop() {
        let children = match fs::read_dir(dir) {
            Ok(children) => children,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        for child in children {
            let child = child?;
            let kind = child.file_type()?;
            let path = child.path();
            if kind.is_dir() {
                dirs.push(path);
            } else if kind.is_file()
                && path
                    .extension()
                    .is_some_and(|value| value.eq_ignore_ascii_case(extension))
            {
                paths.push(path);
            }
        }
    }
    paths.sort();
    Ok(paths)
}

fn label(line: &str) -> Option<(String, String)> {
    let (key, value) = line.trim().split_once(':')?;
    if key.is_empty() || key.contains(char::is_whitespace) || key.starts_with('#') {
        return None;
    }
    let value = value
        .trim_start_matches(|ch: char| ch.is_ascii_digit() || ch.is_whitespace())
        .strip_prefix('"')?;
    let (value, _) = value.rsplit_once('"')?;
    Some((key.into(), value.replace("\\\"", "\"").replace("\\n", "\n")))
}

struct Field<'a> {
    key: &'a str,
    value: &'a str,
    script: &'a str,
    block: bool,
}

fn scalar<'a>(fields: &[Field<'a>], key: &str) -> Option<&'a str> {
    fields
        .iter()
        .find(|field| field.key == key && !field.block)
        .map(|field| unquote(field.value))
}

fn block<'a>(fields: &[Field<'a>], key: &str) -> Option<&'a str> {
    fields
        .iter()
        .find(|field| field.key == key && field.block)
        .map(|field| field.value)
}

fn unquote(value: &str) -> &str {
    value
        .strip_prefix('"')
        .and_then(|text| text.strip_suffix('"'))
        .unwrap_or(value)
}

fn fields(source: &str) -> Result<Vec<Field<'_>>> {
    let mut lexer = Lexer {
        source,
        offset: if source.starts_with('\u{feff}') { 3 } else { 0 },
    };
    let mut result = Vec::new();
    while let Some((start, key)) = lexer.next()? {
        match key {
            "{" => {
                lexer.end_block()?;
                continue;
            }
            "}" => bail!("脚本出现未匹配的右花括号"),
            _ => {}
        }
        let after_key = lexer.offset;
        if !matches!(lexer.next()?, Some((_, "="))) {
            lexer.offset = after_key;
            continue;
        }
        let (position, value) = lexer.next()?.context("脚本字段缺少值")?;
        let block = value == "{";
        let value = if block {
            let end = lexer.end_block()?;
            &source[position + 1..end]
        } else {
            value
        };
        result.push(Field {
            key,
            value,
            script: &source[start..lexer.offset],
            block,
        });
    }
    Ok(result)
}

struct Lexer<'a> {
    source: &'a str,
    offset: usize,
}

impl<'a> Lexer<'a> {
    fn next(&mut self) -> Result<Option<(usize, &'a str)>> {
        let bytes = self.source.as_bytes();
        loop {
            let Some(&byte) = bytes.get(self.offset) else {
                return Ok(None);
            };
            if byte.is_ascii_whitespace() {
                self.offset += 1;
            } else if byte == b'#' {
                self.offset += bytes[self.offset..]
                    .iter()
                    .position(|&byte| byte == b'\n')
                    .unwrap_or(bytes.len() - self.offset);
            } else {
                break;
            }
        }
        let start = self.offset;
        self.offset += 1;
        match bytes[start] {
            b'"' => loop {
                let byte = *bytes.get(self.offset).context("脚本字符串缺少结束引号")?;
                self.offset += 1;
                match byte {
                    b'"' => break,
                    b'\\' => self.offset += 1,
                    _ => {}
                }
            },
            b'{' | b'}' | b'=' => {}
            _ => {
                while bytes
                    .get(self.offset)
                    .is_some_and(|byte| !byte.is_ascii_whitespace() && !b"{}=#\"".contains(byte))
                {
                    self.offset += 1;
                }
            }
        }
        Ok(Some((start, &self.source[start..self.offset])))
    }

    fn end_block(&mut self) -> Result<usize> {
        let mut depth = 1_usize;
        while let Some((offset, value)) = self.next()? {
            match value {
                "{" => depth += 1,
                "}" => {
                    depth -= 1;
                    if depth == 0 {
                        return Ok(offset);
                    }
                }
                _ => {}
            }
        }
        bail!("脚本出现未匹配的左花括号")
    }
}
