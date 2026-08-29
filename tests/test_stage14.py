"""Stage 14 economic-evidence and artifact-contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.shadow import ShadowConfig  # noqa: E402
from derive_options_mm.shadow_baseline import ShadowBaselineSession  # noqa: E402
from derive_options_mm.stage14 import (  # noqa: E402
    STAGE14_REQUIRED_FILES,
    Stage14Config,
    Stage14EconomicValidator,
    _directional_markout,
    assess_economic_evidence,
)


def _complete_markouts(count: int = 5) -> list[dict[str, object]]:
    return [
        {
            "model": "CONSERVATIVE",
            "horizon_seconds": horizon,
            "status": "COMPLETE",
            "markout_bps": 1.0,
        }
        for horizon in (30, 60)
        for _ in range(count)
    ]


def test_stage14_duration_policy_is_bounded_and_cycle_smokes_are_explicit() -> None:
    policy = Stage14Config()
    with pytest.raises(ValueError, match="at least 2 hours"):
        policy.validate_duration(3600)
    with pytest.raises(ValueError, match="cannot exceed 6 hours"):
        policy.validate_duration(6 * 3600 + 1)
    policy.validate_duration(30, cycles=3)


def test_stage14_evidence_status_exposes_pragmatic_targets_and_sample_counts() -> None:
    developing = assess_economic_evidence(
        {
            "fills": 2,
            "completed_cycles": 0,
            "state_observation_count": 10,
            "inventory_by_asset": {"SOL-USDC": {}},
        },
        _complete_markouts(1),
        2 * 3600,
    )
    assert developing.status == "DEVELOPING"
    assert developing.conservative_fills == 2
    assert developing.markout_30s_n == 1

    sufficient = assess_economic_evidence(
        {
            "fills": 20,
            "completed_cycles": 3,
            "state_observation_count": 100,
            "inventory_by_asset": {"SOL-USDC": {}},
        },
        _complete_markouts(),
        2 * 3600,
    )
    assert sufficient.status == "SUFFICIENT_FOR_DIAGNOSIS"
    assert sufficient.diagnostic_fill_target == 20


def test_directional_markout_is_sign_normalized() -> None:
    assert _directional_markout({"price": 100, "side": "buy"}, 100.1) == pytest.approx(10)
    assert _directional_markout({"price": 100, "side": "sell"}, 99.9) == pytest.approx(10)
    assert _directional_markout({"price": 100, "side": "buy"}, 99.9) == pytest.approx(-10)


def test_stage14_finalize_writes_required_session_artifacts(tmp_path: Path) -> None:
    config = ShadowConfig(
        enabled=True,
        markets=("BTC-USDC", "ETH-USDC"),
        enabled_markets=("ETH-USDC",),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        event_path=str(tmp_path / "shadow-events.jsonl"),
        report_root=str(tmp_path / "reports" / "base"),
        checkpoint_interval_seconds=1,
    )
    session = ShadowBaselineSession(
        config,
        session_id="stage14-artifact-test",
        project_root=tmp_path,
    )
    validator = Stage14EconomicValidator(
        session,
        profile_path=tmp_path / "profile.yml",
        project_root=tmp_path,
    )
    validator.prepare()
    session.start(timestamp=1_700_000_000.0)
    validator.start(session._start_epoch)
    report = session.stop(timestamp=1_700_000_001.0, reason="CYCLE_LIMIT_TEST")
    summary = validator.finalize(report, reason="CYCLE_LIMIT_TEST")

    artifact_root = tmp_path / "reports" / "stage14" / session.session_id
    assert set(STAGE14_REQUIRED_FILES).issubset({path.name for path in artifact_root.iterdir()})
    assert summary["safety"]["real_exchange_mutation_calls"] == 0
    assert summary["safety"]["private_derive_trading_client"] == "NOT_ENABLED"
    assert summary["fill_quality"]["markouts"]["300s"]["sample_count"] == 0
    assert (tmp_path / "reports" / "stage14" / "latest_summary.json").is_file()


def test_stage14_live_summary_refreshes_between_hourly_checkpoints(tmp_path: Path) -> None:
    config = ShadowConfig(
        enabled=True,
        markets=("BTC-USDC", "ETH-USDC"),
        enabled_markets=("ETH-USDC",),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        event_path=str(tmp_path / "shadow-events.jsonl"),
        report_root=str(tmp_path / "reports" / "base"),
        checkpoint_interval_seconds=300,
    )
    session = ShadowBaselineSession(
        config,
        session_id="stage14-live-refresh-test",
        project_root=tmp_path,
    )
    validator = Stage14EconomicValidator(
        session,
        profile_path=tmp_path / "profile.yml",
        project_root=tmp_path,
    )
    validator.prepare()
    session.start(timestamp=1_700_000_000.0)
    validator.start(session._start_epoch)

    validator.record_checkpoint(timestamp=1_700_000_001.0)

    live = json.loads(
        (tmp_path / "reports" / "stage14" / "latest_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert live["duration_seconds"] == pytest.approx(1.0)
    assert live["evidence"]["elapsed_seconds"] == pytest.approx(1.0)
    assert not (validator.root / "hourly_metrics.csv").exists()


def test_stage14_manifest_keeps_the_stage13_controls_visible(tmp_path: Path) -> None:
    config = ShadowConfig(
        enabled=True,
        starting_equity_usdc=800,
        markets=("BTC-USDC",),
        enabled_markets=("BTC-USDC",),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        event_path=str(tmp_path / "shadow-events.jsonl"),
        report_root=str(tmp_path / "reports" / "base"),
    )
    session = ShadowBaselineSession(
        config, session_id="stage14-manifest-test", project_root=tmp_path
    )
    validator = Stage14EconomicValidator(
        session,
        profile_path=tmp_path / "profile.yml",
        project_root=tmp_path,
    )
    manifest = validator.prepare()
    assert manifest["starting_paper_equity"] == 800
    assert manifest["execution_enabled"] is False
    assert manifest["allow_mainnet_trading"] is False
    assert manifest["post_only"] is True
    assert manifest["fill_models"]["primary"] == "conservative_trade_through"
