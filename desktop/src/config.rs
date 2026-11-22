//! Saved desktop settings: `%LOCALAPPDATA%\Chat2API\desktop.json`.
//!
//! Stores the instance's model API port, whether first-run login was deferred,
//! the theme and the page filters that must survive a restart.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::ui::MainWindow;

pub const DEFAULT_MODEL_API_PORT: u16 = 7999;
pub const MODEL_API_HOST: &str = "127.0.0.1";
pub const CONFIG_VERSION: i64 = 2;

/// The overview auto-refresh interval. `Off` stops the timer without changing the
/// manual refresh action.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum AutoRefresh {
    #[default]
    Five,
    Fifteen,
    Thirty,
    Sixty,
    Off,
}

impl AutoRefresh {
    pub fn seconds(self) -> i32 {
        match self {
            Self::Five => 5,
            Self::Fifteen => 15,
            Self::Thirty => 30,
            Self::Sixty => 60,
            Self::Off => 0,
        }
    }

    pub fn from_seconds(seconds: i32) -> Option<Self> {
        match seconds {
            5 => Some(Self::Five),
            15 => Some(Self::Fifteen),
            30 => Some(Self::Thirty),
            60 => Some(Self::Sixty),
            0 => Some(Self::Off),
            _ => None,
        }
    }

    /// The segmented-control index used by the overview chart.
    pub fn index(self) -> i32 {
        match self {
            Self::Five => 0,
            Self::Fifteen => 1,
            Self::Thirty => 2,
            Self::Sixty => 3,
            Self::Off => 4,
        }
    }
}

/// The usage/request time range. Both pages share the same five buckets.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum TimeRange {
    #[default]
    Today,
    Day,
    Week,
    Month,
    All,
}

impl TimeRange {
    pub fn index(self) -> i32 {
        match self {
            Self::Today => 0,
            Self::Day => 1,
            Self::Week => 2,
            Self::Month => 3,
            Self::All => 4,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Day,
            2 => Self::Week,
            3 => Self::Month,
            4 => Self::All,
            _ => Self::Today,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::Today => "today",
            Self::Day => "24h",
            Self::Week => "7d",
            Self::Month => "30d",
            Self::All => "all",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "today" => Some(Self::Today),
            "24h" => Some(Self::Day),
            "7d" => Some(Self::Week),
            "30d" => Some(Self::Month),
            "all" => Some(Self::All),
            _ => None,
        }
    }
}

/// The request-result filter. The six result kinds match the request page's
/// dropdown; `All` is the first entry and the default.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum RequestResult {
    #[default]
    All,
    Success,
    Failed,
    Truncated,
    ClientDisconnected,
    Cancelled,
    Unknown,
}

impl RequestResult {
    pub fn index(self) -> i32 {
        match self {
            Self::All => 0,
            Self::Success => 1,
            Self::Failed => 2,
            Self::Truncated => 3,
            Self::ClientDisconnected => 4,
            Self::Cancelled => 5,
            Self::Unknown => 6,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Success,
            2 => Self::Failed,
            3 => Self::Truncated,
            4 => Self::ClientDisconnected,
            5 => Self::Cancelled,
            6 => Self::Unknown,
            _ => Self::All,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::Success => "success",
            Self::Failed => "failed",
            Self::Truncated => "truncated",
            Self::ClientDisconnected => "client_disconnected",
            Self::Cancelled => "cancelled",
            Self::Unknown => "unknown",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "all" => Some(Self::All),
            "success" => Some(Self::Success),
            "failed" => Some(Self::Failed),
            "truncated" => Some(Self::Truncated),
            "client_disconnected" => Some(Self::ClientDisconnected),
            "cancelled" => Some(Self::Cancelled),
            "unknown" => Some(Self::Unknown),
            _ => None,
        }
    }
}

