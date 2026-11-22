//! Slint-generated window types.
//!
//! The binary and the headless tests share one generated copy so the config
//! bridge can set the same properties the running window uses.

use crate::account::AccountSurface;
use crate::overview::Tone;

slint::include_modules!();

impl AccountSurface for MainWindow {
    fn set_saved_account(&self, value: &str) {
        self.set_account_saved(value.into());
    }

    fn set_saved_detail(&self, value: &str) {
        self.set_account_saved_detail(value.into());
    }

    fn set_can_clear(&self, value: bool) {
        self.set_account_can_clear(value);
    }

    fn set_can_continue_login(&self, value: bool) {
        self.set_account_can_continue(value);
    }

    fn set_overview_label(&self, value: &str) {
        self.set_account_label(value.into());
    }

    fn set_overview_detail(&self, value: &str) {
        self.set_account_detail(value.into());
    }

    fn set_overview_tone(&self, tone: Tone) {
        self.set_account_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_overview_pulse(&self, value: bool) {
        self.set_account_pulse(value);
    }

    fn set_login_status(&self, value: &str) {
        self.set_login_status(value.into());
    }

    fn set_credential_status(&self, value: &str) {
        self.set_credential_status(value.into());
    }
}
