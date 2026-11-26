use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use ck3_llm::session::{
    SessionTask, Traffic, Update,
    native::{NakamaSdk, SUPPORTED_SDK},
};
use ck3_llm::settings::ConnectionSettings;
use eframe::egui::{Button, RichText, TextEdit, Ui};

use super::{actions::ActionEditor, notifications::NotificationsPage, style};

#[derive(Default, PartialEq, Eq)]
enum Phase {
    #[default]
    Idle,
    Authenticating,
    Connecting,
    Joining,
    Joined,
    Stopping,
    Failed,
}

impl Phase {
    fn label(&self) -> &'static str {
        match self {
            Self::Idle => "未连接",
            Self::Authenticating => "正在登录",
            Self::Connecting => "正在连接 Nakama",
            Self::Joining => "正在加入房间",
            Self::Joined => "已加入 Nakama 房间",
            Self::Stopping => "正在断开",
            Self::Failed => "连接中断",
        }
    }
}

#[derive(Default)]
pub struct ConnectionPage {
    phase: Phase,
    task: Option<SessionTask>,
    probe: Option<Receiver<anyhow::Result<()>>>,
    feedback: Option<(bool, String)>,
    room: Option<(String, String)>,
    traffic: Traffic,
    game_machine: Option<i32>,
    transfer: Option<(u32, u32)>,
    snapshot: Option<PathBuf>,
    character: String,
    handshake_code: Option<u32>,
    actions: ActionEditor,
}

impl ConnectionPage {
    pub fn busy(&self) -> bool {
        self.task.is_some() || self.probe.is_some()
    }

    pub fn machine(&self) -> Option<u32> {
        self.game_machine
            .and_then(|machine| u32::try_from(machine).ok())
    }

