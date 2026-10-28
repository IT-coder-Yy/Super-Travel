fn main() {
    println!("cargo:rerun-if-env-changed=STA_SHARED_TOOLS_URL");
    println!(
        "cargo:rustc-env=STA_SHARED_TOOLS_URL={}",
        std::env::var("STA_SHARED_TOOLS_URL").unwrap_or_default()
    );
    println!(
        "cargo:rustc-env=STA_TARGET_TRIPLE={}",
        std::env::var("TARGET").unwrap()
    );
    println!("cargo:rerun-if-env-changed=STA_UPDATE_MIRROR");
    println!(
        "cargo:rustc-env=STA_UPDATE_MIRROR={}",
        std::env::var("STA_UPDATE_MIRROR").unwrap_or_else(|_| "https://gh-proxy.com/".into())
    );
    tauri_build::build();
    if std::env::var("TARGET").unwrap().ends_with("windows-msvc") {
        // tauri-build links its Common Controls v6 manifest to binaries, not examples.
        let resource =
            std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("resource.lib");
        println!("cargo:rustc-link-arg-examples={}", resource.display());
    }
}
