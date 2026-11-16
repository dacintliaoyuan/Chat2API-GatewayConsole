//! Deterministic uuid/timestamp sources for tests and production.
//!
//! The Python store stamps rows with `str(uuid.uuid4())` and
//! `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")`. The Rust
//! store accepts injected sources so a fixture can compare the SAME sequence.

use std::time::{SystemTime, UNIX_EPOCH};

/// A fresh UUID-v4 string (Python's `str(uuid.uuid4())`).
pub fn random_uuid() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::getrandom(&mut bytes).is_err() {
        // A failed entropy read is a hard failure, never a reused id.
        panic!("share store cannot read OS entropy");
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

/// The current UTC instant in Python's `%Y-%m-%dT%H:%M:%S.%fZ` format.
pub fn utc_now() -> String {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    format_timestamp(duration.as_secs() as i64, duration.subsec_micros())
}

/// Format one unix second + microsecond pair as the store timestamp.
pub fn format_timestamp(seconds: i64, micros: u32) -> String {
    let days = seconds.div_euclid(86_400);
    let remainder = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = remainder / 3_600;
    let minute = (remainder % 3_600) / 60;
    let second = remainder % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}Z")
}

/// Howard Hinnant's `civil_from_days` (days since 1970-01-01).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}