    pub fn poll(&mut self, notifications: &mut NotificationsPage) {
        if let Some(probe) = &self.probe {
            let result = match probe.try_recv() {
                Ok(result) => Some(result),
                Err(TryRecvError::Disconnected) => Some(Err(anyhow::anyhow!("SDK 检测线程已退出"))),
                Err(TryRecvError::Empty) => None,
            };
            if let Some(result) = result {
                self.feedback = Some(match result {
                    Ok(()) => (true, format!("SDK {SUPPORTED_SDK} 加载成功")),
                    Err(error) => (false, format!("{error:#}")),
                });
                self.probe = None;
            }
        }

        while let Some(task) = &self.task {
            let result = match task.try_recv() {
                Ok(event) => event,
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    Update::Ended(Err(anyhow::anyhow!("联机线程已退出")))
                }
            };
            match result {
                Update::Authenticated if self.phase != Phase::Stopping => {
                    self.phase = Phase::Connecting
                }
                Update::Joining if self.phase != Phase::Stopping => self.phase = Phase::Joining,
                Update::Joined { match_id, user_id } if self.phase != Phase::Stopping => {
                    self.phase = Phase::Joined;
                    notifications.record("room_joined", format!("已加入本地房间 {match_id}"));
                    self.room = Some((match_id, user_id));
                }
                Update::Traffic(traffic) => self.traffic = traffic,
                Update::GameHandshake(code) => self.handshake_code = Some(code),
                Update::GameConnected { machine } => self.game_machine = Some(machine),
                Update::GameTransfer { received, total } => self.transfer = Some((received, total)),
                Update::GameSnapshot { path, machine } => {
                    self.game_machine = Some(machine);
                    notifications.record(
                        "snapshot_loaded",
                        format!("玩家 {machine} 的热加入快照已接收并验证"),
                    );
                    self.snapshot = Some(path);
                }
                Update::ActionSubmitted(message) => self.feedback = Some((true, message)),
                Update::Notice(message) => {
                    notifications.record("session_notice", &message);
                    self.feedback = Some((false, message));
                }
                Update::Ended(result) => {
                    self.task = None;
                    self.room = None;
                    self.game_machine = None;
                    match result {
                        Ok(()) => self.phase = Phase::Idle,
                        Err(error) => {
                            notifications.record("connection_error", format!("{error:#}"));
                            self.set_error(error);
                        }
                    }
                }
                _ => {}
            }
        }
    }

    pub fn show(&mut self, ui: &mut Ui, settings: &mut ConnectionSettings) -> Option<PathBuf> {
        style::card(ui, |ui| {
            ui.add_enabled_ui(!self.busy(), |ui| {
                path_field(ui, "CK3 安装目录", &mut settings.game_dir, true);
                path_field(ui, "房间配置文件", &mut settings.profile_path, false);
                ui.checkbox(&mut settings.join_game, "尝试游戏内热加入");
                ui.columns(2, |columns| {
                    let [player, identity] = columns else {
                        unreachable!()
                    };
                    player.label("玩家名");
                    player.add(
                        TextEdit::singleline(&mut settings.username).desired_width(f32::INFINITY),
                    );
                    identity
                        .label("会话身份")
                        .on_hover_text("重连时使用相同身份");
                    identity.add(
                        TextEdit::singleline(&mut settings.identity).desired_width(f32::INFINITY),
                    );
                });
            });
            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                if let Some(task) = &self.task {
                    if ui
                        .add_enabled(self.phase != Phase::Stopping, Button::new("断开连接"))
                        .clicked()
                    {
                        self.phase = Phase::Stopping;
                        task.stop();
                    }
                } else {
                    if ui
                        .add_enabled(self.probe.is_none(), style::primary("加入房间"))
                        .clicked()
                    {
                        self.join(settings);
                    }
                    if ui
                        .add_enabled(!self.busy(), Button::new("检测 SDK"))
                        .clicked()
                    {
                        self.probe_sdk(PathBuf::from(settings.game_dir.trim()));
                    }
                }
                if self.probe.is_some() {
                    ui.spinner();
                    ui.label("正在检测 SDK");
                }
            });
            if let Some((ok, feedback)) = &self.feedback {
                ui.label(RichText::new(feedback).color(if *ok {
                    style::GREEN
                } else {
                    style::ERROR
                }));
            }
        });
        ui.add_space(10.0);
        style::card(ui, |ui| {
            let color = match self.phase {
                Phase::Joined => style::GREEN,
                Phase::Failed => style::ERROR,
                _ => style::MUTED,
            };
            ui.label(
                RichText::new(self.phase.label())
                    .size(18.0)
                    .strong()
                    .color(color),
            );
            if let Some((room, user)) = &self.room {
                ui.label(RichText::new(format!("房间  {room}")).color(style::MUTED));
                ui.label(RichText::new(format!("玩家 ID  {user}")).color(style::MUTED));
                ui.separator();
            }
            ui.columns(3, |columns| {
                metric(&mut columns[0], "收到数据包", self.traffic.packets);
                metric(&mut columns[1], "PDX 帧", self.traffic.pdx_frames);
                metric(&mut columns[2], "PDX 消息", self.traffic.pdx_messages);
            });
            if self.traffic.invalid_frames > 0 {
                ui.label(
                    RichText::new(format!("{} 帧解析失败", self.traffic.invalid_frames))
                        .color(style::ERROR),
                );
            }
        });
        let mut open = None;
        if self.game_machine.is_some() || self.snapshot.is_some() || self.handshake_code.is_some() {
            ui.add_space(10.0);
            style::card(ui, |ui| {
                if let Some(machine) = self.game_machine {
                    ui.label(format!("游戏玩家编号 {machine}"));
                } else if self.task.is_some()
                    && let Some(code) = self.handshake_code
                {
                    ui.label(format!("游戏握手结果 {code}"));
                }
                if let Some((received, total)) = self.transfer {
                    ui.label(format!("快照分块 {received} / {total}"));
                }
                if let Some(snapshot) = &self.snapshot {
                    if ui.button("查看收到的快照").clicked() {
                        open = Some(snapshot.clone());
                    }
                    if let Some(task) = &self.task
                        && self.phase == Phase::Joined
                    {
                        ui.horizontal_wrapped(|ui| {
                            ui.add(
                                TextEdit::singleline(&mut self.character)
                                    .desired_width(130.0)
                                    .hint_text("人物 ID"),
                            );
                            let character = self.character.trim().parse::<u32>();
                            let mut action = None;
                            if ui
                                .add_enabled(character.is_ok(), Button::new("选角"))
                                .clicked()
                                && let Ok(character) = character
                            {
                                action = Some(task.select_character(character));
                            }
                            if ui.button("准备").clicked() {
                                action = Some(task.set_ready(true));
                            }
                            if ui.button("取消准备").clicked() {
                                action = Some(task.set_ready(false));
                            }
                            if let Some(Err(error)) = action {
                                self.feedback = Some((false, format!("{error:#}")));
                            }
                        });
                        if let Some(machine) = self.game_machine
                            && let Some(Err(error)) = self.actions.show(ui, task, machine)
                        {
                            self.feedback = Some((false, format!("{error:#}")));
                        }
                    }
                }
            });
        }
        open
    }

    fn join(&mut self, settings: &ConnectionSettings) {
        self.feedback = None;
        self.traffic = Traffic::default();
        self.game_machine = None;
        self.transfer = None;
        self.snapshot = None;
        self.handshake_code = None;
        self.actions = ActionEditor::default();
        match SessionTask::start(settings.clone()) {
            Ok(task) => {
                self.task = Some(task);
                self.phase = Phase::Authenticating;
            }
            Err(error) => self.set_error(error),
        }
    }

    fn set_error(&mut self, error: anyhow::Error) {
        self.phase = Phase::Failed;
        self.feedback = Some((false, format!("{error:#}")));
    }

    fn probe_sdk(&mut self, path: PathBuf) {
        self.feedback = None;
        let (send, receive) = mpsc::channel();
        match thread::Builder::new()
            .name("nakama-sdk-check".into())
            .spawn(move || {
                let _ = send.send(NakamaSdk::load(&path).map(|_| ()));
            }) {
            Ok(_) => self.probe = Some(receive),
            Err(error) => self.feedback = Some((false, error.to_string())),
        }
    }
}

fn metric(ui: &mut Ui, title: &str, value: u64) {
    ui.label(RichText::new(title).color(style::MUTED));
    ui.label(RichText::new(value.to_string()).size(23.0).strong());
}

pub(super) fn path_field(ui: &mut Ui, title: &str, value: &mut String, folder: bool) {
    ui.label(title);
    ui.horizontal(|ui| {
        let width = (ui.available_width() - 87.0).max(120.0);
        let hint = if folder {
            r"C:\Games\Crusader Kings III"
        } else {
            "session_profile.json"
        };
        ui.add_sized([width, 30.0], TextEdit::singleline(value).hint_text(hint));
        if !ui.button("浏览…").clicked() {
            return;
        }
        let current = Path::new(value);
        let mut dialog = rfd::FileDialog::new();
        let parent = if folder {
            Some(current)
        } else {
            current.parent()
        };
        if let Some(parent) = parent.filter(|path| path.is_dir()) {
            dialog = dialog.set_directory(parent);
        }
        let picked = if folder {
            dialog.pick_folder()
        } else {
            dialog.add_filter("JSON", &["json"]).pick_file()
        };
        if let Some(path) = picked {
            *value = path.to_string_lossy().into_owned();
        }
    });
}
