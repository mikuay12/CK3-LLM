use ck3_llm::commands::{Specification, catalog};
use ck3_llm::session::SessionTask;
use eframe::egui::{Button, ComboBox, DragValue, RichText, TextEdit, Ui};
use serde_json::{Map, Value, json};

use super::style;

pub struct ActionEditor {
    search: String,
    command: String,
    input: String,
    attributes: u8,
}

impl Default for ActionEditor {
    fn default() -> Self {
        Self {
            search: String::new(),
            command: String::new(),
            input: String::new(),
            attributes: 14,
        }
    }
}

impl ActionEditor {
    pub fn show(
        &mut self,
        ui: &mut Ui,
        task: &SessionTask,
        machine: i32,
    ) -> Option<anyhow::Result<()>> {
        let mut result = None;
        ui.collapsing("游戏命令", |ui| {
            ui.add(
                TextEdit::singleline(&mut self.search)
                    .desired_width(f32::INFINITY)
                    .hint_text("按命令名搜索"),
            );
            let selected = if self.command.is_empty() {
                "选择命令"
            } else {
                &self.command
            };
            ComboBox::from_id_salt("game-command")
                .selected_text(selected)
                .show_ui(ui, |ui| {
                    for (name, spec) in catalog()
                        .iter()
                        .filter(|(name, _)| visible(name) && name.contains(self.search.trim()))
                    {
                        if ui.selectable_label(&self.command == name, name).clicked() {
                            self.command.clone_from(name);
                            self.input = template(spec, machine);
                        }
                    }
                });
            if let Some(spec) = catalog().get(&self.command) {
                for field in spec.fields.iter().filter(|field| !field.choices.is_empty()) {
                    let labels = field
                        .choices
                        .keys()
                        .cloned()
                        .collect::<Vec<_>>()
                        .join(" / ");
                    ui.label(
                        RichText::new(format!("{}  {}", field.name, labels)).color(style::MUTED),
                    );
                }
                ui.add(
                    TextEdit::multiline(&mut self.input)
                        .desired_rows(5)
                        .desired_width(f32::INFINITY)
                        .code_editor(),
                );
                ui.horizontal_wrapped(|ui| {
                    ui.label("命令 attribs");
                    ui.add(DragValue::new(&mut self.attributes));
                    if ui.add(Button::new("提交命令")).clicked() {
                        result = Some(
                            serde_json::from_str(&self.input)
                                .map_err(Into::into)
                                .and_then(|fields| {
                                    task.submit_command(
                                        self.command.clone(),
                                        fields,
                                        self.attributes,
                                    )
                                }),
                        );
                    }
                });
            }
        });
        result
    }
}

fn visible(name: &str) -> bool {
    !name.starts_with("filetransfer_")
        && !matches!(
            name,
            "add_player"
                | "set_player_here_command"
                | "client_ping"
                | "sync_players_command"
                | "hotjoin_sync_command"
                | "daily_tick"
                | "event_tick"
        )
}

fn template(spec: &Specification, machine: i32) -> String {
    let values: Map<_, _> = spec
        .fields
        .iter()
        .filter(|field| !field.optional)
        .map(|field| {
            let value = if field.name == "machineid" {
                json!(machine)
            } else {
                field.default.clone().unwrap_or(Value::Null)
            };
            (field.name.clone(), value)
        })
        .collect();
    serde_json::to_string_pretty(&values).expect("command fields are serializable")
}
