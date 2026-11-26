use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

use ck3_llm::model::{self, Message, Reply, Role};
use ck3_llm::settings::{ModelSettings, read_json};
use ck3_llm::world::QuerySource;
use eframe::egui::{Button, DragValue, RichText, ScrollArea, TextEdit, Ui};

use super::{memory::MemoryPage, style};

#[derive(Clone, Copy)]
enum Operation {
    Test,
    Chat,
}

struct Request {
    operation: Operation,
    response: Receiver<anyhow::Result<Reply>>,
}

#[derive(Default)]
pub struct ModelPage {
    api_key: String,
    draft: String,
    chat: Vec<Message>,
    pending: Option<Request>,
    settings_feedback: Option<(bool, String)>,
    chat_error: Option<String>,
    allowed_world: Option<&'static str>,
}

impl ModelPage {
    pub fn busy(&self) -> bool {
        self.pending.is_some()
    }

    pub fn poll(&mut self) {
        let Some(request) = &self.pending else { return };
        let result = match request.response.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return,
            Err(TryRecvError::Disconnected) => Err(anyhow::anyhow!("模型请求线程已退出")),
        };
        match (request.operation, result) {
            (Operation::Test, result) => {
                self.settings_feedback = Some(match result {
                    Ok(reply) => (
                        true,
                        format!(
                            "连接成功 · HTTP {} · {} ms",
                            reply.status,
                            reply.elapsed.as_millis()
                        ),
                    ),
                    Err(error) => (false, format!("{error:#}")),
                });
            }
            (Operation::Chat, Ok(reply)) => self.chat.push(Message {
                role: Role::Assistant,
                content: reply.content,
            }),
            (Operation::Chat, Err(error)) => self.chat_error = Some(format!("{error:#}")),
        }
        self.pending = None;
    }

    pub fn show_settings(&mut self, ui: &mut Ui, settings: &mut ModelSettings) {
        style::card(ui, |ui| {
            ui.label("Chat Completions API 地址");
            ui.add(TextEdit::singleline(&mut settings.endpoint).desired_width(f32::INFINITY));
            ui.label("模型名称");
            ui.add(TextEdit::singleline(&mut settings.model).desired_width(f32::INFINITY));
            ui.label("密钥环境变量");
            let env = settings.api_key_env.get_or_insert_with(String::new);
            ui.add(
                TextEdit::singleline(env)
                    .desired_width(f32::INFINITY)
                    .hint_text("CK3_MODEL_API_KEY"),
            );
            if env.is_empty() {
                settings.api_key_env = None;
            }
            ui.label("临时 API 密钥").on_hover_text("仅本次运行使用");
            ui.add(
                TextEdit::singleline(&mut self.api_key)
                    .password(true)
                    .desired_width(f32::INFINITY)
                    .hint_text("可选"),
            );
            ui.horizontal(|ui| {
                ui.label("请求超时");
                ui.add(
                    DragValue::new(&mut settings.request_timeout)
                        .speed(1.0)
                        .range(0.1..=600.0)
                        .suffix(" 秒"),
                );
            });
            ui.label("对话指令");
            ui.add(
                TextEdit::multiline(&mut settings.instructions)
                    .desired_width(f32::INFINITY)
                    .desired_rows(3),
            );
            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(!self.busy(), style::primary("测试连接"))
                    .clicked()
                {
                    self.submit(Operation::Test, settings, None);
                }
                if ui
                    .add_enabled(!self.busy(), Button::new("导入旧版模型配置"))
                    .clicked()
                    && let Some(path) = rfd::FileDialog::new()
                        .add_filter("JSON", &["json"])
                        .pick_file()
                {
                    self.settings_feedback = Some(match read_json(&path) {
                        Ok(imported) => {
                            *settings = imported;
                            self.api_key.clear();
                            (true, "模型配置已导入".into())
                        }
                        Err(error) => (false, format!("{error:#}")),
                    });
                }
                if self.busy() {
                    ui.spinner();
                }
            });
            if let Some((success, feedback)) = &self.settings_feedback {
                ui.label(RichText::new(feedback).color(if *success {
                    style::GREEN
                } else {
                    style::ERROR
                }));
            }
        });
    }

    pub fn show_chat(
        &mut self,
        ui: &mut Ui,
        settings: &ModelSettings,
        memory: &MemoryPage,
        world: Option<Arc<dyn QuerySource>>,
        source_name: &'static str,
    ) {
        style::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(
                    RichText::new(settings.model.trim())
                        .strong()
                        .color(style::BLUE_INK),
                );
                if ui
                    .add_enabled(!self.busy() && !self.chat.is_empty(), Button::new("新对话"))
                    .clicked()
                {
                    self.chat.clear();
                    self.chat_error = None;
                }
            });
            let history_height = (ui.available_height() - 140.0).clamp(130.0, 430.0);
            ScrollArea::vertical()
                .id_salt("conversation")
                .max_height(history_height)
                .stick_to_bottom(true)
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    if self.chat.is_empty() {
                        ui.label(RichText::new("在下方输入消息开始对话。").color(style::MUTED));
                    }
                    for message in &self.chat {
                        ui.separator();
                        let speaker = if matches!(message.role, Role::User) {
                            "你"
                        } else {
                            "模型"
                        };
                        ui.label(RichText::new(speaker).strong().color(style::MUTED));
                        ui.label(&message.content);
                        ui.add_space(6.0);
                    }
                });
        });
        ui.add_space(10.0);
        style::card(ui, |ui| {
            ui.add(
                TextEdit::multiline(&mut self.draft)
                    .desired_rows(3)
                    .desired_width(f32::INFINITY)
                    .hint_text("输入消息"),
            );
            ui.horizontal_wrapped(|ui| {
                let mut use_world = self.allowed_world == Some(source_name);
                ui.add_enabled_ui(world.is_some() && !self.busy(), |ui| {
                    if ui
                        .checkbox(&mut use_world, format!("允许模型查询{source_name}"))
                        .changed()
                    {
                        self.allowed_world = use_world.then_some(source_name);
                    }
                });
                let world = world.as_ref().filter(|_| use_world);
                if ui
                    .add_enabled(
                        !self.busy() && !self.draft.trim().is_empty(),
                        style::primary("发送"),
                    )
                    .clicked()
                {
                    self.chat.push(Message {
                        role: Role::User,
                        content: self.draft.trim().to_owned(),
                    });
                    self.draft.clear();
                    self.submit_chat(settings, memory, world);
                }
                if self.chat_error.is_some()
                    && ui.add_enabled(!self.busy(), Button::new("重试")).clicked()
                {
                    self.submit_chat(settings, memory, world);
                }
                if self
                    .pending
                    .as_ref()
                    .is_some_and(|job| matches!(job.operation, Operation::Chat))
                {
                    ui.spinner();
                    ui.label("等待模型回复");
                }
            });
            if let Some(error) = &self.chat_error {
                ui.label(RichText::new(error).color(style::ERROR));
            }
        });
    }

    fn submit_chat(
        &mut self,
        settings: &ModelSettings,
        memory: &MemoryPage,
        world: Option<&Arc<dyn QuerySource>>,
    ) {
        let mut settings = settings.clone();
        let recalled = memory.prompt();
        if !recalled.is_empty() {
            settings
                .instructions
                .push_str("\n\n以下是已启用的用户记忆。\n");
            settings.instructions.push_str(&recalled);
        }
        self.submit(Operation::Chat, &settings, world.cloned());
    }

    fn submit(
        &mut self,
        operation: Operation,
        settings: &ModelSettings,
        world: Option<Arc<dyn QuerySource>>,
    ) {
        match operation {
            Operation::Test => self.settings_feedback = None,
            Operation::Chat => self.chat_error = None,
        }
        let (send, response) = mpsc::channel();
        let settings = settings.clone();
        let key = self.api_key.clone();
        let history = self.chat.clone();
        let worker = thread::Builder::new()
            .name("model-request".into())
            .spawn(move || {
                let result = match operation {
                    Operation::Test => model::test_connection(&settings, &key),
                    Operation::Chat => {
                        model::chat_in_workspace(&settings, &key, &history, world.as_deref())
                    }
                };
                let _ = send.send(result);
            });
        match (worker, operation) {
            (Ok(_), _) => {
                self.pending = Some(Request {
                    operation,
                    response,
                })
            }
            (Err(error), Operation::Test) => {
                self.settings_feedback = Some((false, error.to_string()))
            }
            (Err(error), Operation::Chat) => self.chat_error = Some(error.to_string()),
        }
    }
}
