#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod desktop;

fn main() -> eframe::Result<()> {
    let start_host = std::env::args_os().any(|argument| argument == "--start-host");
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1120.0, 780.0])
            .with_min_inner_size([720.0, 540.0]),
        ..Default::default()
    };
    eframe::run_native(
        "CK3-LLM",
        options,
        Box::new(move |context| {
            Ok(Box::new(desktop::Desktop::new(
                &context.egui_ctx,
                start_host,
            )))
        }),
    )
}

use eframe::egui;
