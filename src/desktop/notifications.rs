use std::collections::{BTreeSet, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc::TryRecvError;

use ck3_llm::notifications::watch::{Target, Task};
use ck3_llm::notifications::{
    HISTORY_LIMIT, Notice, Policy, append_history, history_path, load_history,
};
use ck3_llm::world::live::LiveWorld;
use eframe::egui::{RichText, TextEdit, Ui};

use super::style;

pub struct NotificationsPage {
    notices: VecDeque<Notice>,
    history_path: Option<PathBuf>,
    policy: Policy,
    feedback: Option<(bool, String)>,
    show_hidden: bool,
    character: String,
    watch: Option<Task>,
    watch_error: Option<String>,
}

impl NotificationsPage {
    pub fn open() -> Self {
        let mut page = Self {
            notices: VecDeque::new(),
            history_path: None,
            policy: Policy::default(),
            feedback: None,
            show_hidden: false,
            character: String::new(),
            watch: None,
            watch_error: None,
        };
        match Policy::path() {
            Ok(path) if path.exists() => match Policy::load(&path) {
                Ok(policy) => page.policy = policy,
                Err(error) => page.feedback = Some((false, format!("{error:#}"))),
            },
            Err(error) => page.feedback = Some((false, format!("{error:#}"))),
            _ => {}
        }
        match history_path() {
            Ok(path) if path.is_file() => match load_history(&path) {
                Ok(notices) => page.notices = notices,
                Err(error) => page.feedback = Some((false, format!("{error:#}"))),
            },
            _ => {}
        }
        page
    }

    pub fn record(&mut self, kind: &str, text: impl Into<String>) {
        self.notices.push_back(Notice::runtime(kind, text));
        if self.notices.len() > HISTORY_LIMIT {
            self.notices.pop_front();
        }
    }

    pub fn record_live(&mut self, mut notices: Vec<Notice>) {
        notices.retain(|notice| {
            !self
                .notices
                .iter()
                .any(|entry| entry.kind == notice.kind && entry.text == notice.text)
        });
        if let Err(error) = history_path().and_then(|path| append_history(&path, &notices)) {
            self.feedback = Some((false, format!("写入通知记录失败 · {error:#}")));
        }
        self.notices.extend(notices);
        self.notices
            .drain(..self.notices.len().saturating_sub(HISTORY_LIMIT));
    }

    pub fn poll(&mut self, game: Option<&Arc<LiveWorld>>) {
        while let Some(task) = &self.watch {
            if !game.is_some_and(|game| task.attached_to(game)) {
                self.watch = None;
                self.watch_error = Some("实时 CK3 连接已断开".into());
                break;
            }
            match task.try_recv() {
                Ok(Ok(notices)) => {
                    self.watch_error = None;
                    self.record_live(notices);
                }
                Ok(Err(error)) => self.watch_error = Some(error),
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    self.watch = None;
                    self.watch_error = Some("实时通知已停止".into());
                }
            }
        }
    }

    pub fn show(&mut self, ui: &mut Ui, game: Option<&Arc<LiveWorld>>, machine: Option<u32>) {
        style::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.add_enabled(
                    self.watch.is_none(),
                    TextEdit::singleline(&mut self.character)
                        .desired_width(150.0)
                        .hint_text("角色 ID（可选）"),
                );
                if self.watch.is_some() {
                    if ui.button("停止跟踪").clicked() {
                        self.watch = None;
                        self.watch_error = None;
                    }
                } else if ui
                    .add_enabled(game.is_some(), style::primary("跟踪游戏通知"))
                    .clicked()
                    && let Some(game) = game
                {
                    let target = if self.character.trim().is_empty() {
                        Ok(machine.map_or(Target::LocalPlayer, Target::Machine))
                    } else {
                        self.character
                            .trim()
                            .parse()
                            .map(Target::Character)
                            .map_err(|_| anyhow::anyhow!("角色 ID 需要是整数"))
                    };
                    self.watch_error = None;
                    match target.and_then(|target| Task::start(game.clone(), target)) {
                        Ok(task) => self.watch = Some(task),
                        Err(error) => self.watch_error = Some(format!("{error:#}")),
                    }
                }
                if game.is_none() {
                    ui.label(
                        RichText::new("在世界查询页连接 CK3 后可跟踪通知").color(style::MUTED),
                    );
                } else if self.watch.is_some() && self.watch_error.is_none() {
                    ui.label(RichText::new("正在跟踪").color(style::GREEN));
                }
            });
            if let Some(error) = &self.watch_error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
            ui.separator();
            ui.horizontal_wrapped(|ui| {
                if ui.button("打开旧版通知记录").clicked()
                    && let Some(path) = rfd::FileDialog::new()
                        .add_filter("JSONL", &["jsonl"])
                        .pick_file()
                {
                    self.feedback = Some(match load_history(&path) {
                        Ok(history) => {
                            let count = history.len();
                            self.notices.retain(|notice| notice.group == "app_session");
                            self.notices.extend(history);
                            self.notices
                                .drain(..self.notices.len().saturating_sub(HISTORY_LIMIT));
                            self.history_path = Some(path);
                            (true, format!("已读取 {count} 条游戏通知"))
                        }
                        Err(error) => (false, format!("{error:#}")),
                    });
                }
                if ui.button("导入旧版通知设置").clicked()
                    && let Some(path) = rfd::FileDialog::new()
                        .add_filter("JSON", &["json"])
                        .pick_file()
                {
                    self.feedback = Some(match Policy::load(&path) {
                        Ok(policy) => {
                            self.policy = policy;
                            (true, "通知设置已导入".into())
                        }
                        Err(error) => (false, format!("{error:#}")),
                    });
                }
                if ui.button("保存筛选").clicked() {
                    self.feedback = Some(
                        match Policy::path().and_then(|path| self.policy.save(&path)) {
                            Ok(()) => (true, "通知筛选已保存".into()),
                            Err(error) => (false, format!("{error:#}")),
                        },
                    );
                }
            });
            ui.checkbox(&mut self.show_hidden, "显示已关闭的通知");
            if let Some(path) = &self.history_path {
                ui.label(RichText::new(path.display().to_string()).color(style::MUTED));
            }
            if let Some((ok, message)) = &self.feedback {
                ui.label(RichText::new(message).color(if *ok {
                    style::GREEN
                } else {
                    style::ERROR
                }));
            }
            ui.collapsing("按类别和通知类型筛选", |ui| {
                let groups: BTreeSet<_> = self
                    .notices
                    .iter()
                    .map(|notice| notice.group.clone())
                    .chain(self.policy.disabled_groups.iter().cloned())
                    .collect();
                let types: BTreeSet<_> = self
                    .notices
                    .iter()
                    .map(|notice| notice.kind.clone())
                    .chain(self.policy.disabled_types.iter().cloned())
                    .collect();
                ui.label("类别");
                ui.horizontal_wrapped(|ui| {
                    for group in groups {
                        filter(ui, &mut self.policy.disabled_groups, group);
                    }
                });
                ui.label("通知类型");
                ui.horizontal_wrapped(|ui| {
                    for kind in types {
                        filter(ui, &mut self.policy.disabled_types, kind);
                    }
                });
            });
        });
        for notice in self
            .notices
            .iter()
            .rev()
            .filter(|notice| self.show_hidden || self.policy.allows(notice))
        {
            ui.add_space(6.0);
            style::card(ui, |ui| {
                ui.label(RichText::new(&notice.kind).strong().color(style::MUTED));
                ui.label(&notice.text);
            });
        }
    }
}

fn filter(ui: &mut Ui, hidden: &mut BTreeSet<String>, key: String) {
    let mut enabled = !hidden.contains(&key);
    if ui.checkbox(&mut enabled, &key).changed() {
        if enabled {
            hidden.remove(&key);
        } else {
            hidden.insert(key);
        }
    }
}
