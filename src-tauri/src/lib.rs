mod backend;
mod model_settings;
mod updates;

use backend::{Backend, Connection};
use tauri::{Manager, State};
use updates::Updates;

#[tauri::command]
fn backend_connection(backend: State<'_, Backend>) -> Result<Connection, String> {
    backend.connection()
}

#[tauri::command]
fn app_version(app: tauri::AppHandle) -> String {
    app.package_info().version.to_string()
}

#[tauri::command]
fn set_data_directory(app: tauri::AppHandle, path: String) -> Result<String, String> {
    backend::choose_directory(&app, path)
}

pub fn run() {
    let builder = tauri::Builder::default();
    #[cfg(feature = "native-gui-tests")]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());
    let app = builder
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Backend::default())
        .manage(model_settings::ModelSettings::default())
        .manage(Updates::default())
        .setup(|app| {
            let backend = app.state::<Backend>();
            if let Err(error) = backend.start(app.handle()) {
                backend.set_error(error);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_connection,
            app_version,
            set_data_directory,
            model_settings::get_model_settings,
            model_settings::save_model_settings,
            model_settings::delete_model_key,
            updates::check_update,
            updates::install_update,
            updates::cancel_update
        ])
        .build(tauri::generate_context!())
        .expect("无法创建 SuperTravel 桌面应用");
    app.run(|app, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            app.state::<Updates>().cancel();
            app.state::<Backend>().stop();
        }
    });
}
