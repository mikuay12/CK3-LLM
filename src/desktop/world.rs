use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use anyhow::Result;
use ck3_llm::world::{QueryKind, QuerySource, World, live::LiveWorld};
use eframe::egui::{Button, ComboBox, Label, RichText, ScrollArea, TextEdit, Ui};
use serde_json::Value;

use super::style;

#[derive(Clone, Copy, Default, Eq, PartialEq)]
enum Source {
    #[default]
    Snapshot,
    Live,
}

impl Source {
    fn label(self) -> &'static str {
        match self {
            Self::Snapshot => "存档快照",
            Self::Live => "实时游戏",
        }
    }
}

fn query_label(query: QueryKind) -> &'static str {
    match query {
        QueryKind::Summary => "概览",
        QueryKind::Character => "人物",
        QueryKind::Title => "头衔",
        QueryKind::Province => "省份",
    }
}

enum Output {
    Opened {
        file: PathBuf,
        world: Arc<World>,
        summary: Value,
    },
    Connected(Arc<LiveWorld>),
    Result {
        source: Source,
        value: Value,
    },
}

#[derive(Default)]
pub struct WorldPage {
    opened: Option<(PathBuf, Arc<World>)>,
    live: Option<Arc<LiveWorld>>,
    source: Source,
    job: Option<Receiver<Result<Output>>>,
    query: QueryKind,
    key: String,
    output: String,
    error: Option<String>,
}

impl WorldPage {
    pub fn active(&self) -> Option<Arc<dyn QuerySource>> {
        match self.source {
            Source::Snapshot => self.opened.as_ref().map(|(_, world)| world.clone() as _),
            Source::Live => self.live.as_ref().map(|live| live.clone() as _),
        }
    }

    pub fn source_name(&self) -> &'static str {
        if self.opened.is_none() && self.live.is_none() {
            return "游戏世界";
        }
        self.source.label()
    }

    pub fn has_live_game(&self) -> bool {
        self.live.is_some()
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

    pub fn show(&mut self, ui: &mut Ui, host_pid: Option<u32>) {
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
            });
            if self.opened.is_some() && self.live.is_some() {
                let mut source = self.source;
                ui.add_enabled_ui(!self.busy(), |ui| {
                    ui.horizontal(|ui| {
                        for option in [Source::Snapshot, Source::Live] {
                            ui.selectable_value(&mut source, option, option.label());
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
            if self.source == Source::Live
                && let Some(live) = &self.live
            {
                ui.label(
                    RichText::new(format!("实时游戏 · PID {}", live.pid())).color(style::MUTED),
                );
            }
            if let Some(world) = self.active() {
                ui.horizontal_wrapped(|ui| {
                    ComboBox::from_id_salt("world-query")
                        .selected_text(query_label(self.query))
                        .show_ui(ui, |ui| {
                            for query in [
                                QueryKind::Summary,
                                QueryKind::Character,
                                QueryKind::Title,
                                QueryKind::Province,
                            ] {
                                ui.selectable_value(&mut self.query, query, query_label(query));
                            }
                        });
                    if self.query != QueryKind::Summary {
                        let hint = if self.query == QueryKind::Title {
                            "头衔 ID 或脚本键"
                        } else {
                            "数字 ID"
                        };
                        ui.add(
                            TextEdit::singleline(&mut self.key)
                                .desired_width(170.0)
                                .hint_text(hint),
                        );
                    }
                    if ui
                        .add_enabled(
                            !self.busy()
                                && (self.query == QueryKind::Summary
                                    || !self.key.trim().is_empty()),
                            Button::new("查询"),
                        )
                        .clicked()
                    {
                        let kind = self.query;
                        let key = self.key.trim().to_owned();
                        let source = self.source;
                        self.output.clear();
                        self.start(move || {
                            Ok(Output::Result {
                                source,
                                value: world.query(kind.request(&key)?)?,
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
        self.query = QueryKind::Summary;
        self.output.clear();
        self.error = None;
    }

    fn disconnect_live(&mut self) {
        self.live = None;
        if self.source == Source::Live {
            self.select(Source::Snapshot);
        }
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
