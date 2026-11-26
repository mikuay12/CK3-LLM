use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use anyhow::Result;
use ck3_llm::world::{
    EntityList, Query, QueryKind, QuerySource, World, live::LiveWorld, scripts::Catalog,
    store::WorldStore,
};
use eframe::egui::{Button, ComboBox, Label, RichText, ScrollArea, TextEdit, Ui};
use serde_json::Value;

use super::style;

#[derive(Clone, Copy, Default, Eq, PartialEq)]
enum Source {
    #[default]
    Snapshot,
    Live,
    Scripts,
    Database,
}

impl Source {
    fn label(self) -> &'static str {
        match self {
            Self::Snapshot => "存档快照",
            Self::Live => "实时游戏",
            Self::Scripts => "安装目录",
            Self::Database => "快照数据库",
        }
    }
}

fn query_label(query: QueryKind) -> &'static str {
    match query {
        QueryKind::Summary => "概览",
        QueryKind::Character => "人物",
        QueryKind::Title => "头衔",
        QueryKind::Province => "省份",
        QueryKind::War => "战争",
        QueryKind::Army => "军队",
        QueryKind::Unit => "移动单位",
        QueryKind::UnitMovement => "行军进度",
        QueryKind::Combat => "交战",
        QueryKind::CombatResult => "战报",
        QueryKind::Siege => "围城",
        QueryKind::Regiment => "兵团",
        QueryKind::ArmyRegiment => "出征兵团",
        QueryKind::MercenaryCompany => "佣兵团",
        QueryKind::HoldingBuildings => "地产建筑",
        QueryKind::Adjacencies => "相邻省份",
        QueryKind::Interaction => "人物互动定义",
        QueryKind::Decision => "决议",
        QueryKind::ActivityType => "活动类型",
        QueryKind::CouncilTaskType => "内阁任务类型",
        QueryKind::CourtPositionType => "宫廷职位类型",
        QueryKind::CourtPositionTask => "宫廷职位任务",
        QueryKind::SchemeCountermeasure => "阴谋反制措施",
        QueryKind::Law => "法律定义",
        QueryKind::MenAtArmsType => "常备军兵种",
        QueryKind::NotificationType => "通知类型",
        QueryKind::EventDefinition => "事件定义",
        QueryKind::Event => "待处理事件",
        QueryKind::IncomingInteraction => "待回复互动",
        QueryKind::ActivityInvitation => "活动邀请",
        QueryKind::Activity => "活动",
        QueryKind::ObservedCommands => "历史命令",
        QueryKind::Realm => "领地与封臣",
        QueryKind::Titles => "头衔目录",
        QueryKind::Realms => "独立领地",
        QueryKind::CouncilTask => "内阁任务",
        QueryKind::Faction => "派系",
        QueryKind::Scheme => "阴谋",
        QueryKind::CourtPosition => "宫廷职位",
        QueryKind::HolyOrder => "军事修会",
        QueryKind::Secret => "秘密",
        QueryKind::TaskContract => "任务委托",
        QueryKind::Contract => "封臣契约",
        QueryKind::Alerts => "局势提示",
        QueryKind::PlayerMessages => "人物消息",
        QueryKind::MessageFeed => "游戏窗口消息",
        QueryKind::PlayerSuccessions => "玩家继承状态",
        QueryKind::ProvinceMap => "省份地图",
        QueryKind::Route => "陆路路径",
        QueryKind::Neighbors => "相邻领地",
        QueryKind::Borders => "领地边界",
        QueryKind::WarTheater => "战争态势",
        QueryKind::BuildingType => "建筑类型",
        QueryKind::ConstructionQuote => "建设报价",
        QueryKind::CasusBelli => "宣战理由",
        QueryKind::DeclarationPreview => "宣战预览",
        QueryKind::Relations => "人物关系",
        QueryKind::Opinions => "好感修正",
    }
}

enum Output {
    Opened {
        file: PathBuf,
        world: Arc<World>,
        summary: Value,
    },
    Connected(Arc<LiveWorld>),
    Scripts(Arc<Catalog>),
    Database(PathBuf, Arc<WorldStore>, Value),
    Result {
        source: Source,
        value: Value,
    },
}

