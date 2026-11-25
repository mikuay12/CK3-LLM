use std::collections::{BTreeSet, VecDeque};
use std::path::PathBuf;

use ck3_llm::notifications::{HISTORY_LIMIT, Notice, Policy, load_history};
use eframe::egui::{RichText, Ui};

use super::style;

pub struct NotificationsPage {
    notices: VecDeque<Notice>,
    history_path: Option<PathBuf>,
    policy: Policy,
    feedback: Option<(bool, String)>,
    show_hidden: bool,
}

impl NotificationsPage {
    pub fn open() -> Self {
        let mut page = Self {
            notices: VecDeque::new(),
            history_path: None,
            policy: Policy::default(),
            feedback: None,
            show_hidden: false,
        };
        match Policy::path() {
            Ok(path) if path.exists() => match Policy::load(&path) {
                Ok(policy) => page.policy = policy,
                Err(error) => page.feedback = Some((false, format!("{error:#}"))),
            },
            Err(error) => page.feedback = Some((false, format!("{error:#}"))),
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

    pub fn show(&mut self, ui: &mut Ui) {
        style::card(ui, |ui| {
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
