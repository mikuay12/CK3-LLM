use std::path::Path;
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

use super::{Query, QueryKind, QuerySource, World, binary::decode, date_text};

const ROOT_VALUES: &[&str] = &[
    "date",
    "speed",
    "currently_played_characters",
    "played_character",
    "meta_data",
    "player_event",
];
const TABLES: &[&str] = &[
    "snapshots",
    "active_snapshot",
    "root_values",
    "characters",
    "provinces",
    "titles",
    "observed_commands",
];

const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, date_raw INTEGER NOT NULL,
        date_text TEXT NOT NULL, snapshot_tick INTEGER, source_path TEXT, world_bytes INTEGER NOT NULL,
        imported_at TEXT NOT NULL, character_count INTEGER NOT NULL, province_count INTEGER NOT NULL,
        title_count INTEGER NOT NULL, deleted_title_slots INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS active_snapshot (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1), snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
    );
    CREATE TABLE IF NOT EXISTS root_values (key TEXT PRIMARY KEY, data_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS characters (
        id INTEGER NOT NULL, section TEXT NOT NULL, instance INTEGER NOT NULL, data_json TEXT NOT NULL,
        PRIMARY KEY(section, id, instance)
    );
    CREATE INDEX IF NOT EXISTS characters_by_id ON characters(id);
    CREATE TABLE IF NOT EXISTS provinces (id INTEGER PRIMARY KEY, data_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS titles (id INTEGER PRIMARY KEY, script_key TEXT UNIQUE, holder_id INTEGER, data_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS titles_by_holder ON titles(holder_id);
    CREATE TABLE IF NOT EXISTS observed_commands (
        id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, snapshot_id INTEGER REFERENCES snapshots(id),
        direction TEXT NOT NULL, source_user_id TEXT, source_payload_file TEXT, command_type TEXT NOT NULL,
        command_identity INTEGER, data_json TEXT NOT NULL, capture_key TEXT
    );
    CREATE INDEX IF NOT EXISTS commands_by_type ON observed_commands(command_type);
";

pub struct WorldStore {
    connection: Mutex<Connection>,
}

pub(crate) struct Observation {
    pub time: Option<String>,
    pub direction: &'static str,
    pub source: String,
    pub packet: String,
    pub name: String,
    pub identity: Option<u32>,
    pub data: Value,
    pub key: String,
}

impl WorldStore {
    pub fn open(path: &Path) -> Result<Self> {
        let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        connection.busy_timeout(Duration::from_secs(10))?;
        ensure!(has_schema(&connection)?, "此数据库没有 CK3 快照表");
        connection
            .query_row(
                "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1",
                [],
                |_| Ok(()),
            )
            .context("数据库中没有已导入的 CK3 快照")?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn save(path: &Path, world: &World, source: &Path) -> Result<Self> {
        Self::index(path, world, source, None, std::iter::empty())
    }

    pub(crate) fn index(
        path: &Path,
        world: &World,
        source: &Path,
        tick: Option<u32>,
        commands: impl IntoIterator<Item = Result<Observation>>,
    ) -> Result<Self> {
        let source = source.canonicalize().unwrap_or_else(|_| source.to_owned());
        let mut connection = Connection::open(path)?;
        connection.busy_timeout(Duration::from_secs(10))?;
        connection.pragma_update(None, "foreign_keys", true)?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if !has_schema(&transaction)? {
            let count: u32 = transaction.query_row(
                "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
                [],
                |row| row.get(0),
            )?;
            ensure!(
                count == 0,
                "所选数据库已有其他数据，请选择 CK3 数据库或新文件"
            );
        }
        transaction.execute_batch(SCHEMA)?;
        migrate(&transaction)?;
        let previous: Option<(i64, String)> = transaction.query_row(
            "SELECT s.id, s.sha256 FROM active_snapshot a JOIN snapshots s ON s.id=a.snapshot_id WHERE a.singleton=1",
            [], |row| Ok((row.get(0)?, row.get(1)?)),
        ).optional()?;
        let snapshot;
        if let Some((id, _)) = previous.filter(|(_, hash)| hash == &world.source_sha256) {
            transaction.execute("UPDATE snapshots SET source_path=?2, snapshot_tick=coalesce(?3, snapshot_tick) WHERE id=?1",
                params![id, source.to_string_lossy(), tick])?;
            snapshot = id;
        } else {
            snapshot = replace_snapshot(&transaction, world, &source, tick)?;
        }
        {
            let mut write = transaction.prepare(
                "INSERT INTO observed_commands(observed_at,snapshot_id,direction,source_user_id,source_payload_file,
                    command_type,command_identity,data_json,capture_key)
                 VALUES(coalesce(?1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),?2,?3,?4,?5,?6,?7,?8,?9)
                 ON CONFLICT(snapshot_id,capture_key) DO NOTHING",
            )?;
            for observation in commands {
                let observation = observation?;
                write.execute(params![
                    observation.time,
                    snapshot,
                    observation.direction,
                    observation.source,
                    observation.packet,
                    observation.name,
                    observation.identity,
                    observation.data.to_string(),
                    observation.key
                ])?;
            }
        }
        transaction.commit()?;
        Self::open(path)
    }
}

fn has_schema(connection: &Connection) -> Result<bool> {
    let mut statement =
        connection.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")?;
    let tables = statement
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(TABLES
        .iter()
        .all(|name| tables.iter().any(|table| table == name)))
}

fn migrate(connection: &Connection) -> Result<()> {
    if !has_column(connection, "PRAGMA table_info(characters)", "instance")? {
        connection.execute_batch(
            "
            ALTER TABLE characters RENAME TO characters_legacy;
            CREATE TABLE characters (
                id INTEGER NOT NULL, section TEXT NOT NULL, instance INTEGER NOT NULL,
                data_json TEXT NOT NULL, PRIMARY KEY(section,id,instance)
            );
            INSERT INTO characters SELECT id,section,0,data_json FROM characters_legacy;
            DROP TABLE characters_legacy;
            CREATE INDEX characters_by_id ON characters(id);
        ",
        )?;
    }
    if !has_column(
        connection,
        "PRAGMA table_info(observed_commands)",
        "capture_key",
    )? {
        connection.execute(
            "ALTER TABLE observed_commands ADD COLUMN capture_key TEXT",
            [],
        )?;
    }
    connection.execute_batch("CREATE UNIQUE INDEX IF NOT EXISTS commands_by_capture ON observed_commands(snapshot_id,capture_key)")?;
    Ok(())
}

fn has_column(connection: &Connection, query: &str, column: &str) -> Result<bool> {
    let mut statement = connection.prepare(query)?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(columns.iter().any(|name| name == column))
}

fn replace_snapshot(
    connection: &Connection,
    world: &World,
    source: &Path,
    tick: Option<u32>,
) -> Result<i64> {
    let date = world
        .root
        .get("date")
        .context("快照缺少日期")
        .and_then(|spans| world.values(spans))?;
    let raw = u32::try_from(date.as_u64().context("快照日期无效")?)?;
    connection.execute_batch("DELETE FROM root_values; DELETE FROM characters; DELETE FROM provinces; DELETE FROM titles;")?;
    {
        let mut insert =
            connection.prepare("INSERT INTO root_values(key,data_json) VALUES(?1,?2)")?;
        for &name in ROOT_VALUES {
            if let Some(spans) = world.root.get(name) {
                insert.execute(params![name, world.values(spans)?.to_string()])?;
            }
        }
        connection.execute(
            "INSERT OR IGNORE INTO root_values VALUES('player_event','[]')",
            [],
        )?;
    }
    let mut characters = 0_i64;
    let mut provinces = 0_i64;
    let mut titles = 0_i64;
    let mut deleted = 0_i64;
    {
        let mut insert_character = connection
            .prepare("INSERT INTO characters(id,section,instance,data_json) VALUES(?1,?2,?3,?4)")?;
        let mut insert_province =
            connection.prepare("INSERT INTO provinces(id,data_json) VALUES(?1,?2)")?;
        let mut insert_title = connection
            .prepare("INSERT INTO titles(id,script_key,holder_id,data_json) VALUES(?1,?2,?3,?4)")?;
        for section in [
            "living",
            "dead_unprunable",
            "characters",
            "provinces",
            "landed_titles",
        ] {
            if !world.root.contains_key(section) {
                continue;
            }
            for (key, instances) in world.records(section)?.iter() {
                let id = key
                    .parse::<u32>()
                    .with_context(|| format!("快照 {section} 的编号 {key} 无效"))?;
                for (instance, span) in instances.iter().enumerate() {
                    let data = decode(world.binary(), span.clone())?;
                    if data == "none" {
                        deleted += i64::from(section == "landed_titles");
                        continue;
                    }
                    ensure!(data.is_object(), "快照 {section} 对象 {id} 无效");
                    let encoded = data.to_string();
                    match section {
                        "provinces" => {
                            insert_province.execute(params![id, encoded])?;
                            provinces += 1;
                        }
                        "landed_titles" => {
                            let script = data["key"].as_str();
                            let holder = data["holder"]
                                .as_u64()
                                .and_then(|id| u32::try_from(id).ok());
                            insert_title.execute(params![id, script, holder, encoded])?;
                            titles += 1;
                        }
                        _ => {
                            insert_character.execute(params![
                                id,
                                section,
                                i64::try_from(instance)?,
                                encoded
                            ])?;
                            characters += 1;
                        }
                    }
                }
            }
        }
    }
    let id = connection.query_row(
        "INSERT INTO snapshots(sha256,date_raw,date_text,snapshot_tick,source_path,world_bytes,
            imported_at,character_count,province_count,title_count,deleted_title_slots)
         VALUES(?1,?2,?3,?4,?5,?6,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?7,?8,?9,?10)
         ON CONFLICT(sha256) DO UPDATE SET source_path=excluded.source_path,
            snapshot_tick=coalesce(excluded.snapshot_tick,snapshots.snapshot_tick),
            character_count=excluded.character_count,province_count=excluded.province_count,
            title_count=excluded.title_count,deleted_title_slots=excluded.deleted_title_slots
         RETURNING id",
        params![
            world.source_sha256,
            raw,
            date_text(raw),
            tick,
            source.to_string_lossy(),
            i64::try_from(world.binary().len())?,
            characters,
            provinces,
            titles,
            deleted
        ],
        |row| row.get(0),
    )?;
    connection.execute("INSERT INTO active_snapshot VALUES(1,?1) ON CONFLICT(singleton) DO UPDATE SET snapshot_id=excluded.snapshot_id", [id])?;
    Ok(id)
}

impl QuerySource for WorldStore {
    fn kinds(&self) -> &'static [QueryKind] {
        &[
            QueryKind::Summary,
            QueryKind::Character,
            QueryKind::Title,
            QueryKind::Province,
            QueryKind::ObservedCommands,
        ]
    }

    fn query(&self, request: Query<'_>) -> Result<Value> {
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| anyhow::anyhow!("快照数据库读取中断"))?;
        let view = connection.transaction()?;
        match request {
            Query::Summary => summary(&view),
            Query::Character(id) => character(&view, id),
            Query::Title(key) => {
                let (sql, parameter) = if let Ok(id) = key.parse::<u32>() {
                    (
                        "SELECT id, data_json FROM titles WHERE id=?1",
                        rusqlite::types::Value::Integer(id.into()),
                    )
                } else {
                    (
                        "SELECT id, data_json FROM titles WHERE script_key=?1",
                        rusqlite::types::Value::Text(key.into()),
                    )
                };
                entity(&view, sql, parameter)
            }
            Query::Province(id) => {
                entity(&view, "SELECT id, data_json FROM provinces WHERE id=?1", id)
            }
            Query::ObservedCommands {
                name,
                offset,
                limit,
            } => observations(&view, name, offset, limit),
            _ => bail!("此查询需要实时游戏或安装目录"),
        }
    }
}