/// The log page's time range. The live bucket is the file tail; the rest are
/// history reads. `Day` and `All` are appended after the original four indices so
/// an older saved document keeps its meaning.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum LogRange {
    #[default]
    Live,
    Today,
    Week,
    Month,
    Day,
    All,
}

impl LogRange {
    pub fn index(self) -> i32 {
        match self {
            Self::Live => 0,
            Self::Today => 1,
            Self::Week => 2,
            Self::Month => 3,
            Self::Day => 4,
            Self::All => 5,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Today,
            2 => Self::Week,
            3 => Self::Month,
            4 => Self::Day,
            5 => Self::All,
            _ => Self::Live,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Today => "today",
            Self::Week => "7d",
            Self::Month => "30d",
            Self::Day => "24h",
            Self::All => "all",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "live" => Some(Self::Live),
            "today" => Some(Self::Today),
            "7d" => Some(Self::Week),
            "30d" => Some(Self::Month),
            "24h" => Some(Self::Day),
            "all" => Some(Self::All),
            _ => None,
        }
    }
}

/// The log page's level filter as a bitmask. An empty selection shows every
/// level, including ungraded rows; selecting levels narrows the gateway query.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LogLevels(u8);

impl LogLevels {
    pub const ERROR: u8 = 1;
    pub const WARN: u8 = 2;
    pub const INFO: u8 = 4;
    pub const DEBUG: u8 = 8;

    pub fn from_flags(error: bool, warn: bool, info: bool, debug: bool) -> Self {
        let mut mask = 0;
        if error {
            mask |= Self::ERROR;
        }
        if warn {
            mask |= Self::WARN;
        }
        if info {
            mask |= Self::INFO;
        }
        if debug {
            mask |= Self::DEBUG;
        }
        Self(mask)
    }

    pub fn from_mask(mask: u8) -> Self {
        Self(mask & (Self::ERROR | Self::WARN | Self::INFO | Self::DEBUG))
    }

    pub fn mask(self) -> u8 {
        self.0
    }

    pub fn flags(self) -> (bool, bool, bool, bool) {
        (
            self.0 & Self::ERROR != 0,
            self.0 & Self::WARN != 0,
            self.0 & Self::INFO != 0,
            self.0 & Self::DEBUG != 0,
        )
    }

    pub fn toggle(self, level: &str) -> Self {
        let bit = match level.to_ascii_lowercase().as_str() {
            "error" => Self::ERROR,
            "warn" | "warning" => Self::WARN,
            "info" => Self::INFO,
            "debug" => Self::DEBUG,
            _ => return self,
        };
        Self(self.0 ^ bit)
    }

    pub fn matches(self, level: &str) -> bool {
        if self.0 == 0 {
            return true;
        }
        match level.to_ascii_lowercase().as_str() {
            "error" => self.0 & Self::ERROR != 0,
            "warn" | "warning" => self.0 & Self::WARN != 0,
            "info" => self.0 & Self::INFO != 0,
            "debug" => self.0 & Self::DEBUG != 0,
            _ => false,
        }
    }

    /// The saved values in the gateway's closed-set order.
    pub fn keys(self) -> Vec<&'static str> {
        let mut levels = Vec::new();
        if self.0 & Self::ERROR != 0 {
            levels.push("error");
        }
        if self.0 & Self::WARN != 0 {
            levels.push("warn");
        }
        if self.0 & Self::INFO != 0 {
            levels.push("info");
        }
        if self.0 & Self::DEBUG != 0 {
            levels.push("debug");
        }
        levels
    }

    /// Reads the saved value. The current document stores an array of level
    /// names; an older document stored one of `all/info/warn/error/raw`, and both
    /// keep their meaning.
    pub fn parse_value(value: &Value) -> Option<Self> {
        match value {
            Value::String(text) => match text.as_str() {
                "all" => Some(Self::default()),
                "info" => Some(Self::from_mask(Self::INFO)),
                "warn" => Some(Self::from_mask(Self::WARN)),
                "error" => Some(Self::from_mask(Self::ERROR)),
                "raw" => Some(Self::from_mask(Self::DEBUG)),
                _ => None,
            },
            Value::Array(items) => {
                let mut levels = Self::default();
                for item in items {
                    let bit = match item.as_str()? {
                        "error" => Self::ERROR,
                        "warn" => Self::WARN,
                        "info" => Self::INFO,
                        "debug" => Self::DEBUG,
                        _ => return None,
                    };
                    levels.0 |= bit;
                }
                Some(levels)
            }
            _ => None,
        }
    }
}

