use std::collections::HashMap;

fn main() {
    // The app keeps the local console cover: the same `AI网关控制台.ico` the
    // local console ships, embedded as the executable icon.
    println!("cargo:rerun-if-changed=assets/AI网关控制台.ico");
    println!("cargo:rerun-if-changed=assets/app.rc");
    #[cfg(windows)]
    embed_resource::compile("assets/app.rc", embed_resource::NONE);

    let config = slint_build::CompilerConfiguration::new().with_library_paths(HashMap::from([
        ("slintplot".to_string(), slintplot::library_path()),
    ]));
    slint_build::compile_with_config("ui/main.slint", config).expect("compile the desktop shell UI");
}
