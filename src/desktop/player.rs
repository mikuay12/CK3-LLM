use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::TryRecvError;

use ck3_llm::model::player::{Session, Task, Update};
use ck3_llm::settings::ModelSettings;
use ck3_llm::world::live::LiveWorld;
use eframe::egui::{Button, RichText, ScrollArea, TextEdit, Ui};

use super::{memory::MemoryPage, notifications::NotificationsPage, style};

#[derive(Default)]
pub struct PlayerPage {
    task: Option<Task>,
    target: String,
    guidance: String,
    status: String,
    history: VecDeque<String>,
    error: Option<String>,
    transcript: Option<PathBuf>,
}

impl PlayerPage {
    pub fn busy(&self) -> bool {
        self.task.is_some()
    }

    pub fn poll(
        &mut self,
        live: Option<&Arc<LiveWorld>>,
        session: Option<&Session>,
        notifications: &mut NotificationsPage,
        memory: &mut MemoryPage,
    ) {
        while let Some(task) = &self.task {
            match task.try_recv() {
                Ok(Update::WaitingForPlayer(reason)) => self.status = reason,
                Ok(Update::Playing { character, date }) => {
                    self.status = format!("正在控制人物 {character} · {date}");
                    self.error = None;
                }
                Ok(Update::Reply { content, tool }) => {
                    self.error = None;
                    self.status = tool
                        .as_deref()
                        .map_or_else(|| "模型已回复".into(), |tool| format!("正在处理 {tool}"));
                    if let Some(content) = content.filter(|text| !text.trim().is_empty()) {
                        self.record(format!("模型  {content}"));
                    }
                }
                Ok(Update::Waiting(duration)) => {
                    self.status = format!("等待游戏更新 · 最多 {:.1} 秒", duration.as_secs_f64())
                }
                Ok(Update::Retry { delay, error }) => {
                    self.status = format!("将在 {} 秒后重试", delay.as_secs());
                    self.error = Some(error);
                }
                Ok(Update::Notices(notices)) => notifications.record_live(notices),
                Ok(Update::MemoryChanged) => memory.on_external_update(),
                Ok(Update::Ended(result)) => {
                    self.task = None;
                    self.status = "模型游玩已结束".into();
                    self.error = result.err().map(|error| format!("{error:#}"));
                }
                Err(TryRecvError::Empty) => {
                    if !live
                        .zip(session)
                        .is_some_and(|(world, session)| task.applies_to(world, &session.actions))
                    {
                        self.task = None;
                        self.status = "游戏房间或实时查询已断开".into();
                    }
                    break;
                }
                Err(TryRecvError::Disconnected) => {
                    self.task = None;
                    self.error = Some("模型游玩线程已退出".into());
                }
            }
        }
    }

    pub fn show(
        &mut self,
        ui: &mut Ui,
        settings: &ModelSettings,
        key: &str,
        live: Option<&Arc<LiveWorld>>,
        session: Option<Session>,
    ) {
        style::card(ui, |ui| {
            ui.add_enabled(
                !self.busy(),
                TextEdit::multiline(&mut self.target)
                    .hint_text("游玩目标")
                    .desired_width(f32::INFINITY)
                    .desired_rows(3),
            );
            ui.horizontal_wrapped(|ui| {
                let linked = live.is_some() && session.is_some();
                let available = linked && !self.target.trim().is_empty();
                if self.busy() {
                    if ui.button("停止自动游玩").clicked() {
                        self.task = None;
                        self.status = "自动游玩已停止".into();
                        self.error = None;
                    }
                } else if ui
                    .add_enabled(available, style::primary("开始自动游玩"))
                    .clicked()
                    && let (Some(game), Some(session)) = (live, session)
                {
                    self.error = None;
                    self.history.clear();
                    match Task::start(
                        settings.clone(),
                        key,
                        game.clone(),
                        session,
                        self.target.trim().to_owned(),
                    ) {
                        Ok(task) => {
                            self.transcript = Some(task.transcript.clone());
                            self.task = Some(task);
                            self.status = "正在读取当前角色".into();
                        }
                        Err(error) => self.error = Some(format!("{error:#}")),
                    }
                }
                if !self.status.is_empty() {
                    ui.label(RichText::new(&self.status).color(style::MUTED));
                } else if !linked {
                    ui.label(RichText::new("完成热加入并连接实时游戏后开始").color(style::MUTED));
                }
            });
            if let Some(error) = &self.error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
        });
        if self.busy() || !self.history.is_empty() {
            ui.add_space(10.0);
            style::card(ui, |ui| {
                ScrollArea::vertical()
                    .stick_to_bottom(true)
                    .id_salt("player-chat")
                    .max_height(330.0)
                    .show(ui, |ui| {
                        for message in &self.history {
                            ui.label(message);
                            ui.add_space(5.0);
                        }
                    });
                ui.horizontal_wrapped(|ui| {
                    let running = self.busy();
                    ui.add_enabled(
                        running,
                        TextEdit::singleline(&mut self.guidance)
                            .hint_text("给模型追加指导")
                            .desired_width(450.0),
                    );
                    if ui
                        .add_enabled(
                            running && !self.guidance.trim().is_empty(),
                            Button::new("发送"),
                        )
                        .clicked()
                        && let Some(task) = &self.task
                    {
                        let message = self.guidance.trim().to_owned();
                        match task.guide(message.clone()) {
                            Ok(()) => {
                                self.guidance.clear();
                                self.record(format!("你  {message}"));
                            }
                            Err(error) => self.error = Some(format!("{error:#}")),
                        }
                    }
                });
                if let Some(path) = &self.transcript {
                    ui.label(RichText::new(path.display().to_string()).color(style::MUTED));
                }
            });
        }
    }

    fn record(&mut self, message: String) {
        if self.history.len() == 100 {
            self.history.pop_front();
        }
        self.history.push_back(message);
    }
}