/// Whether the rail shows labels or only icons.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SidebarMode {
    #[default]
    Expanded,
    IconsOnly,
}

impl SidebarMode {
    pub fn index(self) -> i32 {
        match self {
            Self::Expanded => 0,
            Self::IconsOnly => 1,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::IconsOnly,
            _ => Self::Expanded,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::Expanded => "expanded",
            Self::IconsOnly => "icons",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "expanded" => Some(Self::Expanded),
            "icons" => Some(Self::IconsOnly),
            _ => None,
        }
    }
}

/// The reduced-motion preference. `System` follows the Windows setting; `Reduced`
/// turns animations off regardless of it.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum MotionPreference {
    #[default]
    System,
    Reduced,
}

impl MotionPreference {
    pub fn index(self) -> i32 {
        match self {
            Self::System => 0,
            Self::Reduced => 1,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Reduced,
            _ => Self::System,
        }
    }

    pub fn allows(self, system_allows: bool) -> bool {
        match self {
            Self::System => system_allows,
            Self::Reduced => false,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::Reduced => "reduced",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "system" => Some(Self::System),
            "reduced" => Some(Self::Reduced),
            _ => None,
        }
    }
}

/// Every page setting that survives a restart.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Settings {
    pub auto_refresh: AutoRefresh,
    pub usage_range: TimeRange,
    pub request_range: TimeRange,
    pub request_result: RequestResult,
    pub request_search: String,
    pub log_range: LogRange,
    pub log_level: LogLevels,
    pub log_keyword: String,
    pub sidebar: SidebarMode,
    pub reduced_motion: MotionPreference,
    pub reduce_transparency: bool,
}

/// The saved appearance choice. `System` follows the Windows app theme; the two
/// explicit modes ignore it.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ThemeMode {
    #[default]
    System,
    Light,
    Dark,
}

