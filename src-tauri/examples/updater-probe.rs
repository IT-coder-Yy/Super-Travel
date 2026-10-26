//! CI-only native updater driver. Never included in the packaged application.
//! Installs into an isolated copy of the published old app, not the running driver.
use serde::Deserialize;
use std::{path::PathBuf, time::Duration};
use tauri_plugin_updater::{Error, UpdaterExt};

#[derive(Clone, Deserialize)]
struct Probe {
    executable: PathBuf,
    endpoint: url::Url,
    public_key: String,
    platform: String,
    old_version: String,
    new_version: String,
    mode: String,
}

async fn exercise(app: tauri::AppHandle, probe: Probe) -> Result<(), String> {
    let update = app
        .updater_builder()
        .executable_path(&probe.executable)
        .target(&probe.platform)
        .clear_headers()
        .no_proxy()
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())?
        .ok_or("No newer release found by the native updater")?;
    if update.version != probe.new_version {
        return Err("Unexpected update version".into());
    }
    let download = update.download(|_, _| {}, || {}).await;
    match (probe.mode.as_str(), download) {
        ("tampered", Err(Error::Minisign(_))) => {
            println!("PASS: native signature verification rejected the corrupted package")
        }
        ("interrupted", Err(Error::Reqwest(_))) => {
            println!("PASS: native downloader rejected the truncated response")
        }
        ("success", Ok(bytes)) => {
            update.install(bytes).map_err(|error| error.to_string())?;
            println!(
                "PASS: native updater installed {} over {}",
                probe.new_version, probe.old_version
            );
        }
        (mode, Err(error)) => return Err(format!("Unexpected {mode} error: {error}")),
        (mode, Ok(_)) => return Err(format!("The {mode} download should have failed")),
    }
    println!("SUPERTRAVEL_UPDATE_PROBE_OK:{}", probe.mode);
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = std::env::args_os()
        .nth(1)
        .ok_or("Usage: updater-probe <probe.json>")?;
    let probe: Probe = serde_json::from_slice(&std::fs::read(config)?)?;
    if !cfg!(any(target_os = "macos", target_os = "linux"))
        || probe.endpoint.scheme() != "http"
        || probe.endpoint.host_str() != Some("127.0.0.1")
        || !probe.executable.is_absolute()
        || !probe.executable.is_file()
        || !["tampered", "interrupted", "success"].contains(&probe.mode.as_str())
    {
        return Err(
            "Probe requires an existing isolated macOS/Linux install and a loopback test endpoint"
                .into(),
        );
    }
    let mut context = tauri::generate_context!();
    context.package_info_mut().version = probe.old_version.parse()?;
    context.config_mut().app.windows.clear();
    context.config_mut().plugins.0.insert(
        "updater".into(),
        serde_json::json!({
            "pubkey": probe.public_key, "endpoints": [probe.endpoint],
            "requireSignedVersion": true, "allowDowngrades": false,
            "dangerousInsecureTransportProtocol": true
        }),
    );
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(move |app| {
            let handle = app.handle().clone();
            let probe = probe.clone();
            tauri::async_runtime::spawn(async move {
                let result = exercise(handle.clone(), probe).await;
                if let Err(error) = &result {
                    eprintln!("Native update probe failed: {error}");
                }
                handle.exit(if result.is_ok() { 0 } else { 1 });
            });
            Ok(())
        })
        .build(context)?
        .run(|_, _| {});
    Ok(())
}
