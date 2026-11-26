use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Value, json};

use super::{Lexer, block, fields, scalar};

#[derive(Clone, Copy, Serialize)]
pub(crate) struct Position {
    x: f64,
    z: f64,
}

pub(crate) struct Map {
    names: HashMap<u32, String>,
    kinds: HashMap<u32, &'static str>,
    terrain: HashMap<u32, String>,
    land: Option<String>,
    sea: Option<String>,
    positions: HashMap<u32, Position>,
}

impl Map {
    pub fn open(game: &Path) -> Result<Self> {
        let directory = game.join("map_data");
        let config = fs::read_to_string(directory.join("default.map"))?;
        let definition = definition_path(&config)?;
        let mut names = HashMap::new();
        for line in fs::read_to_string(directory.join(definition))?.lines() {
            let mut columns = line.trim_start_matches('\u{feff}').split(';');
            let Some(id) = columns
                .next()
                .and_then(|text| text.parse::<u32>().ok().filter(|&id| id != 0))
            else {
                continue;
            };
            if let Some(name) = columns.nth(3) {
                names.insert(id, name.into());
            }
        }
        let mut kinds = HashMap::new();
        let mut tokens = Lexer {
            source: &config,
            offset: 0,
        };
        while let Some((_, token)) = tokens.next()? {
            let Some(kind) = [
                "sea_zones",
                "river_provinces",
                "lakes",
                "impassable_mountains",
                "impassable_seas",
            ]
            .into_iter()
            .find(|&kind| token.eq_ignore_ascii_case(kind)) else {
                continue;
            };
            ensure!(matches!(tokens.next()?, Some((_, "="))), "地图类型缺少赋值");
            let mode = tokens.next()?.context("地图类型缺少范围")?.1;
            let (start, open) = tokens.next()?.context("地图类型缺少列表")?;
            ensure!(open == "{", "地图类型列表无效");
            let end = tokens.end_block()?;
            let mut values = Lexer {
                source: &config[start + 1..end],
                offset: 0,
            };
            let mut ids = Vec::new();
            while let Some((_, id)) = values.next()? {
                ids.push(id.parse::<u32>()?);
            }
            match (mode.to_ascii_uppercase().as_str(), ids.as_slice()) {
                ("RANGE", [first, last]) => {
                    ensure!(last >= first && last - first <= 100_000, "地图省份范围无效");
                    kinds.extend((*first..=*last).map(|id| (id, kind)));
                }
                ("LIST", ids) => kinds.extend(ids.iter().map(|&id| (id, kind))),
                _ => anyhow::bail!("地图类型范围无效"),
            }
        }
        let position_file = game.join("gfx/map/map_object_data/player_stack_locators.txt");
        let mut positions = HashMap::new();
        if position_file.is_file() {
            let source = fs::read_to_string(position_file)?;
            let root = fields(&source)?;
            let body = fields(block(&root, "game_object_locator").unwrap_or_default())?;
            let mut instances = Lexer {
                source: block(&body, "instances").unwrap_or_default(),
                offset: 0,
            };
            while let Some((start, token)) = instances.next()? {
                if token != "{" {
                    continue;
                }
                let end = instances.end_block()?;
                let row = fields(&instances.source[start + 1..end])?;
                if let (Some(id), Some(position)) = (scalar(&row, "id"), block(&row, "position")) {
                    let coords = position
                        .split_whitespace()
                        .map(str::parse::<f64>)
                        .collect::<std::result::Result<Vec<_>, _>>()?;
                    if let &[x, _, z] = coords.as_slice()
                        && x.is_finite()
                        && z.is_finite()
                    {
                        positions.insert(id.parse()?, Position { x, z });
                    }
                }
            }
        }
        let mut terrain = HashMap::new();
        let mut land = None;
        let mut sea = None;
        let terrain_file = game.join("common/province_terrain/00_province_terrain.txt");
        if terrain_file.is_file() {
            for field in fields(&fs::read_to_string(terrain_file)?)? {
                match field.key {
                    "default_land" => land = Some(field.value.into()),
                    "default_sea" => sea = Some(field.value.into()),
                    key => {
                        if let Ok(id) = key.parse() {
                            terrain.insert(id, field.value.into());
                        }
                    }
                }
            }
        }
        Ok(Self {
            names,
            kinds,
            terrain,
            land,
            sea,
            positions,
        })
    }

    pub fn land(&self, id: u32) -> bool {
        self.names.contains_key(&id) && !self.kinds.contains_key(&id)
    }

    pub fn exists(&self, id: u32) -> bool {
        self.names.contains_key(&id)
    }

    pub fn position(&self, id: u32) -> Option<Position> {
        self.positions.get(&id).copied()
    }

    pub fn terrain(&self, id: u32) -> Option<&str> {
        let kind = self.kinds.get(&id).copied().unwrap_or("land");
        self.terrain.get(&id).map(String::as_str).or(match kind {
            "land" => self.land.as_deref(),
            "sea_zones" | "impassable_seas" => self.sea.as_deref(),
            _ => None,
        })
    }

    pub fn province(&self, id: u32) -> Result<Value> {
        let name = self.names.get(&id).context("安装地图没有此省份")?;
        Ok(
            json!({"province_id":id, "map_name":name, "map_type":self.kinds.get(&id).unwrap_or(&"land"),
            "terrain":self.terrain(id), "land_passable":self.land(id), "position":self.position(id),
            "position_source":"installed_player_stack_locator", "distance_unit":"map_units"}),
        )
    }
}

impl Position {
    pub fn direction(self, target: Self) -> (f64, Value) {
        let dx = target.x - self.x;
        let dz = target.z - self.z;
        let angle = dz.atan2(dx).to_degrees();
        let index = ((angle + 382.5) / 45.0) as usize % 8;
        let cardinal = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"][index];
        let round = |value: f64| (value * 100.0).round() / 100.0;
        (
            round(dx.hypot(dz)),
            json!({"dx":round(dx), "dz":round(dz), "cardinal":cardinal}),
        )
    }
}

fn definition_path(config: &str) -> Result<PathBuf> {
    let config = fields(config)?;
    let definition =
        PathBuf::from(scalar(&config, "definitions").context("安装地图缺少省份定义表")?);
    ensure!(
        definition.components().count() == 1 && definition.file_name().is_some(),
        "安装地图的省份表文件名无效"
    );
    Ok(definition)
}

#[test]
#[ignore = "needs CK3_GAME_DIR"]
fn installed_map_resolves_land_and_positions() -> Result<()> {
    let dir = PathBuf::from(std::env::var_os("CK3_GAME_DIR").context("CK3_GAME_DIR missing")?);
    let map = Map::open(&dir.join("game"))?;
    let value = map.province(2214)?;
    assert_eq!(value["land_passable"], true, "{value}");
    assert!(value["position"].is_object(), "{value}");
    Ok(())
}
