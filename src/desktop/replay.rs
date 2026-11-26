use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use anyhow::Result;
use ck3_llm::session::replay::Replay;
use ck3_llm::settings::Settings;
use eframe::egui::{Label, RichText, ScrollArea, TextEdit, Ui};

use super::style;

#[derive(Default)]
pub struct ReplayPage {
    loaded: Option<Arc<Replay>>,
    job: Option<Receiver<Result<Output>>>,
    database: Option<PathBuf>,
    query: String,
    matches: Vec<usize>,
    selected: Option<usize>,
    detail: String,
    error: Option<String>,
}

enum Output {
    Opened(Arc<Replay>),
    Indexed(PathBuf),
}

pub enum Selection {
    Snapshot(PathBuf),
    Database(PathBuf),
}

impl ReplayPage {
    pub fn busy(&self) -> bool {
        self.job.is_some()
    }

    pub fn poll(&mut self) {
        let Some(job) = &self.job else { return };
        let result = match job.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return,
            Err(TryRecvError::Disconnected) => Err(anyhow::anyhow!("抓包回放线程已退出")),
        };
        self.job = None;
        match result {
            Ok(Output::Opened(replay)) => {
                self.loaded = Some(replay);
                self.database = None;
                self.filter();
            }
            Ok(Output::Indexed(path)) => self.database = Some(path),
            Err(error) => self.error = Some(format!("{error:#}")),
        }
    }

    fn filter(&mut self) {
        let Some(replay) = &self.loaded else { return };
        let query = self.query.trim().to_ascii_lowercase();
        self.matches = replay
            .records
            .iter()
            .enumerate()
            .filter(|(_, record)| record.name.to_ascii_lowercase().contains(&query))
            .map(|(index, _)| index)
            .collect();
        self.selected = None;
        self.detail.clear();
    }

    fn open(&mut self, path: PathBuf, output: PathBuf) {
        self.start(move || Ok(Output::Opened(Arc::new(Replay::open(&path, &output)?))));
    }

    fn start(&mut self, work: impl FnOnce() -> Result<Output> + Send + 'static) {
        let (send, result) = mpsc::channel();
        self.error = None;
        match thread::Builder::new()
            .name("capture-replay".into())
            .spawn(move || {
                let _ = send.send(work());
            }) {
            Ok(_) => self.job = Some(result),
            Err(error) => self.error = Some(error.to_string()),
        }
    }

    pub fn show(&mut self, ui: &mut Ui) -> Option<Selection> {
        let mut selection = None;
        let mut index = None;
        style::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(!self.busy(), style::primary("打开抓包日志"))
                    .clicked()
                {
                    match Settings::path() {
                        Ok(path) => {
                            let output = path.with_file_name("captures");
                            if let Some(log) = rfd::FileDialog::new()
                                .add_filter("抓包事件日志", &["jsonl"])
                                .set_directory(&output)
                                .pick_file()
                            {
                                self.open(log, output);
                            }
                        }
                        Err(error) => self.error = Some(format!("{error:#}")),
                    }
                }
                if self.busy() {
                    ui.spinner();
                }
                if let Some(path) = &self.database
                    && ui.button("查询已保存的回放数据库").clicked()
                {
                    selection = Some(Selection::Database(path.clone()));
                }
            });
            if let Some(error) = &self.error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
            if let Some(replay) = &self.loaded {
                let summary = &replay.summary;
                let commands: u64 = summary.commands.values().sum();
                ui.label(format!(
                    "{} 个接收包 · {commands} 条命令 · {} 个已校验文件",
                    summary.frames, summary.verified_files
                ));
                if summary.decode_errors != 0 || summary.incomplete_transfers != 0 {
                    ui.collapsing(
                        format!(
                            "{} 处解析错误 · {} 个传输未完成",
                            summary.decode_errors, summary.incomplete_transfers
                        ),
                        |ui| {
                            for error in &replay.errors {
                                ui.label(error);
                            }
                        },
                    );
                }
                ui.label(RichText::new(replay.output.display().to_string()).color(style::MUTED));
                for file in &replay.files {
                    ui.horizontal_wrapped(|ui| {
                        ui.label(format!("{} · {} 字节", file.name, file.size));
                        if file.snapshot && ui.button("查询快照").clicked() {
                            selection = Some(Selection::Snapshot(file.path.clone()));
                        }
                        if file.snapshot
                            && ui
                                .add_enabled(
                                    !self.busy(),
                                    eframe::egui::Button::new("保存快照与命令到数据库"),
                                )
                                .clicked()
                            && let Some(path) = rfd::FileDialog::new()
                                .add_filter("SQLite", &["sqlite"])
                                .set_file_name("replay.sqlite")
                                .save_file()
                        {
                            index = Some((replay.clone(), file.path.clone(), path));
                        }
                    });
                }
                if summary.synchronization.hotjoin.is_some() {
                    ui.collapsing("同步记录", |ui| {
                        ui.monospace(
                            serde_json::to_string_pretty(&summary.synchronization)
                                .unwrap_or_default(),
                        );
                    });
                }
            }
        });
        if let Some((replay, snapshot, destination)) = index {
            self.start(move || {
                replay.save_database(&snapshot, &destination)?;
                Ok(Output::Indexed(destination))
            });
        }
        if self.loaded.is_none() {
            return selection;
        }
        ui.add_space(10.0);
        style::card(ui, |ui| {
            if ui
                .add(TextEdit::singleline(&mut self.query).hint_text("搜索命令名称"))
                .changed()
            {
                self.filter();
            }
            let replay = self.loaded.as_ref().expect("opened replay");
            ScrollArea::vertical()
                .id_salt("replay-records")
                .max_height(280.0)
                .show_rows(ui, 23.0, self.matches.len(), |ui, rows| {
                    for row in rows {
                        let index = self.matches[row];
                        let record = &replay.records[index];
                        if ui
                            .selectable_label(
                                self.selected == Some(index),
                                format!("{} · {}", record.sequence, record.name),
                            )
                            .clicked()
                        {
                            self.selected = Some(index);
                            self.detail = match record
                                .describe()
                                .and_then(|value| Ok(serde_json::to_string_pretty(&value)?))
                            {
                                Ok(value) => value,
                                Err(error) => format!("读取消息详情失败 · {error:#}"),
                            };
                        }
                    }
                });
            if !self.detail.is_empty() {
                if ui.button("复制消息").clicked() {
                    ui.ctx().copy_text(self.detail.clone());
                }
                ScrollArea::vertical()
                    .id_salt("replay-detail")
                    .max_height(340.0)
                    .show(ui, |ui| {
                        ui.add(
                            Label::new(RichText::new(&self.detail).monospace())
                                .selectable(true)
                                .wrap(),
                        );
                    });
            }
        });
        selection
    }
}
