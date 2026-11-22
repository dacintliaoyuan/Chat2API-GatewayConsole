use std::collections::HashMap;

fn main() {
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
