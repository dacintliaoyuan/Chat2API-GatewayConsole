//! Admission before the first model request is sent to UM.

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::ingress::{DEFAULT_RECOVERY_QUEUE_MAX, DEFAULT_RECOVERY_QUEUE_MAX_BYTES};

const WAIT_POLL: Duration = Duration::from_millis(100);
const FIRST_RECHECK: Duration = Duration::from_secs(1);
const MAX_RECHECK: Duration = Duration::from_secs(30);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Open,
    Checking,
    Waiting,
    Trial,
    Releasing,
}

struct State {
    phase: Phase,
    generation: u64,
    next_probe: Option<Instant>,
    probing: bool,
    backoff: Duration,
    queue: VecDeque<u64>,
    bytes: u64,
    next_ticket: u64,
    trial: Option<(String, u64)>,
    trial_succeeded: bool,
    releasing: usize,
}

pub(crate) struct Recovery {
    state: Mutex<State>,
    changed: Condvar,
}

pub(crate) enum AdmissionError {
    Timeout,
    QueueFull,
    Cancelled,
}

impl Recovery {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(State {
                phase: Phase::Open,
                generation: 0,
                next_probe: None,
                probing: false,
                backoff: FIRST_RECHECK,
                queue: VecDeque::new(),
                bytes: 0,
                next_ticket: 0,
                trial: None,
                trial_succeeded: false,
                releasing: 0,
            }),
            changed: Condvar::new(),
        }
    }

    pub(crate) fn observe_failure(&self, explicit: bool) {
        let mut state = self.state.lock().expect("upstream recovery state");
        match state.phase {
            Phase::Waiting => return,
            Phase::Checking if !explicit => return,
            Phase::Trial | Phase::Releasing if !explicit => {
                // Until the shared recheck completes, the trial remains the
                // only request allowed through.
                if state.next_probe.is_none() && !state.probing {
                    state.next_probe = Some(Instant::now());
                }
                return;
            }
            _ => {}
        }
        state.phase = if explicit {
            Phase::Waiting
        } else {
            Phase::Checking
        };
        state.generation = state.generation.wrapping_add(1);
        state.next_probe = Some(Instant::now());
        state.backoff = FIRST_RECHECK;
    }

    // Only the existing auth watch takes and completes shared probes.
    pub(crate) fn take_probe(&self, now: Instant) -> Option<u64> {
        let mut state = self.state.lock().expect("upstream recovery state");
        if state.next_probe.is_some_and(|due| due <= now) {
            state.next_probe = None;
            state.probing = true;
            return Some(state.generation);
        }
        None
    }

    pub(crate) fn finish_probe(
        &self,
        generation: u64,
        valid: bool,
        stale_credential: bool,
        unavailable: bool,
    ) {
        let mut state = self.state.lock().expect("upstream recovery state");
        state.probing = false;
        if state.generation != generation {
            return;
        }
        if stale_credential {
            state.next_probe = Some(Instant::now());
        } else if valid {
            state.phase = match state.phase {
                Phase::Checking => Phase::Open,
                Phase::Releasing => Phase::Releasing,
                _ => Phase::Trial,
            };
            state.next_probe = None;
            state.backoff = FIRST_RECHECK;
        } else if unavailable || state.phase != Phase::Checking {
            state.phase = Phase::Waiting;
            state.generation = state.generation.wrapping_add(1);
            state.next_probe = Some(Instant::now() + state.backoff);
            state.backoff = (state.backoff * 2).min(MAX_RECHECK);
        } else {
            // Authentication evidence alone does not establish an outage.
            state.phase = Phase::Open;
        }
        self.changed.notify_all();
    }

    pub(crate) fn admit<'a>(
        &'a self,
        request_id: &'a str,
        bytes: u64,
        deadline: Instant,
        mut cancelled: impl FnMut() -> bool,
    ) -> Result<Option<Permit<'a>>, AdmissionError> {
        let mut state = self.state.lock().expect("upstream recovery state");
        if matches!(state.phase, Phase::Open | Phase::Checking) && state.queue.is_empty() {
            return Ok(None);
        }
        if state.queue.len() >= DEFAULT_RECOVERY_QUEUE_MAX
            || state.bytes.saturating_add(bytes) > DEFAULT_RECOVERY_QUEUE_MAX_BYTES
        {
            return Err(AdmissionError::QueueFull);
        }
        let ticket = state.next_ticket;
        state.next_ticket = state.next_ticket.wrapping_add(1);
        state.queue.push_back(ticket);
        state.bytes += bytes;
        let result = loop {
            drop(state);
            let is_cancelled = cancelled();
            state = self.state.lock().expect("upstream recovery state");
            if is_cancelled {
                break Err(AdmissionError::Cancelled);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break Err(AdmissionError::Timeout);
            }
            if state.queue.front() == Some(&ticket) {
                match state.phase {
                    Phase::Open | Phase::Checking => break Ok(None),
                    Phase::Trial
                        if state.trial.is_none()
                            && state.next_probe.is_none()
                            && !state.probing =>
                    {
                        state.trial = Some((request_id.to_owned(), state.generation));
                        state.trial_succeeded = false;
                        break Ok(Some(Permit {
                            recovery: self,
                            kind: PermitKind::Trial,
                            generation: state.generation,
                            delivered: false,
                        }));
                    }
                    Phase::Releasing if state.releasing < 2 => {
                        state.releasing += 1;
                        break Ok(Some(Permit {
                            recovery: self,
                            kind: PermitKind::Releasing,
                            generation: state.generation,
                            delivered: false,
                        }));
                    }
                    _ => {}
                }
            }
            state = self
                .changed
                .wait_timeout(state, remaining.min(WAIT_POLL))
                .expect("upstream recovery state")
                .0;
        };
        state.queue.retain(|queued| *queued != ticket);
        state.bytes -= bytes;
        if state.phase == Phase::Releasing && state.queue.is_empty() {
            state.phase = Phase::Open;
        }
        self.changed.notify_all();
        result
    }

    pub(crate) fn business_succeeded(&self, request_id: &str) {
        let mut state = self.state.lock().expect("upstream recovery state");
        if state.phase == Phase::Trial
            && state
                .trial
                .as_ref()
                .is_some_and(|(id, generation)| id == request_id && *generation == state.generation)
        {
            state.trial_succeeded = true;
        }
    }
    /// Check the current recovery generation after socket setup, before
    /// writing any upstream request bytes.
    pub(crate) fn can_send(&self, permit: Option<&Permit<'_>>) -> bool {
        let state = self.state.lock().expect("upstream recovery state");
        if matches!(state.phase, Phase::Open | Phase::Checking) {
            return true;
        }
        permit.is_some_and(|permit| {
            permit.generation == state.generation
                && matches!(
                    (state.phase, &permit.kind),
                    (Phase::Trial, PermitKind::Trial) | (Phase::Releasing, PermitKind::Releasing)
                )
        })
    }
}

enum PermitKind {
    Trial,
    Releasing,
}

pub(crate) struct Permit<'a> {
    recovery: &'a Recovery,
    kind: PermitKind,
    generation: u64,
    delivered: bool,
}

impl Permit<'_> {
    pub(crate) fn complete(mut self, delivered: bool) {
        self.delivered = delivered;
    }
}

impl Drop for Permit<'_> {
    fn drop(&mut self) {
        let mut state = self.recovery.state.lock().expect("upstream recovery state");
        match self.kind {
            PermitKind::Trial => {
                let trial = state.trial.take();
                if state.phase == Phase::Trial
                    && self.delivered
                    && state.trial_succeeded
                    && trial.is_some_and(|(_, generation)| generation == state.generation)
                    && state.next_probe.is_none()
                    && !state.probing
                {
                    state.phase = if state.queue.is_empty() {
                        Phase::Open
                    } else {
                        Phase::Releasing
                    };
                }
            }
            PermitKind::Releasing => state.releasing -= 1,
        }
        self.recovery.changed.notify_all();
    }
}
