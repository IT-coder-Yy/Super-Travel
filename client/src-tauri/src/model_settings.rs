use crate::backend::{Backend, Connection};
use keyring::{Entry, Error as KeyringError};
use serde::{Deserialize, Serialize};
use std::{fs, io::Write, path::PathBuf, time::Duration};
use tauri::{AppHandle, Manager, State};
use tokio::sync::Mutex;
use url::Url;

const STORAGE_ERROR: &str =
    "无法访问系统凭据库，请解锁系统钥匙串或启用 Secret Service 后重启应用；API Key 不会保存到本地文件";
const SYNC_ERROR: &str = "模型设置已保存，但未能同步到本地服务，请重新保存设置或重启应用";

#[derive(Default)]
pub struct ModelSettings(Mutex<Option<String>>);

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Preferences {
    base_url: String,
    model: String,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1".into(),
            model: String::new(),
        }
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredKey {
    // Binding inside the secure store also protects against an interrupted JSON write.
    base_url: String,
    api_key: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelStatus {
    base_url: String,
    model: String,
    has_key: bool,
    storage_available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    sync_error: Option<String>,
}

fn normalize(base_url: String, model: String) -> Result<Preferences, String> {
    let value = base_url.trim();
    let url = Url::parse(value).map_err(|_| "请输入有效的 HTTPS 模型服务地址")?;
    if value.len() > 2048
        || value.chars().any(char::is_whitespace)
        || url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("模型服务地址必须使用 HTTPS，且不能包含账号、密码、查询参数或片段".into());
    }
    let model = model.trim().to_string();
    if model.len() > 200 || model.chars().any(char::is_control) {
        return Err("模型名称过长或包含无效字符".into());
    }
    Ok(Preferences {
        base_url: url.to_string().trim_end_matches('/').to_string(),
        model,
    })
}

fn preferences_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|path| path.join("model.json"))
        .map_err(|_| "无法定位模型配置目录".into())
}

fn read_preferences(app: &AppHandle) -> Result<Preferences, String> {
    match fs::read(preferences_path(app)?) {
        Ok(bytes) => {
            let saved: Preferences = serde_json::from_slice(&bytes)
                .map_err(|_| "模型配置损坏，原文件已保留，请检查 model.json")?;
            normalize(saved.base_url, saved.model)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Preferences::default()),
        Err(_) => Err("无法读取模型配置，请检查用户配置目录".into()),
    }
}

fn write_preferences(app: &AppHandle, preferences: &Preferences) -> Result<(), String> {
    let path = preferences_path(app)?;
    let temporary = path.with_extension("json.tmp");
    let result = (|| {
        fs::create_dir_all(path.parent().ok_or("模型配置目录无效")?)
            .map_err(|_| "无法创建模型配置目录")?;
        let bytes = serde_json::to_vec_pretty(preferences).map_err(|_| "无法编码模型配置")?;
        let mut file = fs::File::create(&temporary).map_err(|_| "无法创建模型配置文件")?;
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|_| "无法写入模型配置文件")?;
        drop(file);
        fs::rename(&temporary, &path).map_err(|_| "无法替换模型配置，原文件已保留")
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result.map_err(str::to_string)
}

fn entry() -> Result<Entry, String> {
    #[cfg(not(feature = "native-gui-tests"))]
    const SERVICE: &str = "com.supertravel.desktop";
    #[cfg(feature = "native-gui-tests")]
    const SERVICE: &str = "com.supertravel.desktop.native-gui-tests";
    Entry::new(SERVICE, "model-api-key").map_err(|_| STORAGE_ERROR.into())
}

fn read_key() -> Result<Option<StoredKey>, String> {
    match entry()?.get_password() {
        Ok(secret) => serde_json::from_str(&secret)
            .map(Some)
            .map_err(|_| "系统凭据格式无效，请删除已保存的 API Key 后重新填写".into()),
        Err(KeyringError::NoEntry) => Ok(None),
        Err(_) => Err(STORAGE_ERROR.into()),
    }
}

fn write_key(key: Option<&StoredKey>) -> Result<(), String> {
    let entry = entry()?;
    match key {
        Some(key) => entry
            .set_password(&serde_json::to_string(key).map_err(|_| "无法编码系统凭据")?)
            .map_err(|_| STORAGE_ERROR.into()),
        None => match entry.delete_credential() {
            Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
            Err(_) => Err(STORAGE_ERROR.into()),
        },
    }
}

fn select_key(
    preferences: &Preferences,
    previous: Option<StoredKey>,
    supplied: Option<String>,
) -> Result<Option<StoredKey>, String> {
    if let Some(value) = supplied.filter(|value| !value.trim().is_empty()) {
        let api_key = value.trim().to_string();
        if api_key.len() > 4096
            || !api_key.is_ascii()
            || api_key
                .chars()
                .any(|value| value.is_whitespace() || value.is_control())
        {
            return Err("API Key 格式无效，请检查是否粘贴了额外内容".into());
        }
        return Ok(Some(StoredKey {
            base_url: preferences.base_url.clone(),
            api_key,
        }));
    }
    if previous
        .as_ref()
        .is_some_and(|key| key.base_url != preferences.base_url)
    {
        return Err("修改模型服务地址时，请明确填写新 API Key，或先删除已保存的 Key".into());
    }
    Ok(previous)
}

fn status(preferences: Preferences, key: Result<Option<StoredKey>, String>) -> ModelStatus {
    let has_key = key
        .as_ref()
        .ok()
        .and_then(Option::as_ref)
        .is_some_and(|key| key.base_url == preferences.base_url && !key.api_key.is_empty());
    ModelStatus {
        base_url: preferences.base_url,
        model: preferences.model,
        has_key,
        storage_available: key.is_ok(),
        sync_error: None,
    }
}

fn local_client() -> Result<reqwest::Client, String> {
    // The updater enables reqwest's rustls-no-provider feature; model sync may run first.
    let _ = rustls::crypto::ring::default_provider().install_default();
    reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|_| SYNC_ERROR.into())
}