fn summary(connection: &Connection) -> Result<Value> {
    let mut result = connection.query_row(
        "SELECT s.id,s.sha256,s.date_raw,s.date_text,s.snapshot_tick,s.source_path,s.world_bytes,
                s.imported_at,s.character_count,s.province_count,s.title_count,s.deleted_title_slots
         FROM active_snapshot a JOIN snapshots s ON s.id=a.snapshot_id WHERE a.singleton=1",
         [], |row| {
            Ok(json!({"source":"sqlite_world_store", "id":row.get::<_,i64>(0)?, "source_sha256":row.get::<_,String>(1)?,
                "date_raw":row.get::<_,u32>(2)?, "date":row.get::<_,String>(3)?, "tick":row.get::<_,Option<u32>>(4)?,
                "source_path":row.get::<_,Option<String>>(5)?, "world_bytes":row.get::<_,i64>(6)?,
                "imported_at":row.get::<_,String>(7)?, "record_counts":{"characters":row.get::<_,i64>(8)?,
                    "provinces":row.get::<_,i64>(9)?, "landed_titles":row.get::<_,i64>(10)?, "deleted_title_slots":row.get::<_,i64>(11)?}}))
         },
    )?;
    let mut statement = connection.prepare("SELECT key,data_json FROM root_values")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let key: String = row.get(0)?;
        if key != "date" {
            result[key] = serde_json::from_str(&row.get::<_, String>(1)?)?;
        }
    }
    let count: i64 = connection.query_row("SELECT count(*) FROM observed_commands", [], |row| {
        row.get(0)
    })?;
    result["observed_commands"] = count.into();
    Ok(result)
}

