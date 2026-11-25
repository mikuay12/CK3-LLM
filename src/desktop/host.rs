use std::path::PathBuf;
use std::sync::mpsc::TryRecvError;

use ck3_llm::host::{HostTask, HostUpdate};
use ck3_llm::settings::HostSettings;
use eframe::egui::{Button, DragValue, RichText, TextEdit, Ui};

use super::{connection::path_field, style};

#[derive(Default)]
pub struct HostPage {
    task: Option<HostTask>,
    port: Option<u16>,
    capture: Option<PathBuf>,
    pid: Option<u32>,
    backend_ready: bool,
    profile_ready: bool,
    stopping: bool,
    error: Option<String>,
}

impl HostPage {
    pub fn busy(&self) -> bool {
        self.task.is_some()
    }

    pub fn start(&mut self, settings: &HostSettings, game_dir: &str) {
        self.reset_status();
        self.error = None;
        self.capture = None;
        match HostTask::start(PathBuf::from(game_dir.trim()), settings.clone()) {
            Ok(task) => self.task = Some(task),
            Err(error) => self.error = Some(format!("{error:#}")),
        }
    }

    pub fn poll(&mut self) -> Option<PathBuf> {
        let mut profile = None;
        while let Some(task) = &self.task {
            match task.try_recv() {
                Ok(HostUpdate::Listening { port, capture }) => {
                    self.port = Some(port);
                    self.capture = Some(capture);
                }
                Ok(HostUpdate::Started { pid }) => self.pid = Some(pid),
                Ok(HostUpdate::Ready) => self.backend_ready = true,
                Ok(HostUpdate::Profile(path)) => {
                    self.profile_ready = true;
                    profile = Some(path);
                }
                Ok(HostUpdate::Ended(result)) => {
                    self.task = None;
                    self.error = result.err().map(|error| format!("{error:#}"));
                    self.reset_status();
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    self.task = None;
                    self.error = Some("房主线程已退出".into());
                    self.reset_status();
                }
            }
        }
        profile
    }

    pub fn show(
        &mut self,
        ui: &mut Ui,
        settings: &mut HostSettings,
        game_dir: &mut String,
    ) -> bool {
        let mut open_room = false;
        style::card(ui, |ui| {
            ui.add_enabled_ui(!self.busy(), |ui| {
                path_field(ui, "CK3 安装目录", game_dir, true);
                ui.label("房主名称");
                ui.add(TextEdit::singleline(&mut settings.username).desired_width(f32::INFINITY));
                ui.horizontal(|ui| {
                    ui.label("本地联机端口");
                    ui.add(DragValue::new(&mut settings.port).range(1..=u16::MAX))
                        .on_hover_text("CK3 实机验证使用默认端口 7350");
                });
            });
            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                if let Some(task) = &self.task {
                    if ui
                        .add_enabled(!self.stopping, Button::new("停止本地服务"))
                        .clicked()
                    {
                        task.stop();
                        self.stopping = true;
                    }
                    if self.profile_ready
                        && ui
                            .add_enabled(!self.stopping, style::primary("打开房间连接"))
                            .clicked()
                    {
                        open_room = true;
                    }
                } else if ui.add(style::primary("启动 CK3 房主")).clicked() {
                    self.start(settings, game_dir);
                }
            });
            if let Some(error) = &self.error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
        });
        ui.add_space(10.0);
        style::card(ui, |ui| {
            let status = if self.stopping {
                "正在停止本地服务"
            } else if self.profile_ready {
                "本地房间配置已生成"
            } else if self.backend_ready {
                "本地服务已连接，等待在 CK3 中创建房间"
            } else if self.pid.is_some() {
                "CK3 正在启动"
            } else if self.busy() {
                "正在准备本地房主"
            } else {
                "本地房主未启动"
            };
            ui.label(
                RichText::new(status)
                    .size(18.0)
                    .strong()
                    .color(if self.profile_ready {
                        style::GREEN
                    } else {
                        style::MUTED
                    }),
            );
            if let Some(port) = self.port {
                ui.label(format!("127.0.0.1:{port}"));
            }
            if let Some(capture) = &self.capture {
                ui.label(RichText::new(capture.display().to_string()).color(style::MUTED));
            }
        });
        open_room
    }

    fn reset_status(&mut self) {
        self.port = None;
        self.pid = None;
        self.backend_ready = false;
        self.profile_ready = false;
        self.stopping = false;
    }
}
