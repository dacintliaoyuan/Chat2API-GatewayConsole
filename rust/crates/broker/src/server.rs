//! AC7: one bounded management connection cycle.
//!
//! The cycle is transport-agnostic: read ONE bounded frame, decode, validate,
//! consume the request id, dispatch, encode and write the reply. An overlimit
//! frame is answered explicitly; a deadline or a poisoned cycle never reaches a
//! handler and never stops the next cycle.

use std::io;
use std::time::Instant;

use crate::codec::{decode_frame, encode_frame};
use crate::error::{ProtocolError, ResultCode};
use crate::management::{rejection_frame, ManagementDispatcher};

/// One connection's frame I/O. The POSIX adapter wraps `UnixConnection`; tests
/// use an in-memory connection.
pub trait FrameIo {
    fn read_frame(&mut self, deadline: Instant) -> Result<Vec<u8>, ProtocolError>;
    fn write_frame(&mut self, frame: &[u8]) -> io::Result<()>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CycleOutcome {
    Replied {
        request_id: Option<String>,
        result: ResultCode,
    },
    NoReply {
        error: ResultCode,
    },
}

impl CycleOutcome {
    pub fn code(&self) -> Option<ResultCode> {
        match self {
            Self::Replied { result, .. } => Some(*result),
            Self::NoReply { error } => Some(*error),
        }
    }

    pub fn request_id(&self) -> Option<&str> {
        match self {
            Self::Replied { request_id, .. } => request_id.as_deref(),
            Self::NoReply { .. } => None,
        }
    }
}

/// Run ONE management cycle over `io`.
pub fn serve_management_cycle(
    io: &mut dyn FrameIo,
    dispatcher: &ManagementDispatcher,
    secret: &str,
    deadline: Instant,
    now: i64,
) -> CycleOutcome {
    let frame = match io.read_frame(deadline) {
        Ok(frame) => frame,
        Err(error) => {
            if error.code() == ResultCode::Overlimit {
                // The frame has no request_id association: answer explicitly
                // before the connection is released, never dispatch.
                let reply = rejection_frame(None, &error);
                write_reply(io, &reply);
            }
            return CycleOutcome::NoReply {
                error: error.code(),
            };
        }
    };
    let body = match decode_frame(&frame) {
        Ok(body) => body,
        Err(error) => {
            let reply = rejection_frame(None, &error);
            write_reply(io, &reply);
            return CycleOutcome::NoReply {
                error: error.code(),
            };
        }
    };
    let reply = dispatcher.handle_frame(&body, secret, now);
    let request_id = reply.request_id().map(str::to_string);
    let result = reply.result().error_code();
    let value = reply.to_value();
    if !write_reply(io, &value) {
        return CycleOutcome::NoReply {
            error: ResultCode::InternalError,
        };
    }
    CycleOutcome::Replied { request_id, result }
}

fn write_reply(io: &mut dyn FrameIo, value: &serde_json::Value) -> bool {
    let Ok(frame) = encode_frame(value) else {
        return false;
    };
    io.write_frame(&frame).is_ok()
}
