//! Read-only views over the supported local CK3 executable.

#[cfg(any(test, all(windows, target_arch = "x86_64")))]
mod objects;
#[cfg(all(windows, target_arch = "x86_64"))]
mod process;

use anyhow::Result;
use serde_json::Value;

#[cfg(all(windows, target_arch = "x86_64"))]
use super::scripts::Catalog;
use super::scripts::InteractionOptions;
use super::{Query, QueryKind, QuerySource};
use crate::notifications::watch::{Military, Sample, Target};
use std::collections::BTreeSet;

#[cfg(all(windows, target_arch = "x86_64"))]
use std::sync::Mutex;

pub struct LiveWorld {
    #[cfg(all(windows, target_arch = "x86_64"))]
    process: process::Process,
    #[cfg(all(windows, target_arch = "x86_64"))]
    catalogs: Mutex<objects::Catalogs>,
    #[cfg(all(windows, target_arch = "x86_64"))]
    scripts: Result<Catalog>,
}

#[derive(Clone, serde::Serialize)]
pub(crate) struct PlayerContext {
    pub character: u32,
    pub name: String,
    pub date: String,
    pub tick: u32,
    pub generation: u64,
}

impl PlayerContext {
    pub fn same_character(&self, other: &Self) -> bool {
        (self.generation, self.character) == (other.generation, other.character)
    }
}

impl LiveWorld {
    pub fn connect(pid: Option<u32>) -> Result<Self> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        {
            let process = process::Process::open(pid)?;
            Ok(Self {
                scripts: Catalog::open(&process.game_dir),
                process,
                catalogs: Mutex::default(),
            })
        }
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = pid;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }

    pub fn is_running(&self) -> bool {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.process.is_running();
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        false
    }

    pub fn pid(&self) -> u32 {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.process.pid;
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        0
    }

    pub(crate) fn sample(&self, target: Target) -> Result<Sample> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.read(|catalogs| catalogs.sample(&self.process, self.process.base, target));
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = target;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }

    pub(crate) fn military(
        &self,
        character: u32,
        generation: u64,
        seen: &BTreeSet<u32>,
    ) -> Result<Military> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.read(|catalogs| {
            catalogs.military(
                &self.process,
                self.process.base,
                character,
                generation,
                seen,
            )
        });
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = (character, generation, seen);
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }

    pub(crate) fn message_sample(&self, character: u32, generation: u64) -> Result<Vec<Value>> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.read(|catalogs| {
            catalogs.message_sample(&self.process, self.process.base, character, generation)
        });
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = (character, generation);
            anyhow::bail!("实时通知需要 Windows x64")
        }
    }

    pub(crate) fn player_context(&self, machine: u32) -> Result<PlayerContext> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self
            .read(|catalogs| catalogs.player_context(&self.process, self.process.base, machine));
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = machine;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }

    pub(crate) fn interaction_options(&self, name: &str) -> Result<InteractionOptions> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self.scripts()?.interaction_options(name);
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = name;
            anyhow::bail!("人物互动需要 Windows x64")
        }
    }

    pub(crate) fn acceptance(&self, player: &PlayerContext, data: &Value) -> Result<(f64, f64)> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        return self
            .read(|catalogs| catalogs.acceptance(&self.process, self.process.base, player, data));
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = (player, data);
            anyhow::bail!("原生规则求值需要 Windows x64")
        }
    }

    #[cfg(all(windows, target_arch = "x86_64"))]
    fn scripts(&self) -> Result<&Catalog> {
        self.scripts
            .as_ref()
            .map_err(|error| anyhow::anyhow!("{error:#}"))
    }

    #[cfg(all(windows, target_arch = "x86_64"))]
    fn read<T>(&self, query: impl FnOnce(&mut objects::Catalogs) -> Result<T>) -> Result<T> {
        anyhow::ensure!(self.is_running(), "实时 CK3 进程已退出");
        let mut catalogs = self
            .catalogs
            .lock()
            .map_err(|_| anyhow::anyhow!("实时世界读取中断，请重新连接"))?;
        query(&mut catalogs)
    }
}