#[derive(Default)]
pub struct WorldPage {
    opened: Option<(PathBuf, Arc<World>)>,
    live: Option<Arc<LiveWorld>>,
    scripts: Option<Arc<Catalog>>,
    store: Option<(PathBuf, Arc<WorldStore>)>,
    source: Source,
    job: Option<Receiver<Result<Output>>>,
    query: QueryKind,
    key: String,
    character: String,
    script_list: bool,
    offset: usize,
    output: String,
    error: Option<String>,
}

impl WorldPage {
    pub fn active(&self) -> Option<Arc<dyn QuerySource>> {
        match self.source {
            Source::Snapshot => self.opened.as_ref().map(|(_, world)| world.clone() as _),
            Source::Live => self.live.as_ref().map(|live| live.clone() as _),
            Source::Scripts => self.scripts.as_ref().map(|scripts| scripts.clone() as _),
            Source::Database => self.store.as_ref().map(|(_, store)| store.clone() as _),
        }
    }

    pub fn source_name(&self) -> &'static str {
        if self.active().is_none() {
            return "游戏世界";
        }
        self.source.label()
    }

    pub fn has_live_game(&self) -> bool {
        self.live.is_some()
    }

    pub fn live(&self) -> Option<&Arc<LiveWorld>> {
        self.live.as_ref()
    }

    pub fn open(&mut self, file: PathBuf) {
        if self.busy() {
            self.error = Some("世界快照正在读取".into());
            return;
        }
        self.start(move || {
            let world = Arc::new(World::open(&file)?);
            let summary = world.summary()?;
            Ok(Output::Opened {
                file,
                world,
                summary,
            })
        });
    }

    pub fn busy(&self) -> bool {
        self.job.is_some()
    }

    pub fn open_store(&mut self, path: PathBuf) {
        if self.busy() {
            self.error = Some("请等待当前查询结束".into());
            return;
        }
        self.start(move || {
            let store = Arc::new(WorldStore::open(&path)?);
            let summary = store.query(Query::Summary)?;
            Ok(Output::Database(path, store, summary))
        });
    }

    pub fn poll(&mut self) {
        if self.live.as_ref().is_some_and(|live| !live.is_running()) {
            self.disconnect_live();
            self.error = Some("实时 CK3 进程已退出".into());
        }
        let Some(job) = &self.job else { return };
        let result = match job.try_recv() {
            Ok(value) => value,
            Err(TryRecvError::Empty) => return,
            Err(TryRecvError::Disconnected) => Err(anyhow::anyhow!("世界读取线程已退出")),
        };
        self.job = None;
        let value = match result {
            Ok(Output::Opened {
                file,
                world,
                summary,
            }) => {
                self.opened = Some((file, world));
                self.select(Source::Snapshot);
                summary
            }
            Ok(Output::Connected(world)) => {
                self.live = Some(world);
                self.select(Source::Live);
                return;
            }
            Ok(Output::Scripts(catalog)) => {
                self.scripts = Some(catalog);
                self.select(Source::Scripts);
                return;
            }
            Ok(Output::Database(path, store, summary)) => {
                self.store = Some((path, store));
                self.select(Source::Database);
                summary
            }
            Ok(Output::Result { source, value }) => {
                if source != self.source || self.active().is_none() {
                    return;
                }
                value
            }
            Err(error) => {
                self.error = Some(format!("{error:#}"));
                return;
            }
        };
        match serde_json::to_string_pretty(&value) {
            Ok(text) => self.output = text,
            Err(error) => self.error = Some(error.to_string()),
        }
    }

    pub fn show(&mut self, ui: &mut Ui, host_pid: Option<u32>, game_dir: &Path) {
        style::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(!self.busy(), style::primary("打开存档或快照"))
                    .clicked()
                    && let Some(file) = rfd::FileDialog::new()
                        .add_filter("CK3 快照", &["ck3", "sav", "zip", "bin"])
                        .pick_file()
                {
                    self.open(file);
                }
                if ui
                    .add_enabled(!self.busy(), Button::new("打开快照数据库"))
                    .clicked()
                    && let Some(path) = rfd::FileDialog::new()
                        .add_filter("SQLite", &["sqlite", "sqlite3", "db"])
                        .pick_file()
                {
                    self.open_store(path);
                }
                if !self.busy()
                    && self.source == Source::Snapshot
                    && let Some((source, world)) = self.opened.clone()
                    && ui.button("保存快照到数据库").clicked()
                    && let Some(path) = rfd::FileDialog::new()
                        .add_filter("SQLite", &["sqlite"])
                        .set_file_name("world.sqlite")
                        .save_file()
                {
                    self.start(move || {
                        let store = Arc::new(WorldStore::save(&path, &world, &source)?);
                        let summary = store.query(Query::Summary)?;
                        Ok(Output::Database(path, store, summary))
                    });
                }
                if self.live.is_some() {
                    if ui
                        .add_enabled(!self.busy(), Button::new("断开实时查询"))
                        .clicked()
                    {
                        self.disconnect_live();
                    }
                } else if ui
                    .add_enabled(!self.busy(), Button::new("连接运行中的 CK3"))
                    .clicked()
                {
                    self.start(move || {
                        Ok(Output::Connected(Arc::new(LiveWorld::connect(host_pid)?)))
                    });
                }
                if ui
                    .add_enabled(!self.busy(), Button::new("读取游戏目录"))
                    .clicked()
                {
                    let game_dir = game_dir.to_owned();
                    self.start(move || Ok(Output::Scripts(Arc::new(Catalog::open(&game_dir)?))));
                }
            });
            let sources = [
                (Source::Snapshot, self.opened.is_some()),
                (Source::Live, self.live.is_some()),
                (Source::Scripts, self.scripts.is_some()),
                (Source::Database, self.store.is_some()),
            ];
            if sources.iter().filter(|(_, available)| *available).count() > 1 {
                let mut source = self.source;
                ui.add_enabled_ui(!self.busy(), |ui| {
                    ui.horizontal(|ui| {
                        for (option, available) in sources {
                            if available {
                                ui.selectable_value(&mut source, option, option.label());
                            }
                        }
                    });
                });
                if source != self.source {
                    self.select(source);
                }
            }
            if self.source == Source::Snapshot
                && let Some((file, _)) = &self.opened
            {
                ui.label(RichText::new(file.display().to_string()).color(style::MUTED));
            }
            if self.source == Source::Database
                && let Some((file, _)) = &self.store
            {
                ui.label(RichText::new(file.display().to_string()).color(style::MUTED));
            }
            if self.source == Source::Live
                && let Some(live) = &self.live
            {
                ui.label(
                    RichText::new(format!("实时游戏 · PID {}", live.pid())).color(style::MUTED),
                );
            }
            if let Some(world) = self.active() {
                ui.horizontal_wrapped(|ui| {
                    let previous_query = self.query;
                    ComboBox::from_id_salt("world-query")
                        .selected_text(query_label(self.query))
                        .show_ui(ui, |ui| {
                            for &query in world.kinds() {
                                ui.selectable_value(&mut self.query, query, query_label(query));
                            }
                        });
                    if previous_query != self.query {
                        self.key.clear();
                        self.offset = 0;
                    }
                    if self.query.is_script()
                        && ui.checkbox(&mut self.script_list, "搜索目录").changed()
                    {
                        self.key.clear();
                        self.offset = 0;
                    }
                    if self.query != QueryKind::Summary {
                        let hint = if self.query == QueryKind::ObservedCommands {
                            "命令名称，可留空"
                        } else if self.query.is_script() && self.script_list {
                            "关键词，可留空"
                        } else if self.query.is_script() {
                            "脚本键"
                        } else if self.query == QueryKind::Route {
                            "起点省份 ID, 终点省份 ID"
                        } else if self.query == QueryKind::ConstructionQuote {
                            "省份 ID, 建筑键, 可选槽位"
                        } else if self.query == QueryKind::DeclarationPreview {
                            "目标人物 ID, 宣战理由键"
                        } else if self.query == QueryKind::BuildingType {
                            "建筑键或 ID，留空列出"
                        } else if matches!(
                            self.query,
                            QueryKind::Title
                                | QueryKind::Realm
                                | QueryKind::Neighbors
                                | QueryKind::Borders
                        ) {
                            "头衔 ID 或脚本键"
                        } else if matches!(self.query, QueryKind::Titles | QueryKind::Realms) {
                            "搜索脚本键"
                        } else if self.query.is_collection() {
                            "留空列出记录"
                        } else {
                            "数字 ID"
                        };
                        if ui
                            .add(
                                TextEdit::singleline(&mut self.key)
                                    .desired_width(170.0)
                                    .hint_text(hint),
                            )
                            .changed()
                        {
                            self.offset = 0;
                        }
                    }
                    if self.script_list_for_query()
                        || self.query == QueryKind::ObservedCommands
                        || self.query.is_collection()
                        || self.query == QueryKind::BuildingType && self.key.is_empty()
                    {
                        ui.add(
                            eframe::egui::DragValue::new(&mut self.offset)
                                .speed(20)
                                .prefix("跳过 ")
                                .suffix(" 条"),
                        );
                    }
                    if self.source == Source::Live
                        && self.query.uses_character()
                        && !self.script_list_for_query()
                    {
                        ui.add(
                            TextEdit::singleline(&mut self.character)
                                .desired_width(120.0)
                                .hint_text("角色 ID（可选）"),
                        );
                    }
                    if ui
                        .add_enabled(
                            !self.busy()
                                && (self.query == QueryKind::Summary
                                    || self.script_list_for_query()
                                    || self.query == QueryKind::ObservedCommands
                                    || self.query.is_collection()
                                    || self.query == QueryKind::BuildingType
                                    || !self.key.trim().is_empty()),
                            Button::new("查询"),
                        )
                        .clicked()
                    {
                        let kind = self.query;
                        let key = self.key.trim().to_owned();
                        let source = self.source;
                        let list = self.script_list_for_query();
                        let offset = self.offset;
                        let character = self.character.trim().to_owned();
                        self.output.clear();
                        self.start(move || {
                            let character = if source == Source::Live
                                && kind.uses_character()
                                && !character.is_empty()
                            {
                                Some(character.parse()?)
                            } else {
                                None
                            };
                            let request = if kind == QueryKind::ObservedCommands {
                                Query::ObservedCommands {
                                    name: &key,
                                    offset,
                                    limit: 20,
                                }
                            } else if kind == QueryKind::BuildingType && key.is_empty() {
                                Query::BuildingTypes {
                                    search: "",
                                    offset,
                                    limit: 20,
                                }
                            } else if list {
                                Query::ScriptList {
                                    kind,
                                    search: &key,
                                    group: None,
                                    offset,
                                    limit: 20,
                                }
                            } else if kind.is_collection()
                                && (key.is_empty()
                                    || !kind.is_entity() && kind != QueryKind::Activity)
                            {
                                Query::Entities(EntityList {
                                    kind,
                                    character,
                                    search: &key,
                                    offset,
                                    limit: 20,
                                    include_inactive: false,
                                })
                            } else {
                                kind.request_for(&key, character)?
                            };
                            Ok(Output::Result {
                                source,
                                value: world.query(request)?,
                            })
                        });
                    }
                });
            }
            if self.busy() {
                ui.spinner();
            }
            if let Some(error) = &self.error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
        });
        if !self.output.is_empty() {
            ui.add_space(10.0);
            style::card(ui, |ui| {
                if ui.button("复制结果").clicked() {
                    ui.ctx().copy_text(self.output.clone());
                }
                ScrollArea::vertical()
                    .id_salt("world-query-results")
                    .max_height(440.0)
                    .show(ui, |ui| {
                        ui.add(
                            Label::new(RichText::new(&self.output).monospace())
                                .selectable(true)
                                .wrap(),
                        );
                    });
            });
        }
    }

    fn select(&mut self, source: Source) {
        self.source = source;
        self.query = if source == Source::Scripts {
            QueryKind::Interaction
        } else {
            QueryKind::Summary
        };
        self.script_list = source == Source::Scripts;
        self.key.clear();
        self.offset = 0;
        self.output.clear();
        self.error = None;
    }

    fn disconnect_live(&mut self) {
        self.live = None;
        if self.source == Source::Live {
            self.select(if self.opened.is_none() && self.store.is_some() {
                Source::Database
            } else if self.opened.is_none() && self.scripts.is_some() {
                Source::Scripts
            } else {
                Source::Snapshot
            });
        }
    }

    fn script_list_for_query(&self) -> bool {
        self.query.is_script() && self.script_list
    }

    fn start(&mut self, job: impl FnOnce() -> Result<Output> + Send + 'static) {
        self.error = None;
        let (send, result) = mpsc::channel();
        match thread::Builder::new()
            .name("world-reader".into())
            .spawn(move || {
                let _ = send.send(job());
            }) {
            Ok(_) => self.job = Some(result),
            Err(error) => self.error = Some(error.to_string()),
        }
    }
}
