use crate::backend::Backend;
use serde::Serialize;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};
use tokio::sync::Notify;
use url::Url;

#[derive(Default)]
pub struct Updates {
    pending: Mutex<Option<Update>>,
    busy: AtomicBool,
    installing: AtomicBool,
    cancelled: AtomicBool,
    wake_cancel: Notify,
}

impl Updates {
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        self.wake_cancel.notify_one();
    }
}

struct Busy<'a>(&'a Updates);
impl Drop for Busy<'_> {
    fn drop(&mut self) {
        self.0.busy.store(false, Ordering::SeqCst);
        self.0.installing.store(false, Ordering::SeqCst);
    }
}

#[derive(Serialize)]
pub struct CheckResult {
    configured: bool,
    version: Option<String>,
    body: Option<String>,
}

#[derive(Clone, Serialize)]
struct Progress {
    percent: Option<f64>,
    phase: &'static str,
    source: String,
}

fn configured(app: &AppHandle) -> bool {
    app.config().plugins.0.get("updater").is_some_and(|value| {
        value
            .get("pubkey")
            .and_then(|v| v.as_str())
            .is_some_and(|v| !v.is_empty())
            && value
                .get("endpoints")
                .and_then(|v| v.as_array())
                .is_some_and(|v| !v.is_empty())
    })
}

fn mirror_url(original: &Url) -> Option<Url> {
    // Never send backend/model credentials through an updater or a third-party mirror.
    if original.scheme() != "https"
        || original.host_str() != Some("github.com")
        || !original.path().contains("/releases/download/")
    {
        return None;
    }
    let prefix = env!("STA_UPDATE_MIRROR");
    let base = Url::parse(prefix).ok()?;
    if base.scheme() != "https"
        || base.host_str().is_none()
        || !base.username().is_empty()
        || base.password().is_some()
    {
        return None;
    }
    Url::parse(&format!(
        "{}{original}",
        if prefix.ends_with('/') {
            prefix.to_string()
        } else {
            format!("{prefix}/")
        }
    ))
    .ok()
}

#[tauri::command]
pub async fn check_update(
    app: AppHandle,
    state: State<'_, Updates>,
) -> Result<CheckResult, String> {
    if state.busy.swap(true, Ordering::SeqCst) {
        return Err("更新操作正在进行，请稍候".into());
    }
    let _busy = Busy(&state);
    *state.pending.lock().map_err(|_| "更新状态锁异常")? = None;
    if !configured(&app) {
        return Ok(CheckResult {
            configured: false,
            version: None,
            body: None,
        });
    }
    let exit_app = app.clone();
    let allow_local_http = app.config().plugins.0["updater"]
        .get("dangerousInsecureTransportProtocol")
        .and_then(|value| value.as_bool())
        .unwrap_or(false);
    let update = app
        .updater_builder()
        .clear_headers()
        .configure_client(move |client| client.https_only(!allow_local_http))
        .timeout(Duration::from_secs(30))
        .on_before_exit(move || exit_app.state::<Backend>().stop())
        .build()
        .map_err(|e| format!("更新配置无效：{e}"))?
        .check()
        .await
        .map_err(|e| format!("暂时无法检查新版本：{e}"))?;
    let result = CheckResult {
        configured: true,
        version: update.as_ref().map(|u| u.version.clone()),
        body: update.as_ref().and_then(|u| u.body.clone()),
    };
    *state.pending.lock().map_err(|_| "更新状态锁异常")? = update;
    Ok(result)
}

#[tauri::command]
pub fn cancel_update(state: State<'_, Updates>) -> Result<(), String> {
    if state.installing.load(Ordering::SeqCst) {
        return Err("已开始安装，请等待应用重新启动".into());
    }
    state.cancel();
    Ok(())
}

#[tauri::command]
pub async fn install_update(app: AppHandle, state: State<'_, Updates>) -> Result<(), String> {
    if state.busy.swap(true, Ordering::SeqCst) {
        return Err("更新操作正在进行，请稍候".into());
    }
    let _busy = Busy(&state);
    state.cancelled.store(false, Ordering::SeqCst);
    // Drain a cancellation left over from an earlier attempt.
    let _ = tokio::time::timeout(Duration::ZERO, state.wake_cancel.notified()).await;
    let update = state
        .pending
        .lock()
        .map_err(|_| "更新状态锁异常")?
        .clone()
        .ok_or("请先检查更新")?;
    let mut candidates = Vec::new();
    if let Some(url) = mirror_url(&update.download_url) {
        candidates.push(url);
    }
    candidates.push(update.download_url.clone());
    let mut bytes = None;
    let mut last_error = String::new();
    for url in candidates {
        if state.cancelled.load(Ordering::SeqCst) {
            return Err("下载已取消".into());
        }
        let mut candidate = update.clone();
        candidate.download_url = url;
        candidate.headers.clear();
        candidate.timeout = Some(Duration::from_secs(120));
        let source = candidate
            .download_url
            .host_str()
            .unwrap_or("更新服务器")
            .to_string();
        let _ = app.emit(
            "supertravel-update",
            Progress {
                percent: Some(0.0),
                phase: "downloading",
                source: source.clone(),
            },
        );
        let mut downloaded = 0u64;
        let result = tokio::select! {
            result = candidate.download(|chunk, total| {
                downloaded += chunk as u64;
                let percent = total.filter(|t| *t > 0).map(|total| (downloaded as f64 * 100.0 / total as f64).min(100.0));
                let _ = app.emit("supertravel-update", Progress { percent, phase: "downloading", source: source.clone() });
            }, || {}) => result.map_err(|e| e.to_string()),
            _ = state.wake_cancel.notified() => return Err("下载已取消".into()),
        };
        match result {
            Ok(download) => {
                bytes = Some(download);
                break;
            }
            Err(error) => last_error = error,
        }
    }
    let bytes = bytes
        .ok_or_else(|| format!("镜像与官方源均未取得可信更新包，当前版本保持不变：{last_error}"))?;
    if state.cancelled.load(Ordering::SeqCst) {
        return Err("下载已取消".into());
    }
    state.installing.store(true, Ordering::SeqCst);
    let _ = app.emit(
        "supertravel-update",
        Progress {
            percent: Some(100.0),
            phase: "installing",
            source: "verified".into(),
        },
    );
    // The caller saves edits before invoking this command; all backend writes are atomic.
    app.state::<Backend>().stop();
    if let Err(error) = update.install(bytes) {
        if let Err(start_error) = app.state::<Backend>().start(&app) {
            app.state::<Backend>().set_error(start_error);
        }
        return Err(format!("更新安装失败，已尝试恢复当前本地服务：{error}"));
    }
    app.restart();
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unsigned_build_has_valid_but_disabled_updater_config() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater: tauri_plugin_updater::Config =
            serde_json::from_value(config["plugins"]["updater"].clone()).unwrap();
        assert!(updater.pubkey.is_empty());
        assert!(updater.endpoints.is_empty());
    }

    #[test]
    fn mirror_only_accepts_public_github_release_assets() {
        assert!(mirror_url(
            &Url::parse("https://github.com/owner/repo/releases/download/v1/app.exe").unwrap()
        )
        .is_some());
        assert!(mirror_url(&Url::parse("https://example.com/app.exe").unwrap()).is_none());
        assert!(mirror_url(
            &Url::parse("http://github.com/owner/repo/releases/download/v1/app.exe").unwrap()
        )
        .is_none());
        assert!(mirror_url(&Url::parse("https://github.com/login").unwrap()).is_none());
    }
}
