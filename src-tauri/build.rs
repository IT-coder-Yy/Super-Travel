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
    tauri_build::build()
}
