//! Slint-generated window types.
//!
//! The binary and the headless tests share one generated copy so the config
//! bridge can set the same properties the running window uses.

use std::rc::Rc;

use slint::{Model, ModelRc, VecModel};

use crate::account::{AccountSurface, VerifySurface};
use crate::maintenance::{MaintenanceSurface, MenuRow, RecordsView};
use crate::models::{
    CatalogSurface, ModelRow as ModelViewRow, ProbeHistoryRow as ProbeHistoryViewRow,
    ProbeRow as ProbeViewRow, ProbeSurface,
};
use crate::overview::{FaultRow as FaultViewRow, FaultSurface, Tone};
use crate::recent_issues::{IssueRow as RecentIssueViewRow, RecentIssuesSurface};
use crate::um_service::ServiceSurface;

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

    fn set_manual_takeover(&self, value: bool) {
        self.set_account_manual_takeover(value);
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

impl VerifySurface for MainWindow {
    fn set_verify_at_label(&self, value: &str) {
        self.set_verify_at(value.into());
    }

    fn set_verify_result_label(&self, value: &str) {
        self.set_verify_result(value.into());
    }

    fn set_verify_detail_text(&self, value: &str) {
        self.set_verify_detail(value.into());
    }

    fn set_verify_tone_index(&self, tone: Tone) {
        self.set_verify_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_verify_action_text(&self, value: &str) {
        self.set_verify_action_label(value.into());
    }
}

impl FaultSurface for MainWindow {
    fn set_fault_status(&self, status: &str) {
        self.set_upstream_fault_status(status.into());
    }

    fn set_fault_tone(&self, tone: Tone) {
        self.set_upstream_fault_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_fault_check(&self, check: &str) {
        self.set_upstream_fault_check(check.into());
    }

    fn set_fault_active(&self, active: Option<&FaultViewRow>) {
        self.set_upstream_fault_active(active.is_some());
        let (title, detail) = match active {
            Some(row) => (row.title.as_str(), row.detail.as_str()),
            None => ("", ""),
        };
        self.set_upstream_fault_active_title(title.into());
        self.set_upstream_fault_active_detail(detail.into());
    }

    fn set_fault_history(&self, rows: &[FaultViewRow]) {
        let rows: Vec<FaultRow> = rows
            .iter()
            .map(|row| FaultRow {
                title: row.title.as_str().into(),
                detail: row.detail.as_str().into(),
                evidence: row.evidence.as_str().into(),
            })
            .collect();
        self.set_upstream_fault_history(ModelRc::from(Rc::new(VecModel::from(rows))));
    }
}

impl ServiceSurface for MainWindow {
    fn set_service_state(&self, value: &str) {
        self.set_um_service_state(value.into());
    }

    fn set_service_detail(&self, value: &str) {
        self.set_um_service_detail(value.into());
    }

    fn set_service_tone(&self, tone: Tone) {
        self.set_um_service_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_service_checked_at(&self, value: &str) {
        self.set_um_service_checked_at(value.into());
    }

    fn set_service_can_start(&self, value: bool) {
        self.set_um_service_can_start(value);
    }

    fn set_service_gateway_missing(&self, value: bool) {
        self.set_um_service_gateway_missing(value);
    }

    fn set_service_gateway_detail(&self, value: &str) {
        self.set_um_service_gateway_detail(value.into());
    }

    fn set_service_can_start_gateway(&self, value: bool) {
        self.set_um_service_can_start_gateway(value);
    }

    fn set_service_start_status(&self, value: &str) {
        self.set_um_service_start_status(value.into());
    }

    fn set_service_start_tone(&self, tone: Tone) {
        self.set_um_service_start_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }
}

impl CatalogSurface for MainWindow {
    fn set_models(&self, rows: &[ModelViewRow]) {
        let rows: Vec<ModelRow> = rows
            .iter()
            .map(|row| ModelRow {
                id: row.id.as_str().into(),
                name: row.name.as_str().into(),
                context: row.context.as_str().into(),
                efforts: row.efforts.as_str().into(),
                last_test: row.last_test.as_str().into(),
                detail: row.detail.as_str().into(),
            })
            .collect();
        self.set_um_models(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn set_status(&self, status: &str) {
        self.set_um_model_status(status.into());
    }

    fn set_tone(&self, tone: Tone) {
        self.set_um_model_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_stale(&self, stale: bool) {
        self.set_um_model_stale(stale);
    }

    fn set_empty(&self, empty: bool) {
        self.set_um_model_empty(empty);
    }

    fn set_loading(&self, loading: bool) {
        self.set_um_model_loading(loading);
    }

    fn selected_model(&self) -> String {
        self.get_selected_model().to_string()
    }

    fn set_selected_model(&self, id: &str) {
        self.set_selected_model(id.into());
    }

    fn set_detail(&self, title: &str, body: &str) {
        self.set_um_model_detail_title(title.into());
        self.set_um_model_detail(body.into());
    }
}

impl ProbeSurface for MainWindow {
    fn probe_rows(&self) -> Vec<ProbeViewRow> {
        let rows = self.get_probe_models();
        (0..rows.row_count())
            .filter_map(|index| rows.row_data(index))
            .map(|row| ProbeViewRow {
                model_id: row.model_id.to_string(),
                name: row.name.to_string(),
                state: row.state.to_string(),
                summary: row.summary.to_string(),
                tone: tone_from_index(row.tone),
                selected: row.selected,
            })
            .collect()
    }

    fn set_probe_rows(&self, rows: &[ProbeViewRow]) {
        let rows: Vec<ProbeRow> = rows
            .iter()
            .map(|row| ProbeRow {
                model_id: row.model_id.as_str().into(),
                name: row.name.as_str().into(),
                state: row.state.as_str().into(),
                summary: row.summary.as_str().into(),
                tone: tone_index(row.tone),
                selected: row.selected,
            })
            .collect();
        self.set_probe_models(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn probe_selection(&self) -> Vec<String> {
        self.get_probe_selected()
            .iter()
            .map(|id| id.to_string())
            .collect()
    }

    fn set_probe_selection(&self, ids: &[String]) {
        let values: Vec<slint::SharedString> = ids.iter().map(|id| id.as_str().into()).collect();
        self.set_probe_selected(ModelRc::from(Rc::new(VecModel::from(values))));
    }

    fn set_probe_status(&self, status: &str) {
        self.set_probe_status(status.into());
    }

    fn set_probe_tone(&self, tone: Tone) {
        self.set_probe_tone(tone_index(tone));
    }

    fn set_probe_progress(&self, progress: &str) {
        self.set_probe_progress(progress.into());
    }

    fn set_probe_budget(&self, budget: &str) {
        self.set_probe_budget(budget.into());
    }

    fn set_probe_running(&self, running: bool) {
        self.set_probe_running(running);
    }

    fn set_probe_stop_requested(&self, requested: bool) {
        self.set_probe_stop_requested(requested);
    }

    fn set_probe_history(&self, rows: &[ProbeHistoryViewRow]) {
        let rows: Vec<ProbeHistoryRow> = rows
            .iter()
            .map(|row| ProbeHistoryRow {
                time: row.time.as_str().into(),
                model: row.model.as_str().into(),
                result: row.result.as_str().into(),
                summary: row.summary.as_str().into(),
                tone: tone_index(row.tone),
            })
            .collect();
        self.set_probe_history(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn set_probe_loading(&self, loading: bool) {
        self.set_probe_loading(loading);
    }
}

fn tone_index(tone: Tone) -> i32 {
    match tone {
        Tone::Good => 0,
        Tone::Warn => 1,
        Tone::Bad => 2,
        Tone::Neutral => 3,
    }
}

fn tone_from_index(index: i32) -> Tone {
    match index {
        0 => Tone::Good,
        1 => Tone::Warn,
        2 => Tone::Bad,
        _ => Tone::Neutral,
    }
}

impl MaintenanceSurface for MainWindow {
    fn set_menu_rows(&self, rows: &[MenuRow]) {
        let rows: Vec<MaintenanceRow> = rows
            .iter()
            .map(|row| MaintenanceRow {
                action: row.action.index(),
                label: row.label.as_str().into(),
                impact: row.impact.as_str().into(),
                enabled: row.enabled,
                reason: row.reason.as_str().into(),
                confirm_title: row.confirm.title.as_str().into(),
                confirm_body: row.confirm.body.as_str().into(),
                confirm_label: row.confirm.label.as_str().into(),
                danger: row.confirm.danger,
            })
            .collect();
        self.set_maintenance_items(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn set_records(&self, records: &RecordsView) {
        self.set_maintenance_records_status(records.status.as_str().into());
        self.set_maintenance_records_tone(match records.tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
        self.set_maintenance_records_not_applicable(records.not_applicable);
        let rows: Vec<MaintenanceRecordRow> = records
            .rows
            .iter()
            .map(|row| MaintenanceRecordRow {
                at: row.at.as_str().into(),
                action: row.action.as_str().into(),
                result: row.result.as_str().into(),
            })
            .collect();
        self.set_maintenance_records(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn set_status(&self, status: &str, tone: Tone) {
        self.set_maintenance_status(status.into());
        self.set_maintenance_status_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }
}

impl RecentIssuesSurface for MainWindow {
    fn set_issue_rows(&self, rows: &[RecentIssueViewRow]) {
        let rows: Vec<IssueRow> = rows
            .iter()
            .map(|row| IssueRow {
                title: row.title.as_str().into(),
                count: row.count.as_str().into(),
                detail: row.detail.as_str().into(),
                tone: tone_index(row.tone),
                evidence_result: row.evidence.map_or(-1, |query| query.result),
                evidence_window: row.evidence.map_or(0, |query| query.window),
                evidence_note: row.evidence_note.as_str().into(),
            })
            .collect();
        self.set_recent_issues(ModelRc::from(Rc::new(VecModel::from(rows))));
    }

    fn set_issue_status(&self, status: &str) {
        self.set_recent_issues_status(status.into());
    }

    fn set_issue_tone(&self, tone: Tone) {
        self.set_recent_issues_tone(tone_index(tone));
    }
}
