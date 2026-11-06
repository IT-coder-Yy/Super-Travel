use serde::Serialize;
use std::{fs, io::Write, path::Path};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, FilePath};
use uuid::Uuid;

const MAX_EXPORT_BYTES: usize = 10 * 1024 * 1024;

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "lowercase")]
pub enum SaveResult {
    Saved { path: String },
    Cancelled,
}

fn validate_export(filename: &str, content: &[u8]) -> Result<(), String> {
    let stem = filename.split('.').next().unwrap_or("").to_uppercase();
    let device_name = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ["COM", "LPT"].iter().any(|prefix| {
            stem.strip_prefix(prefix).is_some_and(|number| {
                matches!(
                    number,
                    "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                )
            })
        });
    if filename.trim().is_empty()
        || filename.len() > 240
        || filename.ends_with(['.', ' '])
        || filename
            .chars()
            .any(|value| value.is_control() || "<>:\"/\\|?*".contains(value))
        || device_name
    {
        return Err("导出文件名无效，请使用不含路径或特殊字符的文件名。".into());
    }
    if content.len() > MAX_EXPORT_BYTES {
        return Err("导出内容超过 10 MiB，请减少行程内容后重试。".into());
    }
    Ok(())
}

fn write_export(path: &Path, content: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or("保存位置无效，请选择本地文件夹。")?;
    let temporary = parent.join(format!(".supertravel-export-{}.tmp", Uuid::new_v4()));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| format!("无法创建导出文件，请检查目录权限或更换保存位置：{error}"))?;
    let result = (|| {
        file.write_all(content)
            .and_then(|_| file.sync_all())
            .map_err(|error| {
                format!("写入导出文件失败，请检查磁盘空间和权限；原文件已保留：{error}")
            })?;
        drop(file);
        // Replace only after the complete file has been flushed beside the destination.
        fs::rename(&temporary, path).map_err(|error| {
            format!("无法替换导出文件，请关闭占用它的程序或更换保存位置；原文件已保留：{error}")
        })
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

fn save_selection(selected: Option<FilePath>, content: &[u8]) -> Result<SaveResult, String> {
    let Some(selected) = selected else {
        return Ok(SaveResult::Cancelled);
    };
    let path = selected
        .into_path()
        .map_err(|_| "请选择本地文件保存行程。")?;
    write_export(&path, content)?;
    Ok(SaveResult::Saved {
        path: path.to_string_lossy().into_owned(),
    })
}

async fn save_export(
    app: AppHandle,
    filename: String,
    content: Vec<u8>,
    label: &'static str,
    extension: &'static str,
) -> Result<SaveResult, String> {
    validate_export(&filename, &content)?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app
            .dialog()
            .file()
            .set_title(format!("导出行程 {label}"))
            .set_file_name(filename)
            .add_filter(label, &[extension]);
        if let Some(window) = app.get_webview_window("main") {
            dialog = dialog.set_parent(&window);
        }
        save_selection(dialog.blocking_save_file(), &content)
    })
    .await
    .map_err(|_| "无法完成行程导出，请重试。".to_string())?
}

#[tauri::command]
pub async fn save_markdown(
    app: AppHandle,
    filename: String,
    markdown: String,
) -> Result<SaveResult, String> {
    save_export(app, filename, markdown.into_bytes(), "Markdown", "md").await
}

fn validate_pdf(content: &[u8]) -> Result<(), String> {
    if !content.starts_with(b"%PDF-") {
        return Err("PDF 内容无效，请重新生成后保存。".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn save_pdf(
    app: AppHandle,
    filename: String,
    bytes: Vec<u8>,
) -> Result<SaveResult, String> {
    validate_pdf(&bytes)?;
    save_export(app, filename, bytes, "PDF", "pdf").await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn directory() -> PathBuf {
        let path = std::env::temp_dir().join(format!("supertravel-export-test-{}", Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        path
    }

    #[test]
    fn rejects_unsafe_names_and_limits_utf8_bytes() {
        for name in [
            "",
            " ",
            ".",
            "..",
            "../trip.md",
            "C:\\trip.md",
            "a/b.md",
            "a\n.md",
            "a\u{7f}.md",
            "a.md ",
            "a.",
            "CON.md",
            "Lpt9.md",
            "COM¹.md",
        ] {
            assert!(
                validate_export(name, "行程".as_bytes()).is_err(),
                "{name:?}"
            );
        }
        assert!(validate_export(&format!("{}.md", "旅".repeat(80)), b"").is_err());
        assert!(validate_export("杭州 行程.md", b"").is_ok());
        let content = "旅".repeat(MAX_EXPORT_BYTES / 3) + "a";
        assert_eq!(content.len(), MAX_EXPORT_BYTES);
        assert!(validate_export("行程.md", content.as_bytes()).is_ok());
        assert!(validate_export("行程.md", (content + "a").as_bytes()).is_err());
    }

    #[test]
    fn cancellation_does_not_write() {
        let result = save_selection(None, "# 保留中文\r\n  空格\n".as_bytes()).unwrap();
        assert_eq!(
            serde_json::to_value(result).unwrap(),
            serde_json::json!({ "status": "cancelled" })
        );
    }

    #[test]
    fn saves_exact_utf8_and_replaces_existing_file_at_unicode_path() {
        let directory = directory();
        let path = directory.join("杭州 行程.md");
        let markdown = "# 杭州 🌸\r\n\n- 上午  西湖\n";
        let result = save_selection(Some(path.clone().into()), markdown.as_bytes()).unwrap();
        assert_eq!(
            serde_json::to_value(result).unwrap(),
            serde_json::json!({ "status": "saved", "path": path.to_string_lossy() })
        );
        assert_eq!(fs::read(&path).unwrap(), markdown.as_bytes());
        write_export(&path, "短\n".as_bytes()).unwrap();
        assert_eq!(fs::read(&path).unwrap(), "短\n".as_bytes());
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 1);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn failed_replace_cleans_temporary_and_preserves_destination() {
        let directory = directory();
        let path = directory.join("已有内容.md");
        fs::create_dir(&path).unwrap();
        let existing = path.join("保留.txt");
        fs::write(&existing, "原始数据").unwrap();
        assert!(write_export(&path, b"new")
            .unwrap_err()
            .contains("原文件已保留"));
        assert_eq!(fs::read_to_string(&existing).unwrap(), "原始数据");
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 1);
        fs::remove_file(existing).unwrap();
        fs::remove_dir(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn saves_pdf_binary_exactly_and_rejects_wrong_header() {
        assert!(validate_pdf(b"not a PDF").is_err());
        let content = b"%PDF-1.4\n%\xff\xfe\x00\x80\n%%EOF";
        validate_pdf(content).unwrap();
        let directory = directory();
        let path = directory.join("杭州 含空格.pdf");
        save_selection(Some(path.clone().into()), content).unwrap();
        assert_eq!(fs::read(&path).unwrap(), content);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn locked_existing_file_is_unchanged_on_failure() {
        use std::os::windows::fs::OpenOptionsExt;
        let directory = directory();
        let path = directory.join("占用 行程.md");
        fs::write(&path, "旧行程").unwrap();
        let lock = fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&path)
            .unwrap();
        assert!(write_export(&path, "新行程".as_bytes())
            .unwrap_err()
            .contains("原文件已保留"));
        drop(lock);
        assert_eq!(fs::read_to_string(&path).unwrap(), "旧行程");
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 1);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
