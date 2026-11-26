use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet};

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use crate::world::{Navigation, scripts::InstalledMap};

use super::governance::page;
use super::military::{Edge, UNIT, Unit};
use super::{CHARACTER, Catalogs, Memory, TITLE, WorldView, date_text};

impl Catalogs {
    pub fn navigation(
        &mut self,
        memory: &dyn Memory,
        base: u64,
        map: &InstalledMap,
        request: Navigation<'_>,
    ) -> Result<Value> {
        let (clock, data) =
            self.consistent(memory, base, true, |view| view.navigate(map, request))?;
        Ok(json!({"source":"live", "date":date_text(clock.date), "tick":clock.tick, "data":data}))
    }
}

impl WorldView<'_> {
    fn navigate(&mut self, map: &InstalledMap, request: Navigation<'_>) -> Result<Value> {
        match request {
            Navigation::Route {
                origin,
                target,
                speed,
                unit,
                include_straits,
                max_nodes,
            } => {
                ensure!(
                    map.exists(origin) && map.exists(target),
                    "地图中找不到起点或目标省份"
                );
                let speed = match (speed, unit) {
                    (Some(speed), _) => Some(speed),
                    (None, Some(unit)) => {
                        let object = self.lookup(UNIT, unit)?;
                        Some(self.read.fixed(object + 0x190)?)
                    }
                    _ => None,
                };
                let paths = shortest(origin, BTreeSet::from([target]), max_nodes, |id| {
                    self.land_edges(map, id, include_straits)
                })?;
                Ok(paths.describe(map, origin, target, speed))
            }
            Navigation::Borders {
                realm,
                other,
                neighbors,
                include_straits,
                offset,
                limit,
            } => self.borders(
                map,
                realm,
                other,
                neighbors,
                include_straits,
                (offset, limit),
            ),
            Navigation::Theater {
                war,
                character,
                max_armies,
                max_targets,
                max_nodes,
            } => self.theater(map, war, character, max_armies, max_targets, max_nodes),
        }
    }

    fn land_edges(
        &mut self,
        map: &InstalledMap,
        from: u32,
        include_straits: bool,
    ) -> Result<Vec<Edge>> {
        if !map.land(from) {
            return Ok(Vec::new());
        }
        Ok(self
            .adjacencies(from)?
            .into_iter()
            .filter(|edge| {
                map.land(edge.province_id)
                    && edge.crossing != "impassable"
                    && (include_straits || edge.crossing != "strait")
            })
            .collect())
    }

    fn borders(
        &mut self,
        map: &InstalledMap,
        key: &str,
        other: Option<&str>,
        neighbors: bool,
        include_straits: bool,
        (offset, limit): (usize, usize),
    ) -> Result<Value> {
        ensure!((1..=100).contains(&limit), "边界查询每页需要 1 到 100 项");
        let character = self.ruler(key)?;
        let origin = self
            .realm(Some(character))?
            .context("边界查询需要在位领主")?;
        let ruler = realm_ruler(&origin).context("领地统治者暂不可用")?;
        let other = match other {
            Some(key) => {
                let character = self.ruler(key)?;
                self.realm(Some(character))?
                    .and_then(|realm| realm_ruler(&realm))
            }
            None => None,
        };
        let mut holders = HashMap::new();
        let mut owned = BTreeMap::new();
        let mut provinces = HashMap::new();
        let count = self.provinces()?.count;
        for id in (1..count).filter(|&id| map.land(id)) {
            let object = self.province_object(id)?;
            let Some(title) = self.read.reference(object + 0x738)? else {
                continue;
            };
            let Some(holder) = self.title_holder(Some(title), None)? else {
                continue;
            };
            let top = match holders.get(&holder) {
                Some(top) => *top,
                None => {
                    let top = self.realm(Some(holder))?.as_ref().and_then(realm_ruler);
                    holders.insert(holder, top);
                    top
                }
            };
            if let Some(top) = top {
                provinces.insert(id, (title, top));
                if top == ruler {
                    owned.insert(id, title);
                }
            }
        }
        let mut borders = Vec::new();
        let mut neighbor_counts = BTreeMap::<u32, (usize, Value)>::new();
        for (&id, &title) in &owned {
            for edge in self.land_edges(map, id, include_straits)? {
                let Some(&(neighbor_title, top)) = provinces.get(&edge.province_id) else {
                    continue;
                };
                if top == ruler || other.is_some_and(|other| other != top) {
                    continue;
                }
                let realm = self.realm(Some(top))?;
                if neighbors {
                    let entry = neighbor_counts.entry(top).or_insert((0, json!(realm)));
                    entry.0 += 1;
                } else {
                    borders.push(json!({"province_id":id, "title":self.title_overview(title)?,
                        "neighbor_province_id":edge.province_id, "neighbor_title":self.title_overview(neighbor_title)?,
                        "realm":realm, "crossing":edge.crossing, "through_province_id":edge.through_province_id}));
                }
            }
        }
        if neighbors {
            borders = neighbor_counts
                .into_values()
                .map(|(count, realm)| json!({"realm":realm,"border_count":count}))
                .collect();
        }
        let mut result = page(
            borders.len(),
            offset,
            borders.into_iter().skip(offset).take(limit).collect(),
        );
        result["origin"] = origin;
        Ok(result)
    }

    fn theater(
        &mut self,
        map: &InstalledMap,
        war_id: u32,
        character: Option<u32>,
        max_armies: usize,
        max_targets: usize,
        max_nodes: usize,
    ) -> Result<Value> {
        ensure!(
            (1..=20).contains(&max_armies) && (1..=32).contains(&max_targets),
            "战区查询结果条数超出范围"
        );
        let war = self.war(war_id)?;
        let attackers = participants(&war["attacker"]);
        let defenders = participants(&war["defender"]);
        let (friendly, enemy, side) = match character {
            Some(id) if defenders.contains(&id) => (&defenders, &attackers, "defender"),
            Some(id) if !attackers.contains(&id) => {
                anyhow::bail!("角色没有参与此战争");
            }
            _ => (&attackers, &defenders, "attacker"),
        };
        let own_units = self.theater_units(friendly, character, max_armies)?;
        let enemy_units = self.theater_units(enemy, None, max_armies)?;
        let mut targets = BTreeSet::new();
        let mut stack: Vec<u32> = serde_json::from_value(war["target_title_ids"].clone())?;
        let mut seen = HashSet::new();
        while let Some(title) = stack.pop() {
            if targets.len() >= max_targets {
                break;
            }
            if !seen.insert(title) {
                continue;
            }
            let object = self.lookup(TITLE, title)?;
            let overview = self.title_overview(title)?;
            if overview.tier == "barony" {
                targets.extend(self.barony_province(title)?);
            } else {
                stack.extend(self.read.ids(object + 0x110)?);
            }
        }
        let enemy_capital = enemy
            .first()
            .map(|&leader| self.capital_province(leader))
            .transpose()?
            .flatten();
        let friendly_capital = friendly
            .first()
            .map(|&leader| self.capital_province(leader))
            .transpose()?
            .flatten();
        let ref_character = character.or_else(|| friendly.first().copied());
        let choose_provinces: BTreeSet<_> = targets.iter().copied().chain(enemy_capital).collect();
        let mut provinces = Vec::new();
        for &province in &choose_provinces {
            let data = self.province(province)?;
            let supply_limit = if let Some(character) = ref_character {
                let character = self.lookup(CHARACTER, character)?;
                let object = self.province_object(province)?;
                self.shadow()
                    .and_then(|mut engine| engine.supply_limit(object, character))
                    .ok()
            } else {
                None
            };
            provinces.push(json!({"province_id":province, "map":map.province(province).ok(),
                "title":data["title"], "fort_level":data["fort_level"], "occupier":data["occupying_character_id"],
                "war_target":targets.contains(&province), "enemy_capital":enemy_capital == Some(province), "supply_limit":supply_limit}));
        }
        let mut armies = Vec::new();
        for unit in own_units {
            let Some(army) = unit.army_id else { continue };
            let detail = self.army(army)?;
            let mut route_data = Value::Null;
            if let Some(origin) = unit.province_id {
                let unit = self.lookup(UNIT, unit.id)?;
                let speed = self.read.fixed(unit + 0x190)?;
                route_data = match shortest(origin, choose_provinces.clone(), max_nodes, |id| {
                    self.land_edges(map, id, true)
                }) {
                    Ok(paths) => json!(
                        choose_provinces
                            .iter()
                            .map(|&target| paths.describe(map, origin, target, Some(speed)))
                            .collect::<Vec<_>>()
                    ),
                    Err(error) => json!({"status":"unavailable", "reason":format!("{error:#}")}),
                };
            }
            armies.push(json!({"army":detail, "routes":route_data}));
        }
        let enemies = enemy_units
            .into_iter()
            .filter_map(|unit| unit.army_id)
            .map(|id| self.army(id))
            .collect::<Result<Vec<_>>>()?;
        Ok(
            json!({"war":war, "side":side, "character_id":ref_character, "friendly_capital":friendly_capital,
            "enemy_capital":enemy_capital, "targets":provinces, "friendly_armies":armies, "enemy_armies":enemies}),
        )
    }

    fn theater_units(
        &mut self,
        participants: &[u32],
        first: Option<u32>,
        limit: usize,
    ) -> Result<Vec<Unit>> {
        let mut units = Vec::new();
        let mut seen = BTreeSet::new();
        for character in first.into_iter().chain(
            participants
                .iter()
                .copied()
                .filter(|&participant| first != Some(participant)),
        ) {
            for unit in self.units_for(character)? {
                if unit.owner_id == Some(character)
                    && unit.army_id.is_some_and(|army| seen.insert(army))
                {
                    units.push(unit);
                }
                if units.len() == limit {
                    return Ok(units);
                }
            }
        }
        Ok(units)
    }

    fn capital_province(&mut self, character: u32) -> Result<Option<u32>> {
        let object = self.lookup(CHARACTER, character)?;
        let landed = self.read.pointer(object + 0x1c0)?;
        if landed == 0 {
            return Ok(None);
        }
        let Some(capital) = self.read.reference(landed + 0x1f8)? else {
            return Ok(None);
        };
        let capital = self.title(capital, None)?;
        if let Some(id) = capital.province_id {
            return Ok(Some(id));
        }
        match capital.capital_title_id {
            Some(id) => self.barony_province(id),
            None => Ok(None),
        }
    }
}

