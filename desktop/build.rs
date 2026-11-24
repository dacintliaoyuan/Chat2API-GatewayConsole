use std::collections::HashMap;

fn main() {
    // Slint's generated layout code for the six-page shell recurses deep enough to
    // exhaust the 1 MB default main-thread stack while `MainWindow::new()` builds
    // the component tree. Reserve 32 MB for the app binary; the commit stays at
    // the default so the reservation is only address space.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rustc-link-arg-bins=/STACK:33554432,1048576");
    }
    // Match the Windows shortcut installed by deploy-local-gateway.ps1.
    println!("cargo:rerun-if-changed=../assets/UM云端控制台.ico");
    println!("cargo:rerun-if-changed=assets/app.rc");
    #[cfg(windows)]
    embed_resource::compile("assets/app.rc", embed_resource::NONE)
        .manifest_required()
        .expect("compile the Windows app icon");

    let config = slint_build::CompilerConfiguration::new().with_library_paths(HashMap::from([
        ("slintplot".to_string(), slintplot::library_path()),
    ]));
    slint_build::compile_with_config("ui/main.slint", config).expect("compile the desktop shell UI");
}
