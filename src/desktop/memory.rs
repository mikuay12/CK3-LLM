use ck3_llm::memory::{Category, Scope, Store};
use eframe::egui::{ComboBox, RichText, TextEdit, Ui};

use super::style;

pub struct MemoryPage {
    store: Option<Store>,
    scope: Scope,
    category: Category,
    draft: String,
    feedback: Option<(bool, String)>,
    dirty: bool,
}

impl MemoryPage {
    pub fn open() -> Self {
        let mut page = Self {
            store: None,
            scope: Scope::default(),
            category: Category::default(),
            draft: String::new(),
            feedback: None,
            dirty: false,
        };
        page.reload();
        page
    }

    pub fn prompt(&self) -> String {
        self.store
            .as_ref()
            .map_or_else(String::new, |store| store.prompt(1500))
    }

    pub fn on_external_update(&mut self) {
        if self.dirty {
            self.feedback = Some((false, "模型已写入新记忆，当前页面还有未保存的修改".into()));
        } else {
            self.reload();
        }
    }

    pub fn show(&mut self, ui: &mut Ui) {
        style::card(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                for scope in Scope::ALL {
                    ui.selectable_value(&mut self.scope, scope, scope.label());
                }
            });
            if let Some(store) = &mut self.store {
                ui.add(
                    TextEdit::singleline(&mut self.draft)
                        .desired_width(f32::INFINITY)
                        .hint_text("添加一条记忆"),
                );
                ui.horizontal_wrapped(|ui| {
                    pick_category(ui, "new-memory-category", &mut self.category);
                    if ui
                        .add_enabled(!self.draft.trim().is_empty(), style::primary("添加"))
                        .clicked()
                    {
                        self.feedback =
                            Some(match store.add(self.scope, self.category, &self.draft) {
                                Ok(()) => {
                                    self.draft.clear();
                                    self.dirty = true;
                                    (true, "记忆已添加".into())
                                }
                                Err(error) => (false, format!("{error:#}")),
                            });
                    }
                    if ui.button("导入旧版记忆").clicked()
                        && let Some(file) = rfd::FileDialog::new()
                            .add_filter("JSON", &["json"])
                            .pick_file()
                    {
                        self.feedback = Some(match store.import(&file) {
                            Ok(count) => {
                                self.dirty |= count > 0;
                                (true, format!("已导入 {count} 条记忆"))
                            }
                            Err(error) => (false, format!("{error:#}")),
                        });
                    }
                    if ui.button("保存记忆").clicked() {
                        self.feedback = Some(match store.save() {
                            Ok(()) => {
                                self.dirty = false;
                                (true, "记忆已保存".into())
                            }
                            Err(error) => (false, format!("{error:#}")),
                        });
                    }
                });
            }
            if ui.button("从磁盘重新读取").clicked() {
                self.reload();
            }
            if let Some((ok, message)) = &self.feedback {
                ui.label(RichText::new(message).color(if *ok {
                    style::GREEN
                } else {
                    style::ERROR
                }));
            }
        });
        let Some(store) = &mut self.store else { return };
        let mut removed = None;
        for (index, entry) in store
            .items
            .iter_mut()
            .enumerate()
            .filter(|(_, entry)| entry.scope == self.scope)
        {
            ui.add_space(6.0);
            style::card(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    let category = entry.category;
                    pick_category(ui, &entry.id, &mut entry.category);
                    let enabled_changed = ui.checkbox(&mut entry.enabled, "用于对话").changed();
                    self.dirty |= category != entry.category || enabled_changed;
                    if ui.small_button("删除").clicked() {
                        removed = Some(index);
                    }
                });
                self.dirty |= ui
                    .add(
                        TextEdit::multiline(&mut entry.content)
                            .desired_rows(2)
                            .desired_width(f32::INFINITY),
                    )
                    .changed();
            });
        }
        if let Some(index) = removed {
            store.items.remove(index);
            self.dirty = true;
        }
    }

    fn reload(&mut self) {
        match Store::open() {
            Ok(store) => {
                self.store = Some(store);
                self.feedback = None;
                self.dirty = false;
            }
            Err(error) => self.feedback = Some((false, format!("{error:#}"))),
        }
    }
}

fn pick_category(ui: &mut Ui, id: &str, current: &mut Category) {
    ComboBox::from_id_salt(id)
        .selected_text(current.label())
        .show_ui(ui, |ui| {
            for choice in Category::ALL {
                ui.selectable_value(current, choice, choice.label());
            }
        });
}
