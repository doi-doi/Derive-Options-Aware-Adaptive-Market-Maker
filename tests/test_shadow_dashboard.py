"""Dashboard reader tests for persisted shadow sessions."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dashboard.shadow_reader import read_shadow_state  # noqa: E402
from derive_options_mm.shadow import ShadowConfig, ShadowSession  # noqa: E402


def test_dashboard_reader_survives_refresh_and_reads_latest_paper_state(tmp_path: Path) -> None:
    config = ShadowConfig(
        enabled=True,
        event_path=str(tmp_path / "shadow_execution_events.jsonl"),
        sqlite_path=str(tmp_path / "shadow_execution.sqlite3"),
        report_root=str(tmp_path / "reports"),
    )
    session = ShadowSession(config, session_id="dashboard-test")
    session.start(timestamp=1_700_000_000.0)
    session.stop(timestamp=1_700_000_001.0, reason="TEST")
    state = read_shadow_state(tmp_path)
    assert state.available is True
    assert state.session["session_id"] == "dashboard-test"
    assert state.session["execution_mode"] == "SHADOW"
    assert state.session["real_exchange_mutation_calls"] == 0
    assert state.metrics["orders_are_simulated"] is True
    assert state.event_path == tmp_path / "shadow_execution_events.jsonl"


def test_dashboard_reader_includes_stage14_latest_summary(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stage14 = tmp_path / "reports" / "stage14"
    stage14.mkdir(parents=True)
    (stage14 / "latest_summary.json").write_text(
        '{"stage": "STAGE14", "status": "RUNNING", "evidence": {"status": "DEVELOPING"}}\n',
        encoding="utf-8",
    )
    state = read_shadow_state(data_dir)
    assert state.stage14["stage"] == "STAGE14"
    assert state.stage14["evidence"]["status"] == "DEVELOPING"