fn realm_ruler(realm: &Value) -> Option<u32> {
    realm["ruler"]["id"]
        .as_u64()
        .and_then(|id| u32::try_from(id).ok())
}

fn participants(side: &Value) -> Vec<u32> {
    side["participants"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            row["character"]["id"]
                .as_u64()
                .and_then(|id| u32::try_from(id).ok())
        })
        .collect()
}

struct Routes {
    distances: HashMap<u32, u64>,
    parents: HashMap<u32, (u32, Edge)>,
}

fn shortest(
    origin: u32,
    mut goals: BTreeSet<u32>,
    budget: usize,
    mut edges: impl FnMut(u32) -> Result<Vec<Edge>>,
) -> Result<Routes> {
    ensure!(
        (1..=20_000).contains(&budget),
        "路线搜索预算须为 1 到 20000 个省份"
    );
    let mut paths = Routes {
        distances: HashMap::from([(origin, 0)]),
        parents: HashMap::new(),
    };
    let mut pending = BinaryHeap::from([Reverse((0_u64, origin))]);
    let mut visited = 0;
    while let Some(Reverse((cost, from))) = pending.pop() {
        if paths.distances[&from] != cost {
            continue;
        }
        visited += 1;
        ensure!(visited <= budget, "路线搜索超过省份预算");
        goals.remove(&from);
        if goals.is_empty() {
            break;
        }
        for edge in edges(from)? {
            let next = edge.province_id;
            ensure!(
                edge.movement_cost.is_finite() && edge.movement_cost > 0.0,
                "省份移动消耗无效"
            );
            let weight = (edge.movement_cost * 100_000.0).round() as u64;
            let next_cost = cost.checked_add(weight).context("路线移动消耗超出范围")?;
            if next_cost < *paths.distances.get(&next).unwrap_or(&u64::MAX) {
                paths.distances.insert(next, next_cost);
                paths.parents.insert(next, (from, edge));
                pending.push(Reverse((next_cost, next)));
            }
        }
    }
    Ok(paths)
}

