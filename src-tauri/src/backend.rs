use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager};
use uuid::Uuid;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Connection {
    pub base_url: String,
    pub token: String,
    pub data_dir: String,
}

#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Preferences {
    data_directory: Option<PathBuf>,
}

#[derive(Default)]
pub struct Backend {
    connection: Arc<Mutex<Option<Result<Connection, String>>>>,
    child: Arc<Mutex<Option<Child>>>,
    generation: Arc<AtomicU64>,
    #[cfg(windows)]
    job: Mutex<Option<ProcessJob>>,
}

#[derive(Deserialize)]
struct Ready {
    event: String,
    port: u16,
    version: String,
}

fn preferences_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|p| p.join("desktop.json"))
        .map_err(|e| e.to_string())
}

pub fn data_directory(app: &AppHandle) -> Result<PathBuf, String> {
    let path = preferences_path(app)?;
    if path.exists() {
        let bytes = fs::read(path).map_err(|e| format!("读取数据目录配置失败：{e}"))?;
        let preferences: Preferences = serde_json::from_slice(&bytes)
            .map_err(|e| format!("数据目录配置损坏，原文件已保留：{e}"))?;
        if let Some(dir) = preferences.data_directory {
            return Ok(dir);
        }
    }
    app.path().app_data_dir().map_err(|e| e.to_string())
}

pub fn choose_directory(app: &AppHandle, selected: String) -> Result<String, String> {
    let requested = PathBuf::from(selected);
    if !requested.is_absolute() || !requested.is_dir() {
        return Err("请选择一个已存在的本地文件夹".into());
    }
    let selected = requested.canonicalize().map_err(|e| e.to_string())?;
    let probe = selected.join(format!(".supertravel-write-{}.tmp", Uuid::new_v4()));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .map_err(|_| "所选目录不可写，请选择其他文件夹".to_string())?;
    file.write_all(b"supertravel directory probe")
        .and_then(|_| file.sync_all())
        .map_err(|e| e.to_string())?;
    drop(file);
    fs::remove_file(probe).map_err(|e| e.to_string())?;
    let path = preferences_path(app)?;
    fs::create_dir_all(path.parent().ok_or("配置目录无效")?).map_err(|e| e.to_string())?;
    let encoded = serde_json::to_vec_pretty(&Preferences {
        data_directory: Some(selected.clone()),
    })
    .map_err(|e| e.to_string())?;
    // A new folder only takes effect after restart; existing conversations are never moved or deleted.
    let temporary = path.with_extension("json.tmp");
    let mut file = fs::File::create(&temporary).map_err(|e| e.to_string())?;
    file.write_all(&encoded)
        .and_then(|_| file.sync_all())
        .map_err(|e| e.to_string())?;
    drop(file);
    if path.exists() {
        fs::copy(&path, path.with_extension("json.previous")).map_err(|e| e.to_string())?;
    }
    fs::rename(&temporary, &path).map_err(|e| format!("保存目录设置失败，原配置已保留：{e}"))?;
    Ok(selected.to_string_lossy().into_owned())
}

fn backend_command() -> Result<Command, String> {
    #[cfg(debug_assertions)]
    {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("工程目录无效")?;
        let venv = root.join(if cfg!(windows) {
            "backend/.venv/Scripts/python.exe"
        } else {
            "backend/.venv/bin/python"
        });
        let python = std::env::var_os("STA_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if venv.exists() {
                    venv
                } else {
                    PathBuf::from("python")
                }
            });
        let mut command = Command::new(python);
        command.arg(root.join("backend/main.py"));
        Ok(command)
    }
    #[cfg(not(debug_assertions))]
    {
        let executable = std::env::current_exe().map_err(|e| e.to_string())?;
        let directory = executable.parent().ok_or("无法定位应用安装目录")?;
        let sidecar = directory.join(if cfg!(windows) {
            "supertravel-backend.exe"
        } else {
            "supertravel-backend"
        });
        if !sidecar.is_file() {
            return Err("安装包缺少 Python 服务，请重新安装完整客户端".into());
        }
        Ok(Command::new(sidecar))
    }
}

