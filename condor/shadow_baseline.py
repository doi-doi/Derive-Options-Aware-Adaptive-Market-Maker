"""Run a bounded Stage 12 Derive-mainnet shadow baseline.

The command is an explicit human gate for public-data paper execution.  It
never constructs a private Derive client, never submits/cancels an exchange
order, and never applies self-tuning recommendations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from derive_options_mm.shadow import (
    SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
    MainnetPublicDataSource,
    ShadowConfig,
    ShadowEnvironmentError,
    require_shadow_environment,
)
from derive_options_mm.shadow_baseline import (
    BASELINE_BANNER,
    BaselineConfigChanged,
    ShadowBaselineSession,
)

from .shadow import parse_duration, resolve_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="shadow_competition_800_usdc.yml",
        help="shadow YAML path, or a name under configs/",
    )
    parser.add_argument(
        "--duration",
        default=None,
        type=parse_duration,
        help="bounded duration such as 6h, 12h, 24h, 48h, or a short smoke duration",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between public-data snapshots",
    )
    parser.add_argument(
        "--trade-transport",
        choices=("websocket", "rest"),
        default="websocket",
        help="public trade transport; WebSocket is preferred for the diagnostic",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=float,
        default=None,
        help="checkpoint interval in seconds; profile default is five minutes",
    )
    parser.add_argument("--cycles", type=int, default=None, help="stop after this many cycles")
    parser.add_argument(
        "--no-trades",
        action="store_true",
        help=(
            "skip the public trade-history request; conservative trade-through fills remain "
            "unavailable while touch sensitivity may still use BBO evidence"
        ),
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help=(
            "start a new clean session after marking an old session interrupted; "
            "virtual state is never merged or converted to real orders"
        ),
    )
    return parser


def _resume_notice(session_id: str, project_root: Path, config: ShadowConfig) -> None:
    """Close an old session as interrupted before starting a clean session.

    Full coordinator state is intentionally not reconstructed from a report:
    doing so could silently merge incompatible strategy history.  A new session
    is therefore the safe fallback promised by the Stage 12 contract.
    """

    report_root = Path(config.report_root).expanduser()
    old_root = report_root / session_id
    old_root.mkdir(parents=True, exist_ok=True)
    marker = old_root / "interrupted.json"
    marker.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "status": "INTERRUPTED",
                "reason": "RESUME_STARTED_NEW_CLEAN_SESSION",
                "note": "Coordinator state was not merged; virtual orders remain virtual.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"RESUME: marked {session_id} INTERRUPTED; starting a new clean shadow session",
        flush=True,
    )
    del project_root


async def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    profile_path = resolve_profile(args.profile, project_root)
    config = ShadowConfig.from_yaml(profile_path)
    updates: dict[str, object] = {"enabled": True}
    if args.checkpoint_interval is not None:
        if args.checkpoint_interval <= 0:
            raise ValueError("checkpoint interval must be positive")
        updates["checkpoint_interval_seconds"] = args.checkpoint_interval
    config = config.model_copy(update=updates)
    duration = args.duration if args.duration is not None else config.session_duration_seconds
    if duration <= 0:
        raise ValueError("duration must be positive")
    if args.interval <= 0:
        raise ValueError("interval must be positive")
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("cycles must be positive")
    if args.resume:
        _resume_notice(args.resume, project_root, config)

    source = MainnetPublicDataSource(
        trade_history_enabled=not args.no_trades,
        trade_transport=args.trade_transport,
        trade_sample_interval_seconds=args.interval,
        market_data_stale_seconds=config.market_data_stale_seconds,
    )
    session = ShadowBaselineSession(
        config,
        config_source_path=profile_path,
        project_root=project_root,
        trade_history_enabled=not args.no_trades,
    )
    session.assert_isolated()
    session.start()
    started = time.monotonic()
    print(BASELINE_BANNER, flush=True)
    print(SHADOW_ENVIRONMENT_CONSISTENCY_PASS, flush=True)
    print(f"SESSION ID: {session.session_id}", flush=True)
    print(f"PROFILE: {profile_path}", flush=True)
    print(f"CONFIG VERSION: {config.baseline_config_version}", flush=True)
    print(f"CONFIG HASH: {config.config_hash}", flush=True)
    print(f"STRATEGY CONFIG HASH: {config.strategy_config_hash}", flush=True)
    print(f"START EQUITY: {config.starting_equity_usdc} USDC", flush=True)
    print(f"MARKETS: {', '.join(config.markets)}", flush=True)
    print("FILL MODEL: conservative_trade_through + touch_optimistic sensitivity", flush=True)
    print(
        "PUBLIC TRADE EVIDENCE REQUEST: "
        + ("ENABLED" if not args.no_trades else "DISABLED"),
        flush=True,
    )
    print(
        "DASHBOARD: PYTHONPATH=src .venv/bin/streamlit run dashboard/app.py "
        f"-- --data-dir {Path(config.sqlite_path).expanduser().parent}",
        flush=True,
    )
    print("REAL EXCHANGE MUTATIONS: 0", flush=True)

    cycles = 0
    reason = "MANUAL_STOP"
    exit_code = 0
    try:
        while True:
            if args.cycles is not None and cycles >= args.cycles:
                reason = "CYCLE_LIMIT_COMPLETE"
                break
            if args.cycles is None and time.monotonic() - started >= duration:
                reason = "DURATION_COMPLETE"
                break
            frames, options = await source.fetch_bundle(config.markets)
            streams = [*frames.values()]
            if options is not None:
                streams.append({"environment": options.environment})
            require_shadow_environment(streams)
            session.run_cycle(frames)
            cycles += 1
            metrics = session.metrics()
            print(
                f"cycle={cycles} active_orders={metrics.get('active_orders')} "
                f"fills={metrics.get('fills')} volume={metrics.get('total_executed_notional')} "
                f"pnl_reconciliation={metrics.get('pnl_reconciliation_status')} "
                f"real_exchange_mutation_calls=0",
                flush=True,
            )
            if args.cycles is None and time.monotonic() - started >= duration:
                reason = "DURATION_COMPLETE"
                break
            await asyncio.sleep(args.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        reason = "MANUAL_STOP"
    except BaselineConfigChanged:
        reason = "CONFIG_CHANGE"
        exit_code = 2
    except ShadowEnvironmentError as exc:
        print(f"DATA FAILURE: {exc}", flush=True)
        reason = "ENVIRONMENT_INCONSISTENCY"
        exit_code = 2
    except Exception as exc:  # pragma: no cover - exercised by real API failures
        print(f"DATA FAILURE: {type(exc).__name__}: {exc}", flush=True)
        reason = "DATA_FAILURE"
        exit_code = 1
    finally:
        try:
            report = session.stop(reason=reason)
            summary = session.summary(reason=reason)
            print(session.format_final_output(summary), flush=True)
            print(f"REPORT: {report}", flush=True)
        finally:
            await source.close()
    return exit_code


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (FileNotFoundError, ValueError, ShadowEnvironmentError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
