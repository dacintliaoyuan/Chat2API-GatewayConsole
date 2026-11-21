use std::collections::HashMap;

fn main() {
    let config = slint_build::CompilerConfiguration::new().with_library_paths(HashMap::from([
        ("slintplot".to_string(), slintplot::library_path()),
    ]));
    slint_build::compile_with_config("ui/main.slint", config).expect("compile the desktop shell UI");
}