impl Backend {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        self.stop();
        let this_generation = self.generation.load(Ordering::SeqCst);
        *self.connection.lock().map_err(|_| "后端状态锁异常")? = None;
        let data_dir = data_directory(app)?;
        fs::create_dir_all(&data_dir).map_err(|e| format!("无法创建用户数据目录：{e}"))?;
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let version = app.package_info().version.to_string();
        let mut command = backend_command()?;
        #[cfg(not(debug_assertions))]
        command.env("STA_SHARED_TOOLS_URL", env!("STA_SHARED_TOOLS_URL"));
        command
            .args(["--host", "127.0.0.1", "--port", "0", "--data-dir"])
            .arg(&data_dir)
            .env("STA_BACKEND_TOKEN", &token)
            .env("STA_APP_VERSION", &version)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let mut process = command
            .spawn()
            .map_err(|e| format!("Python 服务启动失败：{e}"))?;
        #[cfg(windows)]
        match ProcessJob::attach(&process) {
            Ok(job) => *self.job.lock().map_err(|_| "进程状态锁异常")? = Some(job),
            Err(error) => {
                let _ = process.kill();
                let _ = process.wait();
                return Err(error);
            }
        }
        let stdout = process.stdout.take().ok_or("无法读取后端启动状态")?;
        *self.child.lock().map_err(|_| "进程状态锁异常")? = Some(process);
        let status = Arc::clone(&self.connection);
        let generation = Arc::clone(&self.generation);
        let (send, receive) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(ready) = serde_json::from_str::<Ready>(&line) {
                    if ready.event == "ready" {
                        let _ = send.send(ready);
                    }
                }
                // Raw backend stdout is never forwarded to the renderer or persistent logs.
            }
            if generation.load(Ordering::SeqCst) == this_generation {
                if let Ok(mut slot) = status.lock() {
                    *slot = Some(Err("本地服务已停止，请重新启动应用".into()));
                }
            }
        });
        let status = Arc::clone(&self.connection);
        let child = Arc::clone(&self.child);
        let generation = Arc::clone(&self.generation);
        let app = app.clone();
        thread::spawn(move || {
            let result = receive
                .recv_timeout(Duration::from_secs(45))
                .map_err(|_| "本地服务未在 45 秒内准备就绪".to_string())
                .and_then(|ready| {
                    if ready.port == 0 || ready.version != version {
                        return Err("后端版本或启动端口不匹配".into());
                    }
                    Ok(Connection {
                        base_url: format!("http://127.0.0.1:{}", ready.port),
                        token,
                        data_dir: data_dir.to_string_lossy().into_owned(),
                    })
                });
            if generation.load(Ordering::SeqCst) != this_generation {
                return;
            }
            if let Ok(connection) = &result {
                tauri::async_runtime::block_on(crate::model_settings::sync_on_start(
                    &app, connection,
                ));
            }
            if generation.load(Ordering::SeqCst) != this_generation {
                return;
            }
            if result.is_err() {
                if let Ok(mut process) = child.lock() {
                    if let Some(mut process) = process.take() {
                        drop(process.stdin.take());
                        let _ = process.kill();
                        let _ = process.wait();
                    }
                }
            }
            if let Ok(mut slot) = status.lock() {
                *slot = Some(result);
            }
        });
        Ok(())
    }

    pub fn connection(&self) -> Result<Connection, String> {
        self.connection
            .lock()
            .map_err(|_| "后端状态锁异常")?
            .clone()
            .unwrap_or_else(|| Err("BACKEND_STARTING".into()))
    }

    pub fn set_error(&self, error: String) {
        if let Ok(mut slot) = self.connection.lock() {
            *slot = Some(Err(error));
        }
    }

    pub fn stop(&self) {
        self.generation.fetch_add(1, Ordering::SeqCst);
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut process) = slot.take() {
                drop(process.stdin.take()); // EOF requests uvicorn to flush and shut down gracefully.
                let deadline = Instant::now() + Duration::from_secs(5);
                while Instant::now() < deadline {
                    match process.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) => thread::sleep(Duration::from_millis(50)),
                        Err(_) => break,
                    }
                }
                if matches!(process.try_wait(), Ok(None)) {
                    let _ = process.kill();
                }
                let _ = process.wait();
            }
        }
        #[cfg(windows)]
        if let Ok(mut job) = self.job.lock() {
            job.take();
        }
        self.set_error("本地服务已停止".into());
    }
}

#[cfg(windows)]
struct ProcessJob(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
unsafe impl Send for ProcessJob {}
#[cfg(windows)]
impl ProcessJob {
    fn attach(child: &Child) -> Result<Self, String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err("无法创建后端进程保护组".into());
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as _,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
                || AssignProcessToJobObject(handle, child.as_raw_handle() as _) == 0
            {
                CloseHandle(handle);
                return Err("无法启用后端退出保护；已停止启动以避免遗留后台服务".into());
            }
            Ok(Self(handle))
        }
    }
}
#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