impl QuerySource for LiveWorld {
    fn kinds(&self) -> &'static [QueryKind] {
        QueryKind::LIVE
    }

    fn query(&self, request: Query<'_>) -> Result<Value> {
        #[cfg(all(windows, target_arch = "x86_64"))]
        {
            if let Query::Navigation(navigation) = request {
                let map = self.scripts()?.map()?;
                return self.read(|catalogs| {
                    catalogs.navigation(&self.process, self.process.base, map, navigation)
                });
            }
            if let Query::ProvinceMap(id) = request {
                return self.scripts()?.map()?.province(id);
            }
            if matches!(request, Query::Script { .. } | Query::ScriptList { .. }) {
                let mut data = self.scripts()?.query(request)?;
                if let Query::Script {
                    kind: kind @ (QueryKind::Decision | QueryKind::ActivityType),
                    id,
                    character: Some(character),
                } = request
                {
                    data["character_id"] = character.into();
                    let result = self.read(|catalogs| {
                        catalogs.evaluate_script(
                            &self.process,
                            self.process.base,
                            kind,
                            id,
                            character,
                        )
                    });
                    match result {
                        Ok(Value::Object(mut value)) => {
                            if let Some(source) = value.remove("source") {
                                data["availability"] = source;
                            }
                            if let Some(data) = data.as_object_mut() {
                                data.extend(value);
                            }
                        }
                        Ok(_) => unreachable!("native planning result is an object"),
                        Err(error) => data["native_error"] = format!("{error:#}").into(),
                    }
                }
                if matches!(
                    request,
                    Query::Script {
                        kind: QueryKind::MenAtArmsType,
                        ..
                    } | Query::ScriptList {
                        kind: QueryKind::MenAtArmsType,
                        ..
                    }
                ) && let Ok(indices) = self
                    .read(|catalogs| catalogs.men_at_arms_indices(&self.process, self.process.base))
                {
                    let annotate = |value: &mut Value| {
                        if let Some(index) = value["id"]
                            .as_str()
                            .and_then(|key| indices.get(key))
                            .copied()
                        {
                            value["native_index"] = index.into();
                        }
                    };
                    if let Some(items) = data["items"].as_array_mut() {
                        for item in items {
                            annotate(item);
                        }
                    } else {
                        annotate(&mut data);
                    }
                }
                return Ok(data);
            }
            let mut result =
                self.read(|catalogs| catalogs.query(&self.process, self.process.base, request))?;
            if let Query::Province(id) | Query::ProvinceSupply { province: id, .. } = request
                && let Ok(map) = self.scripts().and_then(Catalog::map)
            {
                result["data"]["terrain"] = map.terrain(id).into();
            }
            if let Query::Pending { kind, .. } = request
                && let Ok(scripts) = self.scripts()
            {
                let data = &mut result["data"];
                match kind {
                    QueryKind::Event => {
                        if let Some(key) = data["event"].as_str()
                            && let Ok(definition) =
                                scripts.query(QueryKind::EventDefinition.request(key)?)
                        {
                            let context = data["scope"].clone();
                            if let Some(options) = data["options"].as_array_mut() {
                                for option in options {
                                    let index =
                                        option["index"].as_u64().unwrap_or(u64::MAX) as usize;
                                    if let Some(script) = definition["options"].get(index) {
                                        option["label"] = script["label"].clone();
                                        scripts.render_localization(&mut option["label"], &context);
                                        option["conditional"] = script["conditional"].clone();
                                        option["effect_summary"] = script["effect_summary"].clone();
                                        option["script"] = script["script"].clone();
                                    }
                                }
                            }
                            data["title"] = definition["title"].clone();
                            data["description"] = definition["description"].clone();
                            for key in ["title", "description"] {
                                scripts.render_localization(&mut data[key], &context);
                            }
                        }
                    }
                    QueryKind::Activity | QueryKind::ActivityInvitation => {
                        if let Some(key) = data["activity"]["key"].as_str() {
                            data["activity"]["label"] = scripts.text(key).into();
                        }
                    }
                    QueryKind::IncomingInteraction => {
                        if let Some(key) = data["interaction"]["key"].as_str() {
                            data["interaction"]["label"] = scripts.text(key).into();
                        }
                    }
                    _ => {}
                }
            }
            Ok(result)
        }
        #[cfg(not(all(windows, target_arch = "x86_64")))]
        {
            let _ = request;
            anyhow::bail!("实时世界查询需要 Windows x64");
        }
    }
}
