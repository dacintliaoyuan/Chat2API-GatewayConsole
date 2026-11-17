//! Shared auth-watch visibility.
//!
//! Python keeps the maintenance-thread liveness and the last finished round on
//! the proxy itself (`_watch_thread`, `_watch_last_epoch`,
//! `_watch_last_outcome`, `_watch_rounds`, `_watch_interval_s`). The Rust port
//! splits the executor and the HTTP server, so both need one shared handle: the
//! watch thread writes it, `LoginService::status()` and `/health` read it.
//!
//! The view is additive and carries no credential and no round content: only
//! liveness, the last check moment/outcome, the finished round count, and the
//! interval that gates the next round.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};

use crate::login::profile::epoch_now;

#[derive(Debug, Default)]
struct WatchRound {
    last_round_epoch: Option<f64>,
    last_round_outcome: Option<String>,
    next_round_interval: Option<Duration>,
    rounds: u64,
}

/// The auth-watch thread state shared by the executor and the HTTP surface.
///
/// A never-started watch reads as `thread_alive=false`, null round fields and
/// `rounds=0`, exactly like Python before `begin_auth_watch()`.
#[derive(Debug, Default)]
pub struct WatchStatus {
    thread_alive: AtomicBool,
    round: Mutex<WatchRound>,
}

impl WatchStatus {
    pub fn new() -> Self {
        Self::default()
    }

    /// The watch thread started: report honest liveness.
    pub fn mark_started(&self) {
        self.thread_alive.store(true, Ordering::SeqCst);
    }

    /// The watch thread exited (including shutdown): never pretend it is alive.
    pub fn mark_stopped(&self) {
        self.thread_alive.store(false, Ordering::SeqCst);
    }

    pub fn is_alive(&self) -> bool {
        self.thread_alive.load(Ordering::SeqCst)
    }

    /// Record one finished round and the interval gating the next one.
    ///
    /// Python records the epoch/outcome/round count in
    /// `_record_watch_round()` and publishes the next backoff interval right
    /// after the round, so `next_round_due` is last epoch + this interval.
    pub fn record_round(&self, outcome: &str, next_interval: Duration) {
        let mut round = self.round.lock().expect("watch status poisoned");
        round.last_round_epoch = Some(epoch_now());
        round.last_round_outcome = Some(outcome.to_string());
        round.next_round_interval = Some(next_interval);
        round.rounds = round.rounds.saturating_add(1);
    }

    /// The additive `watch` object served by `/admin/auth/status` and `/health`.
    pub fn snapshot(&self) -> Value {
        let round = self.round.lock().expect("watch status poisoned");
        let next_round_due = round
            .last_round_epoch
            .zip(round.next_round_interval)
            .map(|(epoch, interval)| epoch + interval.as_secs_f64());
        json!({
            "thread_alive": self.is_alive(),
            "last_round_epoch": round.last_round_epoch,
            "last_round_outcome": round.last_round_outcome,
            "next_round_due": next_round_due,
            "rounds": round.rounds,
        })
    }
}