impl ThemeMode {
    /// Reads the persisted value. Unknown text and any non-string JSON value fall
    /// back to `System`, so an older or hand-edited file keeps the port working.
    pub fn parse(text: &str) -> Self {
        match text {
            "light" => Self::Light,
            "dark" => Self::Dark,
            _ => Self::System,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::Light => "light",
            Self::Dark => "dark",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopConfig {
    pub model_api_port: u16,
    pub intro_dismissed: bool,
    pub theme: ThemeMode,
    pub settings: Settings,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadOutcome {
    /// No settings file yet. This is a legal first-open state, not an error.
    Missing,
    Loaded(DesktopConfig),
    /// The file exists but is unreadable, corrupt or invalid. The caller keeps
    /// the defaults and must show the reason instead of pretending success.
    Invalid(String),
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_api_port: DEFAULT_MODEL_API_PORT,
            intro_dismissed: false,
            theme: ThemeMode::System,
            settings: Settings::default(),
        }
    }
}

/// `%LOCALAPPDATA%\Chat2API` on Windows. The caller supplies the root so tests
/// never touch the real user profile.
pub fn config_dir(local_app_data: &Path) -> PathBuf {
    local_app_data.join("Chat2API")
}

pub fn config_path(local_app_data: &Path) -> PathBuf {
    config_dir(local_app_data).join("desktop.json")
}

/// `%LOCALAPPDATA%\Chat2API`.
pub fn app_data_root() -> Result<PathBuf, String> {
    let local = std::env::var_os("LOCALAPPDATA")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "LOCALAPPDATA 未设置，无法定位应用数据目录".to_string())?;
    Ok(config_dir(Path::new(&local)))
}

/// `%LOCALAPPDATA%\Chat2API\desktop.json`.
pub fn default_config_path() -> Result<PathBuf, String> {
    Ok(app_data_root()?.join("desktop.json"))
}

pub fn model_api_bind(config: &DesktopConfig) -> String {
    format!("{MODEL_API_HOST}:{}", config.model_api_port)
}

pub fn validate_port(text: &str) -> Result<u16, String> {
    let trimmed = text.trim();
    let port = trimmed
        .parse::<u16>()
        .map_err(|_| "端口必须是 1-65535 的整数".to_string())?;
    if port == 0 {
        return Err("端口必须在 1-65535".to_string());
    }
    Ok(port)
}

pub fn parse_config(text: &str) -> Result<DesktopConfig, String> {
    parse_config_with_warnings(text).map(|(config, _)| config)
}

/// Parses the saved document and keeps a reason for every optional page setting
/// that was present but invalid. A missing setting is a legal older document and
/// keeps its default without a warning; a corrupt required field stays an error.
pub fn parse_config_with_warnings(text: &str) -> Result<(DesktopConfig, Vec<String>), String> {
    let body: Value =
        serde_json::from_str(text).map_err(|error| format!("配置不是有效 JSON：{error}"))?;
    let port = body
        .get("model_api_port")
        .ok_or_else(|| "配置缺少 model_api_port".to_string())?
        .as_u64()
        .ok_or_else(|| "配置 model_api_port 不是整数".to_string())?;
    if port == 0 || port > u16::MAX as u64 {
        return Err("配置 model_api_port 超出 1-65535".to_string());
    }
    let intro_dismissed = match body.get("intro_dismissed") {
        None => false,
        Some(value) => value
            .as_bool()
            .ok_or_else(|| "配置 intro_dismissed 不是布尔值".to_string())?,
    };
    let theme = match body.get("theme") {
        Some(Value::String(text)) => ThemeMode::parse(text),
        _ => ThemeMode::System,
    };
    let (settings, warnings) = parse_settings(&body);
    Ok((
        DesktopConfig {
            model_api_port: port as u16,
            intro_dismissed,
            theme,
            settings,
        },
        warnings,
    ))
}

fn parse_settings(body: &Value) -> (Settings, Vec<String>) {
    let mut settings = Settings::default();
    let mut warnings = Vec::new();

    if let Some(value) = body.get("auto_refresh") {
        match value
            .as_i64()
            .and_then(|seconds| i32::try_from(seconds).ok())
            .and_then(AutoRefresh::from_seconds)
        {
            Some(parsed) => settings.auto_refresh = parsed,
            None => warnings.push(invalid_setting(
                "auto_refresh",
                "5、15、30、60 或 0",
                "5 秒",
            )),
        }
    }
    if let Some(value) = body.get("usage_range") {
        match value.as_str().and_then(TimeRange::parse) {
            Some(parsed) => settings.usage_range = parsed,
            None => warnings.push(invalid_setting(
                "usage_range",
                "today、24h、7d、30d 或 all",
                "today",
            )),
        }
    }
    if let Some(value) = body.get("request_range") {
        match value.as_str().and_then(TimeRange::parse) {
            Some(parsed) => settings.request_range = parsed,
            None => warnings.push(invalid_setting(
                "request_range",
                "today、24h、7d、30d 或 all",
                "today",
            )),
        }
    }
    if let Some(value) = body.get("request_result") {
        match value.as_str().and_then(RequestResult::parse) {
            Some(parsed) => settings.request_result = parsed,
            None => warnings.push(invalid_setting(
                "request_result",
                "all、success、failed、truncated、client_disconnected、cancelled 或 unknown",
                "all",
            )),
        }
    }
    if let Some(value) = body.get("request_search") {
        match value.as_str() {
            Some(text) => settings.request_search = text.to_string(),
            None => warnings.push(invalid_setting("request_search", "字符串", "空")),
        }
    }
    if let Some(value) = body.get("log_range") {
        match value.as_str().and_then(LogRange::parse) {
            Some(parsed) => settings.log_range = parsed,
            None => warnings.push(invalid_setting(
                "log_range",
                "live、today、24h、7d、30d 或 all",
                "live",
            )),
        }
    }
    if let Some(value) = body.get("log_level") {
        match LogLevels::parse_value(value) {
            Some(parsed) => settings.log_level = parsed,
            None => warnings.push(invalid_setting(
                "log_level",
                "级别数组（error、warn、info、debug）或旧单值 all、info、warn、error、raw",
                "全部级别",
            )),
        }
    }
    if let Some(value) = body.get("log_keyword") {
        match value.as_str() {
            Some(text) => settings.log_keyword = text.to_string(),
            None => warnings.push(invalid_setting("log_keyword", "字符串", "空")),
        }
    }
    if let Some(value) = body.get("sidebar") {
        match value.as_str().and_then(SidebarMode::parse) {
            Some(parsed) => settings.sidebar = parsed,
            None => warnings.push(invalid_setting("sidebar", "expanded 或 icons", "expanded")),
        }
    }
    if let Some(value) = body.get("reduced_motion") {
        match value.as_str().and_then(MotionPreference::parse) {
            Some(parsed) => settings.reduced_motion = parsed,
            None => warnings.push(invalid_setting(
                "reduced_motion",
                "system 或 reduced",
                "system",
            )),
        }
    }
    if let Some(value) = body.get("reduce_transparency") {
        match value.as_bool() {
            Some(parsed) => settings.reduce_transparency = parsed,
            None => warnings.push(invalid_setting("reduce_transparency", "布尔值", "false")),
        }
    }

    (settings, warnings)
}

fn invalid_setting(field: &str, expected: &str, default: &str) -> String {
    format!("配置 {field} 无效（需要 {expected}），已使用默认值 {default}")
}

pub fn load_from(path: &Path) -> LoadOutcome {
    load_from_with_warnings(path).0
}

/// Loads the document and keeps the reasons for invalid page settings so the
/// status line can say which values fell back to their defaults.
pub fn load_from_with_warnings(path: &Path) -> (LoadOutcome, Vec<String>) {
    match std::fs::read_to_string(path) {
        Ok(text) => match parse_config_with_warnings(&text) {
            Ok((config, warnings)) => (LoadOutcome::Loaded(config), warnings),
            Err(error) => (LoadOutcome::Invalid(error), Vec::new()),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            (LoadOutcome::Missing, Vec::new())
        }
        Err(error) => (
            LoadOutcome::Invalid(format!("读取配置失败：{error}")),
            Vec::new(),
        ),
    }
}

/// Writes the settings atomically and reduces the file ACL to the current user.
///
/// The temp file is hardened before the rename so the final path never exists with
/// the inherited profile ACL. A failed hardening removes the temp file and reports
/// the error instead of leaving a half-protected config behind.
pub fn save_to(path: &Path, config: &DesktopConfig) -> Result<(), String> {
    if config.model_api_port == 0 {
        return Err("端口必须在 1-65535".to_string());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "配置路径没有父目录".to_string())?;
    std::fs::create_dir_all(parent).map_err(|error| format!("创建配置目录失败：{error}"))?;
    let settings = &config.settings;
    let text = serde_json::to_string_pretty(&json!({
        "version": CONFIG_VERSION,
        "model_api_port": config.model_api_port,
        "intro_dismissed": config.intro_dismissed,
        "theme": config.theme.key(),
        "auto_refresh": settings.auto_refresh.seconds(),
        "usage_range": settings.usage_range.key(),
        "request_range": settings.request_range.key(),
        "request_result": settings.request_result.key(),
        "request_search": settings.request_search,
        "log_range": settings.log_range.key(),
        "log_level": settings.log_level.keys(),
        "log_keyword": settings.log_keyword,
        "sidebar": settings.sidebar.key(),
        "reduced_motion": settings.reduced_motion.key(),
        "reduce_transparency": settings.reduce_transparency,
    }))
    .map_err(|error| format!("序列化配置失败：{error}"))?;
    let temp = parent.join(format!(".desktop.json.{}.tmp", std::process::id()));
    std::fs::write(&temp, format!("{text}\n"))
        .map_err(|error| format!("写入临时配置失败：{error}"))?;
    if let Err(error) = harden_private_file(&temp) {
        let _ = std::fs::remove_file(&temp);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temp, path) {
        let _ = std::fs::remove_file(&temp);
        return Err(format!("替换配置文件失败：{error}"));
    }
    Ok(())
}

/// Applies a theme choice to the in-memory config first, then persists it.
///
/// The window switches even when the disk write fails, so the in-memory mode is
/// updated before the save. The error carries the status-row text for that case.
pub fn apply_theme(
    path: Option<&Path>,
    config: &mut DesktopConfig,
    theme: ThemeMode,
) -> Result<(), String> {
    config.theme = theme;
    persist(path, config, "主题已切换")
}

/// Applies a sidebar choice to the in-memory config first, then persists it.
pub fn apply_sidebar(
    path: Option<&Path>,
    config: &mut DesktopConfig,
    mode: SidebarMode,
) -> Result<(), String> {
    config.settings.sidebar = mode;
    persist(path, config, "外观设置已更新")
}

/// Applies a motion preference to the in-memory config first, then persists it.
pub fn apply_motion(
    path: Option<&Path>,
    config: &mut DesktopConfig,
    preference: MotionPreference,
) -> Result<(), String> {
    config.settings.reduced_motion = preference;
    persist(path, config, "外观设置已更新")
}

/// Applies the reduced-transparency choice to the in-memory config first, then
/// persists it.
pub fn apply_reduce_transparency(
    path: Option<&Path>,
    config: &mut DesktopConfig,
    reduce: bool,
) -> Result<(), String> {
    config.settings.reduce_transparency = reduce;
    persist(path, config, "外观设置已更新")
}

/// Pushes every saved page setting into the window. The window's own `changed`
/// handlers keep the derived indices and timers in step with these values.
pub fn apply_settings(window: &MainWindow, config: &DesktopConfig, system_allows_motion: bool) {
    let settings = &config.settings;
    window.set_auto_refresh(settings.auto_refresh.seconds());
    window.set_auto_refresh_index(settings.auto_refresh.index());
    window.set_usage_period(settings.usage_range.index());
    window.set_request_period(settings.request_range.index());
    window.set_request_result(settings.request_result.index());
    window.set_request_keyword(settings.request_search.clone().into());
    window.set_log_period(settings.log_range.index());
    let (error, warn, info, debug) = settings.log_level.flags();
    window.set_log_level_error(error);
    window.set_log_level_warn(warn);
    window.set_log_level_info(info);
    window.set_log_level_debug(debug);
    window.set_log_filter_keyword(settings.log_keyword.clone().into());
    window.set_sidebar_mode(settings.sidebar.index());
    window.set_motion_mode(settings.reduced_motion.index());
    window.set_system_allows_motion(system_allows_motion);
    window.set_reduce_transparency(settings.reduce_transparency);
}

/// Reads the page settings that have a window control. Appearance choices that
/// are not controlled from the window yet keep their saved value.
pub fn settings_from_window(window: &MainWindow) -> Settings {
    Settings {
        auto_refresh: AutoRefresh::from_seconds(window.get_auto_refresh()).unwrap_or_default(),
        usage_range: TimeRange::from_index(window.get_usage_period()),
        request_range: TimeRange::from_index(window.get_request_period()),
        request_result: RequestResult::from_index(window.get_request_result()),
        request_search: window.get_request_keyword().to_string(),
        log_range: LogRange::from_index(window.get_log_period()),
        log_level: LogLevels::from_flags(
            window.get_log_level_error(),
            window.get_log_level_warn(),
            window.get_log_level_info(),
            window.get_log_level_debug(),
        ),
        log_keyword: window.get_log_filter_keyword().to_string(),
        ..Settings::default()
    }
}

/// Captures the window's page settings and persists them. A failed write keeps
/// the captured values in memory and returns the status-row reason.
pub fn save_window_settings(
    path: Option<&Path>,
    config: &mut DesktopConfig,
    window: &MainWindow,
) -> Result<(), String> {
    let captured = settings_from_window(window);
    config.settings.auto_refresh = captured.auto_refresh;
    config.settings.usage_range = captured.usage_range;
    config.settings.request_range = captured.request_range;
    config.settings.request_result = captured.request_result;
    config.settings.request_search = captured.request_search;
    config.settings.log_range = captured.log_range;
    config.settings.log_level = captured.log_level;
    config.settings.log_keyword = captured.log_keyword;
    persist(path, config, "设置已更新")
}

fn persist(path: Option<&Path>, config: &DesktopConfig, applied: &str) -> Result<(), String> {
    let Some(path) = path else {
        return Err(format!("{applied}，但保存失败：无法定位配置文件"));
    };
    save_to(path, config).map_err(|error| format!("{applied}，但保存失败：{error}"))
}

#[cfg(unix)]
pub(crate) fn harden_private_file(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("设置配置文件权限失败：{error}"))
}