async fn configure_backend(
    connection: &Connection,
    preferences: &Preferences,
    key: Option<&StoredKey>,
) -> Result<(), String> {
    let url = Url::parse(&connection.base_url).map_err(|_| SYNC_ERROR)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(SYNC_ERROR.into());
    }
    let api_key = key
        .filter(|key| key.base_url == preferences.base_url && !preferences.model.is_empty())
        .map(|key| key.api_key.as_str())
        .unwrap_or("");
    #[derive(Deserialize)]
    struct Configured {
        configured: bool,
    }
    let response = local_client()?
        .post(url.join("api/model/configure").map_err(|_| SYNC_ERROR)?)
        .bearer_auth(&connection.token)
        .json(&serde_json::json!({
            "base_url": preferences.base_url,
            "model": preferences.model,
            "api_key": api_key,
        }))
        .send()
        .await
        .map_err(|_| SYNC_ERROR)?;
    if !response.status().is_success() {
        return Err(SYNC_ERROR.into());
    }
    let result: Configured = response.json().await.map_err(|_| SYNC_ERROR)?;
    if result.configured != !api_key.is_empty() {
        return Err(SYNC_ERROR.into());
    }
    Ok(())
}

pub async fn sync_on_start(app: &AppHandle, connection: &Connection) {
    let state = app.state::<ModelSettings>();
    let mut sync_error = state.0.lock().await;
    let handle = app.clone();
    let loaded = tauri::async_runtime::spawn_blocking(move || {
        Ok::<_, String>((read_preferences(&handle)?, read_key()?))
    })
    .await;
    let result = match loaded {
        Ok(Ok((preferences, key))) => {
            configure_backend(connection, &preferences, key.as_ref()).await
        }
        Ok(Err(error)) => Err(error),
        Err(_) => Err("无法读取模型设置，请重启应用后重试".into()),
    };
    *sync_error = result.err();
}

#[tauri::command]
pub async fn get_model_settings(
    app: AppHandle,
    state: State<'_, ModelSettings>,
) -> Result<ModelStatus, String> {
    let sync_error = state.0.lock().await;
    let mut result = tauri::async_runtime::spawn_blocking(move || {
        Ok::<_, String>(status(read_preferences(&app)?, read_key()))
    })
    .await
    .map_err(|_| "无法读取模型设置")??;
    result.sync_error = sync_error.clone();
    Ok(result)
}

