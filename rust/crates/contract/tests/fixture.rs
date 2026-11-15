use std::collections::BTreeSet;

use um_contract::ContractFixture;

#[test]
fn generated_fixture_describes_gateway_and_console_endpoints() {
    let fixture = ContractFixture::load_default().expect("generated fixture loads");
    fixture.validate().expect("generated fixture is valid");

    assert!(fixture.metadata.fixture);
    assert!(!fixture.endpoints.is_empty());

    let endpoint_surfaces: BTreeSet<&str> = fixture
        .endpoints
        .iter()
        .map(|endpoint| endpoint.surface.as_str())
        .collect();
    assert_eq!(endpoint_surfaces.len(), fixture.metadata.surfaces.len());
    for surface in &fixture.metadata.surfaces {
        assert!(
            endpoint_surfaces.contains(surface.as_str()),
            "metadata surface has no endpoint: {surface}"
        );
    }

    for path in &fixture.cloudflare.share_paths {
        assert!(
            fixture
                .endpoints
                .iter()
                .any(|endpoint| &endpoint.path == path),
            "share path is missing from the generated endpoints: {path}"
        );
    }

    let mut target_ports: Vec<u16> = fixture.ports.target.values().copied().collect();
    target_ports.sort_unstable();
    target_ports.dedup();
    assert_eq!(fixture.ports.production, target_ports);
    assert!(fixture.ports.legacy_aliases.is_empty());
}

#[test]
fn generated_fixture_covers_errors_sse_and_boundaries() {
    let fixture = ContractFixture::load_default().expect("generated fixture loads");
    fixture.validate().expect("generated fixture is valid");

    assert!(fixture.coverage.values().all(|covered| *covered));
    assert!(fixture
        .errors
        .iter()
        .any(|group| group.status.is_some_and(|status| status >= 400)));
    let error_surfaces: BTreeSet<&str> = fixture
        .errors
        .iter()
        .map(|group| group.surface.as_str())
        .collect();
    assert!(error_surfaces.len() >= 2);

    assert!(!fixture.sse.gateway.responses.events.is_empty());
    assert!(fixture
        .sse
        .gateway
        .responses
        .events
        .iter()
        .all(|event| event.starts_with("response.")));
    assert!(!fixture.sse.gateway.messages.terminal_event.is_empty());
    assert!(!fixture.sse.gateway.messages.error_event.is_empty());

    assert!(!fixture.boundaries.timeouts.is_empty());
    assert!(fixture
        .boundaries
        .timeouts
        .iter()
        .all(|timeout| timeout.value > 0.0 && !timeout.source.is_empty()));
    assert!(!fixture.boundaries.cancellation.is_empty());
    assert!(!fixture.boundaries.idempotency.is_empty());
    assert!(!fixture.boundaries.retry.retryable_statuses.is_empty());
    assert!(!fixture.boundaries.side_effects.is_empty());
    assert!(!fixture.boundaries.forbidden.is_empty());

    assert!(!fixture.broker.result_codes.is_empty());
    assert!(!fixture.broker.management_actions.is_empty());
    assert!(!fixture.broker.instance_actions.is_empty());
    assert!(fixture.key_management.key_concurrency.default > 0);
    assert!(!fixture.metering.usage_sources.is_empty());
    assert!(fixture.display.grant_ttl_seconds > 0.0);
    assert!(!fixture.share_auth.chat_fields.is_empty());
}

#[test]
fn generated_fixture_rejects_a_stale_source_hash() {
    let mut fixture = ContractFixture::load_default().expect("generated fixture loads");
    fixture
        .metadata
        .source_hashes
        .insert("stale/source.py".to_string(), "0".repeat(64));
    fixture.validate().expect("well-formed source hash");

    fixture
        .metadata
        .source_hashes
        .insert("stale/source.py".to_string(), "not-a-hash".to_string());
    assert!(fixture.validate().is_err());
}