#[cfg(windows)]
pub(crate) fn harden_private_file(path: &Path) -> Result<(), String> {
    windows_acl::harden_to_current_user(path)
}

#[cfg(windows)]
mod windows_acl {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, ERROR_SUCCESS, HANDLE};
    use windows::Win32::Security::Authorization::{SetNamedSecurityInfoW, SE_FILE_OBJECT};
    use windows::Win32::Security::{
        AddAccessAllowedAce, GetTokenInformation, InitializeAcl, TokenUser, ACL, ACL_REVISION,
        DACL_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION, PSID, TOKEN_QUERY,
        TOKEN_USER,
    };
    use windows::Win32::Storage::FileSystem::FILE_ALL_ACCESS;
    use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    pub fn harden_to_current_user(path: &std::path::Path) -> Result<(), String> {
        let wide: Vec<u16> = path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            let mut token = HANDLE::default();
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)
                .map_err(|error| format!("读取当前用户令牌失败：{error}"))?;

            let mut size = 0u32;
            let _ = GetTokenInformation(token, TokenUser, None, 0, &mut size);
            if size == 0 {
                let _ = CloseHandle(token);
                return Err("当前用户令牌大小不可用".to_string());
            }
            let mut token_buffer = vec![0u8; size as usize];
            let token_info = GetTokenInformation(
                token,
                TokenUser,
                Some(token_buffer.as_mut_ptr() as *mut c_void),
                size,
                &mut size,
            );
            let _ = CloseHandle(token);
            token_info.map_err(|error| format!("读取当前用户 SID 失败：{error}"))?;
            let sid: PSID = (*token_buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid;

            let mut acl_buffer = vec![0u8; 1024];
            let acl = acl_buffer.as_mut_ptr().cast::<ACL>();
            InitializeAcl(acl, acl_buffer.len() as u32, ACL_REVISION)
                .map_err(|error| format!("初始化 ACL 失败：{error}"))?;
            AddAccessAllowedAce(acl, ACL_REVISION, FILE_ALL_ACCESS.0, sid)
                .map_err(|error| format!("添加当前用户 ACE 失败：{error}"))?;

            let result = SetNamedSecurityInfoW(
                PCWSTR(wide.as_ptr()),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(acl),
                None,
            );
            if result != ERROR_SUCCESS {
                return Err(format!("设置配置文件 ACL 失败：{result:?}"));
            }
        }
        Ok(())
    }
}