impl Routes {
    fn describe(&self, map: &InstalledMap, origin: u32, target: u32, speed: Option<f64>) -> Value {
        let start = map.position(origin);
        let end = map.position(target);
        let geometry = start.zip(end).map(|(start, end)| start.direction(end));
        let mut data = json!({"status":"graph_shortest_path", "route_basis":"native_adjacency_graph", "reachable":false,
            "origin_province_id":origin, "target_province_id":target, "origin_position":start, "target_position":end,
            "straight_line_map_distance":geometry.as_ref().map(|(distance,_)| distance), "direction":geometry.map(|(_,direction)| direction),
            "distance_unit":"map_units", "hops":null, "route":null, "estimated_days":null});
        let Some(&cost) = self
            .distances
            .get(&target)
            .filter(|_| map.land(origin) && map.land(target))
        else {
            return data;
        };
        let mut segments = Vec::new();
        let mut crossings = BTreeMap::<&str, u32>::new();
        let mut cursor = target;
        let mut route = vec![target];
        while let Some(&(previous, ref edge)) = self.parents.get(&cursor) {
            if edge.crossing != "normal" {
                *crossings.entry(edge.crossing).or_default() += 1;
            }
            segments.push(json!({"from_province_id":previous,"edge":edge}));
            cursor = previous;
            route.push(cursor);
        }
        route.reverse();
        segments.reverse();
        let movement_cost = cost as f64 / 100_000.0;
        let days = speed
            .filter(|speed| speed.is_finite() && *speed > 0.0)
            .map(|speed| (movement_cost / speed).ceil() as u64);
        data["reachable"] = true.into();
        data["total_movement_cost"] = movement_cost.into();
        data["hops"] = json!(segments.len());
        data["route"] = json!(route);
        data["segments"] = json!(segments);
        data["crossings"] = json!(crossings);
        data["estimated_days"] = json!(days);
        data
    }
}
