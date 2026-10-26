//! CI-only real OS credential test. Never reads or writes the production model entry.
use keyring::{Entry, Error};
use std::{fs, path::PathBuf};
use uuid::Uuid;

struct Cleanup<'a>(&'a Entry);

impl Drop for Cleanup<'_> {
    fn drop(&mut self) {
        if !matches!(self.0.delete_credential(), Ok(()) | Err(Error::NoEntry)) {
            eprintln!("Credential probe could not remove its temporary entry");
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let report = PathBuf::from(
        std::env::args_os()
            .nth(1)
            .ok_or("Usage: credential-probe <report.json>")?,
    );
    let backend = match std::env::consts::OS {
        "windows" => "Windows Credential Manager",
        "macos" => "macOS Keychain",
        "linux" => "D-Bus Secret Service",
        _ => return Err("Unsupported credential probe platform".into()),
    };
    Entry::store_status()
        .as_ref()
        .map_err(|_| "Native credential store initialization failed")?;
    let account = format!("ci-probe-{}", Uuid::new_v4());
    let service = "com.supertravel.desktop.credential-probe";
    let entry = Entry::new(service, &account).map_err(|_| "Could not create probe entry")?;
    if !matches!(entry.get_password(), Err(Error::NoEntry)) {
        return Err("Probe entry must not already exist".into());
    }
    let _cleanup = Cleanup(&entry);
    let first = format!("test-only-{}", Uuid::new_v4());
    entry
        .set_password(&first)
        .map_err(|_| "Native credential write failed")?;
    // A fresh Entry must read the value from the OS store, not a retained Rust value.
    let reopened = Entry::new(service, &account).map_err(|_| "Could not reopen probe entry")?;
    if reopened
        .get_password()
        .map_err(|_| "Native credential read failed")?
        != first
    {
        return Err("Native credential round-trip did not match".into());
    }
    let replacement = format!("replacement-test-only-{}", Uuid::new_v4());
    reopened
        .set_password(&replacement)
        .map_err(|_| "Native credential replacement failed")?;
    if entry
        .get_password()
        .map_err(|_| "Native credential replacement read failed")?
        != replacement
    {
        return Err("Native credential replacement did not match".into());
    }
    entry
        .delete_credential()
        .map_err(|_| "Native credential deletion failed")?;
    if !matches!(reopened.get_password(), Err(Error::NoEntry)) {
        return Err("Deleted probe credential is still accessible".into());
    }
    fs::write(
        report,
        serde_json::to_vec_pretty(&serde_json::json!({
            "platform": std::env::consts::OS,
            "backend": backend,
            "write_read_replace_delete": "passed",
            "production_entry_accessed": false,
            "secret_values_logged": false,
        }))?,
    )?;
    println!("SUPERTRAVEL_CREDENTIAL_PROBE_OK:{}", std::env::consts::OS);
    Ok(())
}