fn character(connection: &Connection, id: u32) -> Result<Value> {
    let order = if has_column(connection, "PRAGMA table_info(characters)", "instance")? {
        "instance"
    } else {
        "rowid"
    };
    let sql = format!("SELECT section,data_json FROM characters WHERE id=?1
        ORDER BY CASE section WHEN 'living' THEN 0 WHEN 'dead_unprunable' THEN 1 ELSE 2 END, {order}");
    let mut statement = connection.prepare(&sql)?;
    let mut rows = statement.query([id])?;
    let first = rows
        .next()?
        .with_context(|| format!("数据库没有人物 {id}"))?;
    let section: String = first.get(0)?;
    let mut instances = vec![serde_json::from_str::<Value>(&first.get::<_, String>(1)?)?];
    while let Some(row) = rows.next()? {
        if row.get::<_, String>(0)? != section {
            break;
        }
        instances.push(serde_json::from_str(&row.get::<_, String>(1)?)?);
    }
    let data = if instances.len() == 1 {
        instances.pop().expect("first instance")
    } else {
        Value::Array(instances)
    };
    Ok(json!({"id":id, "section":section, "data":data}))
}

fn entity(connection: &Connection, sql: &str, parameter: impl rusqlite::ToSql) -> Result<Value> {
    let (id, data): (u32, String) = connection
        .query_row(sql, [parameter], |row| Ok((row.get(0)?, row.get(1)?)))
        .context("快照中没有对应记录")?;
    Ok(json!({"id":id, "data":serde_json::from_str::<Value>(&data)?}))
}

fn observations(connection: &Connection, name: &str, offset: usize, limit: usize) -> Result<Value> {
    ensure!((1..=100).contains(&limit), "历史命令单页条数须为 1 到 100");
    let mut statement = connection.prepare(
        "SELECT id,observed_at,snapshot_id,direction,source_user_id,source_payload_file,command_type,command_identity,data_json
         FROM observed_commands WHERE ?1='' OR command_type=?1 ORDER BY id LIMIT ?2 OFFSET ?3",
    )?;
    let mut rows = statement.query(params![
        name,
        i64::try_from(limit + 1)?,
        i64::try_from(offset)?
    ])?;
    let mut items = Vec::new();
    while let Some(row) = rows.next()? {
        items.push(
            json!({"id":row.get::<_,i64>(0)?, "time":row.get::<_,String>(1)?,
            "snapshot_id":row.get::<_,Option<i64>>(2)?, "direction":row.get::<_,String>(3)?,
            "source":row.get::<_,Option<String>>(4)?, "packet":row.get::<_,Option<String>>(5)?,
            "type":row.get::<_,String>(6)?, "identity":row.get::<_,Option<u32>>(7)?,
            "command":serde_json::from_str::<Value>(&row.get::<_,String>(8)?)?}),
        );
    }
    let more = items.len() > limit;
    items.truncate(limit);
    Ok(json!({"offset":offset, "next_offset":more.then(|| offset + limit), "items":items}))
}