#[tauri::command]
pub async fn save_model_settings(
    app: AppHandle,
    state: State<'_, ModelSettings>,
    base_url: String,
    model: String,
    api_key: Option<String>,
) -> Result<ModelStatus, String> {
    let mut sync_error = state.0.lock().await;
    let handle = app.clone();
    let (preferences, key) = tauri::async_runtime::spawn_blocking(move || {
        let preferences = normalize(base_url, model)?;
        if preferences.model.is_empty() {
            return Err("请填写模型名称".to_string());
        }
        let previous = read_key()?;
        let changed = api_key
            .as_ref()
            .is_some_and(|value| !value.trim().is_empty());
        let key = select_key(&preferences, previous.clone(), api_key)?;
        if changed {
            write_key(key.as_ref())?;
        }
        if let Err(error) = write_preferences(&handle, &preferences) {
            if changed && write_key(previous.as_ref()).is_err() {
                return Err("模型配置未保存，系统凭据也未能恢复，请重新填写模型设置".into());
            }
            return Err(error);
        }
        Ok::<_, String>((preferences, key))
    })
    .await
    .map_err(|_| "无法保存模型设置")??;
    let result = match app.state::<Backend>().connection() {
        Ok(connection) => configure_backend(&connection, &preferences, key.as_ref()).await,
        Err(_) => Err(SYNC_ERROR.into()),
    };
    *sync_error = result.clone().err();
    result?;
    Ok(status(preferences, Ok(key)))
}

#[tauri::command]
pub async fn delete_model_key(
    app: AppHandle,
    state: State<'_, ModelSettings>,
) -> Result<ModelStatus, String> {
    let mut sync_error = state.0.lock().await;
    let handle = app.clone();
    let preferences = tauri::async_runtime::spawn_blocking(move || {
        write_key(None)?;
        // A damaged non-secret JSON file must not prevent deleting a credential.
        Ok::<_, String>(read_preferences(&handle).unwrap_or_default())
    })
    .await
    .map_err(|_| "无法删除模型凭据")??;
    let result = match app.state::<Backend>().connection() {
        Ok(connection) => configure_backend(&connection, &preferences, None).await,
        Err(_) => Err(SYNC_ERROR.into()),
    };
    if result.is_err() {
        // Do not leave a deleted key usable in a backend that failed to clear it.
        let handle = app.clone();
        let _ =
            tauri::async_runtime::spawn_blocking(move || handle.state::<Backend>().stop()).await;
        let error =
            "API Key 已从系统凭据库删除；本地服务未能确认清空，已停止服务，请重启应用".to_string();
        *sync_error = Some(error.clone());
        return Err(error);
    }
    *sync_error = None;
    Ok(status(preferences, Ok(None)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_model_sync_does_not_require_a_prior_update_check() {
        assert!(local_client().is_ok());
    }

    #[test]
    fn endpoints_must_be_https_without_embedded_secrets() {
        for url in [
            "http://example.com/v1",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?key=secret",
            "https://example.com/v1#secret",
            "https://example.com/\npath",
        ] {
            assert!(normalize(url.into(), "model".into()).is_err());
        }
        let saved = normalize(" https://EXAMPLE.com:443/v1/ ".into(), " model ".into()).unwrap();
        assert_eq!(saved.base_url, "https://example.com/v1");
        assert_eq!(saved.model, "model");
    }

    #[test]
    fn blank_key_only_preserves_the_same_endpoint() {
        let preferences = normalize("https://new.example/v1".into(), "model".into()).unwrap();
        let old_key = StoredKey {
            base_url: "https://old.example/v1".into(),
            api_key: "test-secret".into(),
        };
        assert!(select_key(&preferences, Some(old_key.clone()), Some(" ".into())).is_err());
        let key = select_key(&preferences, Some(old_key), Some("new-secret".into()))
            .unwrap()
            .unwrap();
        assert_eq!(key.base_url, preferences.base_url);
        assert_eq!(key.api_key, "new-secret");
        assert_eq!(
            select_key(&preferences, Some(key), None)
                .unwrap()
                .unwrap()
                .api_key,
            "new-secret"
        );
        assert!(select_key(&preferences, None, None).unwrap().is_none());
    }

    #[test]
    fn status_and_json_never_return_the_key() {
        let preferences = Preferences::default();
        let stored = StoredKey {
            base_url: "https://different.example/v1".into(),
            api_key: "test-secret".into(),
        };
        let view = status(preferences.clone(), Ok(Some(stored)));
        assert!(!view.has_key);
        assert!(!serde_json::to_string(&view)
            .unwrap()
            .contains("test-secret"));
        assert!(!serde_json::to_string(&preferences)
            .unwrap()
            .contains("apiKey"));
        assert!(!status(preferences, Err(STORAGE_ERROR.into())).storage_available);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn backend_credentials_are_only_sent_to_a_loopback_port() {
        for base_url in [
            "https://example.com",
            "http://127.0.0.1",
            "http://user@127.0.0.1:9999",
        ] {
            let connection = Connection {
                base_url: base_url.into(),
                token: "test-token".into(),
                data_dir: String::new(),
            };
            assert_eq!(
                configure_backend(&connection, &Preferences::default(), None).await,
                Err(SYNC_ERROR.into())
            );
        }
    }
}
