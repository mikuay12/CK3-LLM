use std::path::PathBuf;
use std::sync::Arc;

use eframe::egui::{
    self, Button, Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Frame,
    Margin, RichText, Stroke, TextStyle, Ui,
};

pub const WHITE: Color32 = Color32::WHITE;
pub const SURFACE: Color32 = Color32::from_rgb(246, 248, 251);
pub const TEXT: Color32 = Color32::from_rgb(27, 39, 57);
pub const MUTED: Color32 = Color32::from_rgb(83, 98, 117);
pub const BORDER: Color32 = Color32::from_rgb(220, 226, 235);
pub const BLUE: Color32 = Color32::from_rgb(30, 89, 170);
pub const BLUE_INK: Color32 = Color32::from_rgb(19, 66, 135);
pub const BLUE_TINT: Color32 = Color32::from_rgb(232, 240, 253);
pub const GREEN: Color32 = Color32::from_rgb(23, 108, 70);
pub const ERROR: Color32 = Color32::from_rgb(171, 45, 47);

pub fn configure(context: &egui::Context) {
    let mut visuals = egui::Visuals::light();
    visuals.override_text_color = Some(TEXT);
    visuals.extreme_bg_color = WHITE;
    visuals.faint_bg_color = SURFACE;
    visuals.selection.bg_fill = BLUE_TINT;
    visuals.selection.stroke = Stroke::new(1.0_f32, BLUE);
    visuals.widgets.inactive.bg_fill = SURFACE;
    visuals.widgets.inactive.weak_bg_fill = SURFACE;
    visuals.widgets.inactive.bg_stroke = Stroke::new(1.0_f32, BORDER);
    context.set_visuals(visuals);
    context.style_mut(|style| {
        style.spacing.item_spacing = egui::vec2(9.0, 8.0);
        style.spacing.button_padding = egui::vec2(12.0, 7.0);
        style.spacing.interact_size.y = 30.0;
        for text in [TextStyle::Body, TextStyle::Button] {
            style.text_styles.insert(text, FontId::proportional(15.0));
        }
    });

    if let Some(system_root) = std::env::var_os("WINDIR") {
        let directory = PathBuf::from(system_root).join("Fonts");
        let font = ["msyh.ttc", "simsun.ttc", "msjh.ttc"]
            .into_iter()
            .find_map(|font| std::fs::read(directory.join(font)).ok());
        if let Some(font) = font {
            let mut fonts = FontDefinitions::default();
            let name = "windows-cjk".to_owned();
            fonts
                .font_data
                .insert(name.clone(), Arc::new(FontData::from_owned(font)));
            for family in [FontFamily::Proportional, FontFamily::Monospace] {
                fonts.families.entry(family).or_default().push(name.clone());
            }
            context.set_fonts(fonts);
        }
    }
}

pub fn card(ui: &mut Ui, content: impl FnOnce(&mut Ui)) {
    Frame::new()
        .fill(WHITE)
        .stroke(Stroke::new(1.0_f32, BORDER))
        .corner_radius(CornerRadius::same(13))
        .inner_margin(Margin::same(20))
        .show(ui, |ui| {
            ui.set_min_width(ui.available_width());
            content(ui);
        });
}

pub fn primary(title: &str) -> Button<'_> {
    Button::new(RichText::new(title).color(WHITE).strong())
        .fill(BLUE)
        .stroke(Stroke::NONE)
}
