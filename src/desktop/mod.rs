mod connection;
mod host;
mod model;
mod style;

use std::path::PathBuf;
use std::time::Duration;

use ck3_llm::session::CK3_BUILD;
use ck3_llm::settings::Settings;
use eframe::egui::{self, Align, Button, Frame, Layout, Margin, RichText, ScrollArea};

#[derive(PartialEq, Eq, Clone, Copy)]
enum Page {
    Host,
    Connection,
    Model,
    Chat,
}

impl Page {
    fn name(self) -> &'static str {
        match self {
            Self::Host => "本地房主",
            Self::Connection => "房间连接",
            Self::Model => "模型设置",
            Self::Chat => "模型对话",
        }
    }
}

pub struct Desktop {
    page: Page,
    settings: Settings,
    settings_path: Option<PathBuf>,
    settings_feedback: Option<(bool, String)>,
    host: host::HostPage,
    connection: connection::ConnectionPage,
    model: model::ModelPage,
}

impl Desktop {
    pub fn new(context: &egui::Context, start_host: bool) -> Self {
        style::configure(context);
        let mut app = Self {
            page: Page::Host,
            settings: Settings::default(),
            settings_path: None,
            settings_feedback: None,
            host: host::HostPage::default(),
            connection: connection::ConnectionPage::default(),
            model: model::ModelPage::default(),
        };
        if let Err(error) = app.load_settings() {
            app.settings_feedback = Some((false, format!("{error:#}")));
        } else if start_host {
            app.host
                .start(&app.settings.host, &app.settings.connection.game_dir);
        }
        app
    }

    fn load_settings(&mut self) -> anyhow::Result<()> {
        let path = Settings::path()?;
        let loaded = Settings::load(&path);
        self.settings_path = Some(path);
        self.settings = loaded?;
        Ok(())
    }

    fn save_settings(&mut self) {
        if let Some(path) = &self.settings_path {
            self.settings_feedback = Some(match self.settings.save(path) {
                Ok(()) => (true, "设置已保存".into()),
                Err(error) => (false, format!("{error:#}")),
            });
        }
    }

    fn navigation(&mut self, context: &egui::Context) {
        egui::SidePanel::left("navigation")
            .resizable(false)
            .exact_width(205.0)
            .frame(
                Frame::new()
                    .fill(style::WHITE)
                    .inner_margin(Margin::symmetric(20, 25)),
            )
            .show(context, |ui| {
                ui.label(RichText::new("CK3-LLM").size(23.0).strong());
                ui.add_space(25.0);
                for page in [Page::Host, Page::Connection, Page::Model, Page::Chat] {
                    let selected = self.page == page;
                    let text = RichText::new(page.name()).size(15.0).color(if selected {
                        style::BLUE_INK
                    } else {
                        style::TEXT
                    });
                    let button = Button::new(text)
                        .fill(if selected {
                            style::BLUE_TINT
                        } else {
                            egui::Color32::TRANSPARENT
                        })
                        .stroke(egui::Stroke::NONE);
                    if ui.add_sized([ui.available_width(), 40.0], button).clicked() {
                        self.page = page;
                    }
                }
                ui.with_layout(Layout::bottom_up(Align::Min), |ui| {
                    ui.label(
                        RichText::new(format!("适配 CK3 {CK3_BUILD}"))
                            .small()
                            .color(style::MUTED),
                    );
                    if ui
                        .add_enabled(self.settings_path.is_some(), Button::new("保存设置"))
                        .clicked()
                    {
                        self.save_settings();
                    }
                });
            });
    }
}

impl eframe::App for Desktop {
    fn update(&mut self, context: &egui::Context, _: &mut eframe::Frame) {
        if let Some(profile) = self.host.poll() {
            self.settings.connection.profile_path = profile.to_string_lossy().into_owned();
        }
        self.connection.poll();
        self.model.poll();
        if self.host.busy() || self.connection.busy() || self.model.busy() {
            context.request_repaint_after(Duration::from_millis(60));
        }
        self.navigation(context);

        egui::CentralPanel::default()
            .frame(
                Frame::new()
                    .fill(style::SURFACE)
                    .inner_margin(Margin::same(28)),
            )
            .show(context, |ui| {
                ScrollArea::vertical()
                    .id_salt(self.page.name())
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.set_max_width(960.0);
                        ui.label(RichText::new(self.page.name()).size(30.0).strong());
                        ui.add_space(12.0);
                        if let Some((success, message)) = &self.settings_feedback {
                            let color = if *success { style::GREEN } else { style::ERROR };
                            let dismiss = ui
                                .horizontal_wrapped(|ui| {
                                    ui.label(RichText::new(message).color(color));
                                    ui.small_button("关闭提示").clicked()
                                })
                                .inner;
                            if dismiss {
                                self.settings_feedback = None;
                            }
                            ui.add_space(6.0);
                        }
                        match self.page {
                            Page::Host => {
                                if self.host.show(
                                    ui,
                                    &mut self.settings.host,
                                    &mut self.settings.connection.game_dir,
                                ) {
                                    self.page = Page::Connection;
                                }
                            }
                            Page::Connection => {
                                self.connection.show(ui, &mut self.settings.connection)
                            }
                            Page::Model => self.model.show_settings(ui, &mut self.settings.model),
                            Page::Chat => self.model.show_chat(ui, &self.settings.model),
                        }
                    });
            });
    }
}
