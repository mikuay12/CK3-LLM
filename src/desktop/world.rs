use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use anyhow::{Context, Result};
use ck3_llm::world::World;
use eframe::egui::{Button, ComboBox, Label, RichText, ScrollArea, TextEdit, Ui};
use serde_json::Value;

use super::style;

#[derive(Clone, Copy, Default, Eq, PartialEq)]
enum Query {
    #[default]
    Summary,
    Character,
    Title,
    Province,
}

impl Query {
    fn label(self) -> &'static str {
        match self {
            Self::Summary => "概览",
            Self::Character => "人物",
            Self::Title => "头衔",
            Self::Province => "省份",
        }
    }

    fn execute(self, world: &World, key: &str) -> Result<Value> {
        match self {
            Self::Summary => world.summary(),
            Self::Character => world.character(key.parse().context("人物 ID 需要是整数")?),
            Self::Title => world.title(key),
            Self::Province => world.province(key.parse().context("省份 ID 需要是整数")?),
        }
    }
}

enum Output {
    Opened {
        file: PathBuf,
        world: Arc<World>,
        summary: Value,
    },
    Result(Value),
}

#[derive(Default)]
pub struct WorldPage {
    opened: Option<(PathBuf, Arc<World>)>,
    job: Option<Receiver<Result<Output>>>,
    query: Query,
    key: String,
    output: String,
    error: Option<String>,
}

impl WorldPage {
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
                self.query = Query::Summary;
                summary
            }
            Ok(Output::Result(value)) => value,
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

    pub fn show(&mut self, ui: &mut Ui) {
        style::card(ui, |ui| {
            if ui
                .add_enabled(!self.busy(), style::primary("打开存档或快照"))
                .clicked()
                && let Some(file) = rfd::FileDialog::new()
                    .add_filter("CK3 快照", &["ck3", "sav", "zip", "bin"])
                    .pick_file()
            {
                self.open(file);
            }
            if let Some((file, world)) = &self.opened {
                let world = Arc::clone(world);
                ui.label(RichText::new(file.display().to_string()).color(style::MUTED));
                ui.horizontal_wrapped(|ui| {
                    ComboBox::from_id_salt("world-query")
                        .selected_text(self.query.label())
                        .show_ui(ui, |ui| {
                            for query in [
                                Query::Summary,
                                Query::Character,
                                Query::Title,
                                Query::Province,
                            ] {
                                ui.selectable_value(&mut self.query, query, query.label());
                            }
                        });
                    if self.query != Query::Summary {
                        let hint = if self.query == Query::Title {
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
                                && (self.query == Query::Summary || !self.key.trim().is_empty()),
                            Button::new("查询"),
                        )
                        .clicked()
                    {
                        let query = self.query;
                        let key = self.key.trim().to_owned();
                        self.start(move || Ok(Output::Result(query.execute(&world, &key)?)));
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
