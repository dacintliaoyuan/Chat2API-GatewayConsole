//! AC6: the broker budgets and the total-deadline bounded read.
//!
//! Mirrors `share/broker_protocol.py`: the wire deadline is 10 s, dispatch
//! 130 s, the drain window 60 s, the reserved stop 20 s, the client drain
//! deadline 90 s and emergency 30 s. A client exchange may shorten the
//! deadline but never extend it, and the remaining time is re-derived before
//! EVERY byte so a drip cannot park a cycle beyond the deadline.

use std::io;
use std::time::{Duration, Instant};

use crate::codec::MAX_MESSAGE;
use crate::error::{ProtocolError, ResultCode};

pub const DEFAULT_DEADLINE_SECONDS: f64 = 10.0;
pub const DISPATCH_DEADLINE_SECONDS: f64 = 130.0;
pub const DRAIN_WINDOW_SECONDS: f64 = 60.0;
pub const STOP_BUDGET_SECONDS: f64 = 20.0;
pub const DRAIN_DEADLINE_SECONDS: f64 = DRAIN_WINDOW_SECONDS + STOP_BUDGET_SECONDS + 10.0;
pub const EMERGENCY_DEADLINE_SECONDS: f64 = STOP_BUDGET_SECONDS + 10.0;
pub const MIN_CLIENT_TIMEOUT_SECONDS: f64 = 1.0;
pub const MAX_CLIENT_TIMEOUT_SECONDS: f64 = 180.0;

/// One byte source bounded by the caller's total deadline.
pub trait ByteSource {
    /// `Ok(None)` is EOF; an `Err` is an I/O failure (never a frame).
    fn read_byte(&mut self) -> io::Result<Option<u8>>;
}

impl<R: std::io::Read> ByteSource for std::io::Bytes<R> {
    fn read_byte(&mut self) -> io::Result<Option<u8>> {
        match self.next() {
            None => Ok(None),
            Some(Ok(byte)) => Ok(Some(byte)),
            Some(Err(error)) => Err(error),
        }
    }
}

/// A client `timeout_s` is either absent (the configured default) or a finite
/// value in 1..180; an out-of-range value is refused, never clamped.
pub fn validate_timeout_s(timeout_s: Option<f64>) -> Result<f64, ProtocolError> {
    match timeout_s {
        None => Ok(DEFAULT_DEADLINE_SECONDS),
        Some(value)
            if value.is_finite()
                && (MIN_CLIENT_TIMEOUT_SECONDS..=MAX_CLIENT_TIMEOUT_SECONDS).contains(&value) =>
        {
            Ok(value)
        }
        Some(_) => Err(ProtocolError::new(
            ResultCode::BadRequest,
            "timeout_s must be within the bounded 1..180 second range",
        )),
    }
}

/// The caller's deadline only SHORTENS the exchange; it never starts a new
/// timeout after peer verification.
pub fn exchange_deadline(
    now: Instant,
    timeout_s: f64,
    caller_deadline: Option<Instant>,
) -> Instant {
    let deadline = now + Duration::from_secs_f64(timeout_s);
    match caller_deadline {
        Some(caller) if caller < deadline => caller,
        _ => deadline,
    }
}

/// Read exactly one line bounded by [`MAX_MESSAGE`] and the TOTAL deadline.
///
/// The remaining time is re-derived before every read; EOF before any byte is
/// `empty_request`, EOF mid-frame is `deadline_exceeded`, and an oversized
/// frame is answered `overlimit` BEFORE a complete frame is accepted.
pub fn read_bounded_line<S, F>(
    source: &mut S,
    deadline: Instant,
    mut now: F,
) -> Result<Vec<u8>, ProtocolError>
where
    S: ByteSource,
    F: FnMut() -> Instant,
{
    let mut chunks = Vec::new();
    loop {
        if now() >= deadline {
            return Err(ProtocolError::new(
                ResultCode::DeadlineExceeded,
                "request deadline exceeded",
            ));
        }
        let byte = source.read_byte().map_err(|error| {
            ProtocolError::new(
                ResultCode::BadRequest,
                format!("frame read failed: {}", error.kind() as u8),
            )
        })?;
        match byte {
            None => {
                return Err(if chunks.is_empty() {
                    ProtocolError::new(ResultCode::EmptyRequest, "connection closed mid-request")
                } else {
                    ProtocolError::new(
                        ResultCode::DeadlineExceeded,
                        "connection closed mid-request",
                    )
                });
            }
            Some(b'\n') => {
                chunks.push(b'\n');
                return Ok(chunks);
            }
            Some(byte) => {
                chunks.push(byte);
                if chunks.len() > MAX_MESSAGE {
                    return Err(ProtocolError::new(
                        ResultCode::Overlimit,
                        "request line exceeds bound",
                    ));
                }
            }
        }
    }
}
