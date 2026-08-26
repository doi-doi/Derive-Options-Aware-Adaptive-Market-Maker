"""Stage 9 configuration, history, validation, and secret-safety tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dashboard.config_schema import DashboardConfig, environment_preset  # noqa: E402
from dashboard.config_store import ConfigStore  # noqa: E402
from dashboard.config_validation import (  # noqa: E402
    config_hash,
    redact_secrets,
    validate_and_diff,
)


def _store(tmp_path: Path, *, controller: bool = False) -> ConfigStore:
    root = tmp_path / "project"
    configs = root / "configs"
    configs.mkdir(parents=True)
    profile = PROJECT_ROOT / "configs" / "competition_800_usdc.yml"
    strategy = PROJECT_ROOT / "configs" / "stage9_strategy.yml"
    (configs / profile.name).write_text(profile.read_text(encoding="utf-8"), encoding="utf-8")
    (configs / strategy.name).write_text(strategy.read_text(encoding="utf-8"), encoding="utf-8")
    controller_path = configs / "derive_adaptive_grid_controller.yml" if controller else None
    return ConfigStore(
        configs / profile.name,
        strategy_path=configs / strategy.name,
        controller_path=controller_path,
    )


def test_dashboard_load_and_staging_do_not_mutate_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before_profile = store.profile_path.read_text(encoding="utf-8")
    before_strategy = store.strategy_path.read_text(encoding="utf-8")
    saved = store.load()
    staged = DashboardConfig.model_validate(saved.to_record())
    staged = DashboardConfig(
        competition=staged.competition.model_copy(update={"target_order_notional": 65.0}),
        strategy=staged.strategy,
    )

    assert store.profile_path.read_text(encoding="utf-8") == before_profile
    assert store.strategy_path.read_text(encoding="utf-8") == before_strategy
    assert staged.competition.target_order_notional == 65


def test_invalid_relationships_block_validation() -> None:
    base = ConfigStore(
        PROJECT_ROOT / "configs" / "competition_800_usdc.yml",
        strategy_path=PROJECT_ROOT / "configs" / "stage9_strategy.yml",
    ).load()
    raw = base.to_record()
    raw["competition"]["portfolio_max_gross_notional"] = raw["competition"][
        "portfolio_soft_gross_notional"
    ]
    validation, changes = validate_and_diff(base.to_record(), raw)
    assert validation.valid is False
    assert changes


def test_apply_history_hash_and_rollback_are_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.load()
    first = DashboardConfig(
        competition=saved.competition.model_copy(update={"refresh_price_tolerance_bps": 15.0}),
        strategy=saved.strategy,
    )
    result1 = store.apply(first, operator_note="increase deadband for review")
    assert result1.version == 1
    assert store.load().competition.refresh_price_tolerance_bps == 15
    second = DashboardConfig(
        competition=first.competition.model_copy(update={"minimum_replace_interval_seconds": 90.0}),
        strategy=first.strategy,
    )
    result2 = store.apply(second, operator_note="test cooldown")
    assert result2.version == 2
    rollback = store.apply_version(1, operator_note="restore v1")
    assert rollback.version == 3
    assert store.load().competition.refresh_price_tolerance_bps == 15
    assert store.load().competition.minimum_replace_interval_seconds == 60
    assert len(store.load_history()) == 3
    events = [
        json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["version"] for event in events] == [1, 2, 3]
    assert store.profile_path.with_suffix(".yml.bak").exists()


def test_secret_redaction_and_hash_are_stable() -> None:
    value = {
        "api_key": "secret",
        "nested": {"wallet_private_key": "secret2", "safe": 1},
    }
    redacted = redact_secrets(value)
    assert redacted == {
        "api_key": "********",
        "nested": {"wallet_private_key": "********", "safe": 1},
    }
    assert config_hash(value) == config_hash(redacted)


def test_apply_generates_selected_environment_controller_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path, controller=True)
    saved = store.load()
    mainnet = DashboardConfig(
        competition=environment_preset(saved.competition, "mainnet"),
        strategy=saved.strategy,
    )

    store.apply(mainnet, operator_note="stage mainnet read-only profile")
    raw = yaml.safe_load(store.controller_path.read_text(encoding="utf-8"))

    assert raw["connector_name"] == "derive_perpetual"
    assert raw["environment"] == "mainnet"
    assert raw["options_environment"] == "mainnet"
    assert raw["execution_enabled"] is False
    assert raw["allow_mainnet_trading"] is False
    assert raw["testnet_order_scale"] is None
    assert raw["mainnet_canary_ack"] is None
