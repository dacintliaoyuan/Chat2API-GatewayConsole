//! #79: the local role keeps Python's 30 second auth-wait budget.
//!
//! The cloud role keeps 180 seconds. An explicit CLI/env value overrides the
//! role default and is clamped to 1..180; an invalid env value keeps the role
//! default instead of disabling the budget.

use std::time::Duration;

use um_gateway::config::{resolve_auth_wait_budget, GatewayConfig, Role};

#[test]
fn local_defaults_to_30_seconds_and_cloud_to_180() {
    let local = GatewayConfig {
        role: Role::Local,
        ..GatewayConfig::default()
    };
    assert_eq!(local.auth_wait_budget(), Duration::from_secs(30));
    let cloud = GatewayConfig {
        role: Role::Cloud,
        ..GatewayConfig::default()
    };
    assert_eq!(cloud.auth_wait_budget(), Duration::from_secs(180));
    assert_eq!(
        GatewayConfig {
            role: Role::Local,
            auth_wait_budget: Some(Duration::from_secs(45)),
            ..GatewayConfig::default()
        }
        .auth_wait_budget(),
        Duration::from_secs(45)
    );
}

#[test]
fn explicit_values_are_clamped_to_1_through_180() {
    assert_eq!(
        resolve_auth_wait_budget(Some(0.5), None, Role::Local),
        Duration::from_secs(1)
    );
    assert_eq!(
        resolve_auth_wait_budget(Some(60.0), None, Role::Local),
        Duration::from_secs(60)
    );
    assert_eq!(
        resolve_auth_wait_budget(Some(500.0), None, Role::Cloud),
        Duration::from_secs(180)
    );
    assert_eq!(
        resolve_auth_wait_budget(None, Some("0.5"), Role::Local),
        Duration::from_secs(1)
    );
    assert_eq!(
        resolve_auth_wait_budget(None, Some("60"), Role::Cloud),
        Duration::from_secs(60)
    );
    assert_eq!(
        resolve_auth_wait_budget(None, Some("500"), Role::Cloud),
        Duration::from_secs(180)
    );
}

#[test]
fn invalid_values_keep_the_role_default() {
    assert_eq!(
        resolve_auth_wait_budget(None, Some("not-a-number"), Role::Cloud),
        Duration::from_secs(180)
    );
    assert_eq!(
        resolve_auth_wait_budget(None, Some("NaN"), Role::Local),
        Duration::from_secs(30)
    );
    assert_eq!(
        resolve_auth_wait_budget(None, Some("inf"), Role::Local),
        Duration::from_secs(30)
    );
}

#[test]
fn the_cli_value_wins_over_the_environment() {
    assert_eq!(
        resolve_auth_wait_budget(Some(45.0), Some("120"), Role::Local),
        Duration::from_secs(45)
    );
    assert_eq!(
        resolve_auth_wait_budget(Some(0.5), Some("120"), Role::Cloud),
        Duration::from_secs(1)
    );
}
