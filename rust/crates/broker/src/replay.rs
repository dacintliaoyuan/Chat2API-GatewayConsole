//! AC6: one-shot `request_id` consumption (replay protection).
//!
//! Mirrors `share/broker_protocol.py::ReplayRegistry`: expired ids are
//! reclaimed first, a table FULL of still-valid ids refuses new requests
//! instead of evicting them, and consumption happens BEFORE any side effect.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::error::{ProtocolError, ResultCode};
use crate::request::{MAX_REQUEST_ID_CHARS, TS_WINDOW_SECONDS};

pub const DEFAULT_MAX_SEEN: usize = 20_000;

pub struct ReplayRegistry {
    max_seen: usize,
    seen: Mutex<HashMap<String, i64>>,
}

impl ReplayRegistry {
    pub fn new(max_seen: usize) -> Self {
        Self {
            max_seen,
            seen: Mutex::new(HashMap::new()),
        }
    }

    pub fn size(&self) -> usize {
        self.seen.lock().map(|seen| seen.len()).unwrap_or(0)
    }

    /// Consume one request id atomically. Raises `replay` on a replayed id,
    /// `overlimit` when the table is full of still-valid ids, and
    /// `bad_request` for an invalid id.
    pub fn consume(&self, request_id: &str, ts: i64, now: i64) -> Result<(), ProtocolError> {
        let length = request_id.chars().count();
        if !(1..=MAX_REQUEST_ID_CHARS).contains(&length) {
            return Err(ProtocolError::new(
                ResultCode::BadRequest,
                "invalid request_id",
            ));
        }
        let mut seen = self.seen.lock().map_err(|_| {
            ProtocolError::new(ResultCode::InternalError, "replay registry poisoned")
        })?;
        seen.retain(|_, seen_ts| now - *seen_ts <= TS_WINDOW_SECONDS);
        if seen.contains_key(request_id) {
            return Err(ProtocolError::new(
                ResultCode::Replay,
                "replayed request_id",
            ));
        }
        if seen.len() >= self.max_seen {
            return Err(ProtocolError::new(
                ResultCode::Overlimit,
                "replay table capacity exceeded; retry after expiry",
            ));
        }
        seen.insert(request_id.to_string(), ts);
        Ok(())
    }
}

impl Default for ReplayRegistry {
    fn default() -> Self {
        Self::new(DEFAULT_MAX_SEEN)
    }
}
