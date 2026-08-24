from __future__ import annotations

import sys
from pathlib import Path

INTEGRATION_ROOT = Path(__file__).parents[1] / "integrations" / "hummingbot"
if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))

from mirror_grid_plan import MirrorConfig, mirror_once  # noqa: E402


def test_mirror_grid_plan_replaces_target_atomically_on_source_change(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    target = tmp_path / "bot-data" / "derive_grid_plans.jsonl"
    source.write_text('{"plan_version": 1}\n', encoding="utf-8")
    config = MirrorConfig(source, target)

    signature = mirror_once(config)
    assert signature is not None
    assert target.read_text(encoding="utf-8") == '{"plan_version": 1}\n'
    assert mirror_once(config, signature) == signature

    source.write_text('{"plan_version": 2}\n', encoding="utf-8")
    next_signature = mirror_once(config, signature)
    assert next_signature != signature
    assert target.read_text(encoding="utf-8") == '{"plan_version": 2}\n'
