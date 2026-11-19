slint::include_modules!();

fn main() -> Result<(), slint::PlatformError> {
    let window = OverviewPrototype::new()?;
    window.run()
}
