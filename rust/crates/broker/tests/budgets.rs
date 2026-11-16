//! AC6: the broker budgets and the total-deadline bounded read.

use std::io;
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

use um_broker::budget::{
    exchange_deadline, read_bounded_line, validate_timeout_s, ByteSource, DEFAULT_DEADLINE_SECONDS,
    DISPATCH_DEADLINE_SECONDS, DRAIN_DEADLINE_SECONDS, DRAIN_WINDOW_SECONDS,
    EMERGENCY_DEADLINE_SECONDS, STOP_BUDGET_SECONDS,
};
use um_broker::codec::MAX_MESSAGE;
use um_broker::error::ResultCode;
use um_broker::replay::{ReplayRegistry, DEFAULT_MAX_SEEN};

fn code(error: &um_broker::error::ProtocolError) -> ResultCode {
    error.code()
}

#[test]
fn the_budgets_match_the_python_contract() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let expected = |name: &str| -> f64 {
        fixture
            .boundaries
            .timeouts
            .iter()
            .find(|timeout| timeout.surface == "broker" && timeout.name == name)
            .unwrap_or_else(|| panic!("missing broker timeout {name}"))
            .value
    };
    assert_eq!(DEFAULT_DEADLINE_SECONDS, expected("wire_deadline"));
    assert_eq!(DISPATCH_DEADLINE_SECONDS, expected("dispatch_deadline"));
    assert_eq!(DRAIN_WINDOW_SECONDS, expected("drain_window"));
    assert_eq!(STOP_BUDGET_SECONDS, expected("stop_budget"));
    assert_eq!(DRAIN_DEADLINE_SECONDS, expected("drain_deadline"));
    assert_eq!(EMERGENCY_DEADLINE_SECONDS, expected("emergency_deadline"));
    assert_eq!(
        DRAIN_DEADLINE_SECONDS,
        DRAIN_WINDOW_SECONDS + STOP_BUDGET_SECONDS + 10.0
    );
    assert_eq!(EMERGENCY_DEADLINE_SECONDS, STOP_BUDGET_SECONDS + 10.0);
}

#[test]
fn the_replay_registry_default_capacity_matches_python() {
    assert_eq!(DEFAULT_MAX_SEEN, 20_000);
    assert_eq!(ReplayRegistry::default().size(), 0);
}

#[test]
fn a_client_timeout_is_bounded_to_1_through_180_seconds() {
    assert_eq!(validate_timeout_s(None).unwrap(), DEFAULT_DEADLINE_SECONDS);
    for value in [1.0, 1.5, 180.0] {
        assert_eq!(validate_timeout_s(Some(value)).unwrap(), value);
    }
    for value in [0.0, 0.5, 180.1, -1.0, f64::NAN, f64::INFINITY] {
        let error = validate_timeout_s(Some(value)).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest, "value={value}");
    }
}

#[test]
fn a_caller_deadline_only_shortens_the_exchange() {
    let now = Instant::now();
    let caller = now + Duration::from_secs(5);
    assert_eq!(
        exchange_deadline(now, 10.0, Some(caller)),
        caller,
        "the caller deadline wins when shorter"
    );
    assert_eq!(
        exchange_deadline(now, 1.0, Some(caller)),
        now + Duration::from_secs(1),
        "the client timeout wins when shorter"
    );
    assert_eq!(
        exchange_deadline(now, 10.0, None),
        now + Duration::from_secs(10)
    );
}

struct SliceSource<'a> {
    data: &'a [u8],
    index: usize,
    reads: usize,
}

impl<'a> SliceSource<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            index: 0,
            reads: 0,
        }
    }
}

impl ByteSource for SliceSource<'_> {
    fn read_byte(&mut self) -> io::Result<Option<u8>> {
        self.reads += 1;
        let byte = self.data.get(self.index).copied();
        if byte.is_some() {
            self.index += 1;
        }
        Ok(byte)
    }
}

#[test]
fn the_total_deadline_is_checked_before_every_byte() {
    let base = Instant::now();
    let deadline = base + Duration::from_secs(3);
    let mut source = SliceSource::new(b"{\"v\": 2}\n");
    let mut ticks = 0;
    let error = read_bounded_line(&mut source, deadline, || {
        let now = base + Duration::from_secs(ticks);
        ticks += 1;
        now
    })
    .unwrap_err();
    assert_eq!(code(&error), ResultCode::DeadlineExceeded);
    assert_eq!(source.reads, 3, "a drip cannot read past the deadline");
}

#[test]
fn a_deadline_reached_exactly_is_expired() {
    let base = Instant::now();
    let deadline = base;
    let mut source = SliceSource::new(b"x\n");
    let error = read_bounded_line(&mut source, deadline, || base).unwrap_err();
    assert_eq!(code(&error), ResultCode::DeadlineExceeded);
    assert_eq!(source.reads, 0);
}

#[test]
fn an_empty_connection_is_empty_request() {
    let base = Instant::now();
    let mut source = SliceSource::new(b"");
    let error = read_bounded_line(&mut source, base + Duration::from_secs(1), || base).unwrap_err();
    assert_eq!(code(&error), ResultCode::EmptyRequest);
}

#[test]
fn a_closed_mid_frame_is_deadline_exceeded() {
    let base = Instant::now();
    let mut source = SliceSource::new(b"{\"v\": 2");
    let error = read_bounded_line(&mut source, base + Duration::from_secs(1), || base).unwrap_err();
    assert_eq!(code(&error), ResultCode::DeadlineExceeded);
}

#[test]
fn an_overlimit_frame_is_rejected_before_a_complete_line() {
    let base = Instant::now();
    let data = vec![b'x'; MAX_MESSAGE + 1];
    let mut source = SliceSource::new(&data);
    let error = read_bounded_line(&mut source, base + Duration::from_secs(1), || base).unwrap_err();
    assert_eq!(code(&error), ResultCode::Overlimit);
    assert_eq!(source.reads, MAX_MESSAGE + 1);
}

#[test]
fn a_frame_of_exactly_the_bound_is_accepted() {
    let base = Instant::now();
    let data = vec![b'x'; MAX_MESSAGE - 1];
    let mut line = data;
    line.push(b'\n');
    let mut source = SliceSource::new(&line);
    let frame = read_bounded_line(&mut source, base + Duration::from_secs(1), || base)
        .expect("exactly the bound");
    assert_eq!(frame.len(), MAX_MESSAGE);
    assert_eq!(frame.last(), Some(&b'\n'));
}

#[test]
fn concurrent_consumers_see_exactly_one_success() {
    let registry = Arc::new(ReplayRegistry::default());
    let barrier = Arc::new(Barrier::new(8));
    let workers: Vec<_> = (0..8)
        .map(|_| {
            let registry = registry.clone();
            let barrier = barrier.clone();
            thread::spawn(move || {
                barrier.wait();
                registry.consume("r-1", 100, 100)
            })
        })
        .collect();
    let results: Vec<_> = workers
        .into_iter()
        .map(|worker| worker.join().expect("worker"))
        .collect();
    let successes = results.iter().filter(|result| result.is_ok()).count();
    let replays = results
        .iter()
        .filter(|result| {
            result
                .as_ref()
                .err()
                .is_some_and(|error| code(error) == ResultCode::Replay)
        })
        .count();
    assert_eq!(successes, 1, "exactly one consumer owns the request id");
    assert_eq!(replays, 7);
}
