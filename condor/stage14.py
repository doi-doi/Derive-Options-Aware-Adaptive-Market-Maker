"""Run the bounded Stage 14 mainnet-public-data economic shadow validation.

The Stage 13 profile is loaded exactly as validated.  This command only
enables the local virtual session for the current process; it never creates a
private Derive client, submits an order, cancels an order, or mutates an
account.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from derive_options_mm.shadow import (
    SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
    MainnetPublicDataSource,
    ShadowConfig,
    ShadowEnvironmentError,
    require_shadow_environment,
)
from derive_options_mm.shadow_baseline import BaselineConfigChanged, ShadowBaselineSession
from derive_options_mm.stage14 import (
    Stage14Config,
    Stage14EconomicValidator,
    validate_stage13_reference,
)

from .shadow import parse_duration, resolve_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="configs/shadow_competition_800_stage13.yml",
        help="validated Stage 13 shadow YAML path, or a name under configs/",
    )
    parser.add_argument(
        "--duration",
        default="6h",
        type=parse_duration,
        help="bounded Stage 14 duration from 2h to 6h; default is 6h",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between real public Derive snapshots",
    )
    parser.add_argument(
        "--trade-transport",
        choices=("websocket", "rest"),
        default="websocket",
        help="public trade transport; WebSocket is preferred",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="optional short cycle-limited smoke test; it is not an economic completion",
    )
    parser.add_argument(
        "--no-trades",
        action="store_true",
        help="disable public trade-history collection; conservative fills will be unavailable",
    )
    return parser


def _resolved_data_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


async def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    profile_path = resolve_profile(args.profile, project_root)
    source: MainnetPublicDataSource | None = None
    session: ShadowBaselineSession | None = None
    validator: Stage14EconomicValidator | None = None
    reason = "MANUAL_STOP"
    exit_code = 0

    config = ShadowConfig.from_yaml(profile_path)
    if not config.stage13.enabled:
        raise ValueError("Stage 14 requires the enabled validated Stage 13 profile")
    # The profile is intentionally disabled on disk. Explicit invocation is
    # the human gate for this paper-only session; no strategy setting changes.
    config = config.model_copy(update={"enabled": True})
    policy = Stage14Config()
    policy.validate_duration(args.duration, cycles=args.cycles)
    if args.interval <= 0:
        raise ValueError("interval must be positive")
    stage13_reference = validate_stage13_reference(config, project_root)

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
    validator = Stage14EconomicValidator(
        session,
        profile_path=profile_path,
        project_root=project_root,
        policy=policy,
        stage13_reference=stage13_reference,
    )
    manifest = validator.prepare()
    session.assert_isolated()
    session.start()
    validator.start(session._start_epoch)
    started = time.monotonic()
    print("STAGE 14 — EVIDENCE-BASED ECONOMIC SHADOW VALIDATION", flush=True)
    print(SHADOW_ENVIRONMENT_CONSISTENCY_PASS, flush=True)
    print("STAGE 14 CONFIG FROZEN: PASS", flush=True)
    print(f"SESSION ID: {session.session_id}", flush=True)
    print(f"PROFILE: {profile_path}", flush=True)
    print(f"CONFIG HASH: {manifest['config_hash']}", flush=True)
    print(f"STAGE13 BEHAVIOR HASH: {manifest['stage13_behavior_hash']}", flush=True)
    print(f"STARTING PAPER EQUITY: {config.starting_equity_usdc} USDC", flush=True)
    print(f"ASSET STATUS: {manifest['asset_execution_status']}", flush=True)
    print("DATA: REAL DERIVE MAINNET PUBLIC DATA", flush=True)
    print("EXECUTION: SHADOW / PAPER ONLY", flush=True)
    print("FILL MODEL: CONSERVATIVE TRADE-THROUGH + TOUCH-OPTIMISTIC SENSITIVITY", flush=True)
    print(
        "PUBLIC TRADE EVIDENCE REQUEST: " + ("ENABLED" if not args.no_trades else "DISABLED"),
        flush=True,
    )
    print("PRIVATE DERIVE TRADING CLIENT: NOT ENABLED", flush=True)
    print("REAL EXCHANGE MUTATIONS: 0", flush=True)
    maximum_end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + args.duration))
    print(
        f"MAXIMUM END: {maximum_end}",
        flush=True,
    )
    print(f"STAGE 14 REPORT ROOT: {validator.root}", flush=True)
    print(
        "DASHBOARD: PYTHONPATH=src:. .venv/bin/streamlit run dashboard/app.py "
        f"--server.headless true --server.port 8502 -- --data-dir {validator.data_dir}",
        flush=True,
    )

    cycles = 0
    try:
        while True:
            if args.cycles is not None and cycles >= args.cycles:
                reason = "CYCLE_LIMIT_TEST"
                break
            if args.cycles is None and time.monotonic() - started >= args.duration:
                reason = "MAXIMUM_6_HOUR_WINDOW"
                break
            frames, options = await source.fetch_bundle(config.markets)
            streams = [*frames.values()]
            if options is not None:
                streams.append({"environment": options.environment})
            require_shadow_environment(streams)
            session.run_cycle(frames)
            cycles += 1
            validator.record_checkpoint()
            metrics = session.metrics()
            print(
                f"cycle={cycles} elapsed={validator._elapsed(time.time()):.1f}s "
                f"active_orders={metrics.get('active_orders')} "
                f"conservative_fills={metrics.get('fills')} "
                f"touch_fills={metrics.get('touch_optimistic_metrics', {}).get('fills')} "
                f"volume={metrics.get('total_executed_notional')} "
                "real_exchange_mutation_calls=0",
                flush=True,
            )
            if args.cycles is None and validator.should_early_stop():
                reason = "EARLY_EVIDENCE_SUFFICIENT"
                break
            if args.cycles is None and time.monotonic() - started >= args.duration:
                reason = "MAXIMUM_6_HOUR_WINDOW"
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
    except Exception as exc:  # pragma: no cover - exercised by live API failures
        print(f"DATA FAILURE: {type(exc).__name__}: {exc}", flush=True)
        reason = "DATA_FAILURE"
        exit_code = 1
    finally:
        if source is not None:
            try:
                await source.close()
            except Exception as exc:  # pragma: no cover - defensive network cleanup
                print(f"PUBLIC SOURCE CLOSE FAILURE: {type(exc).__name__}: {exc}", flush=True)
                exit_code = max(exit_code, 1)
        if session is not None and validator is not None:
            try:
                report = session.stop(reason=reason)
                summary = validator.finalize(report, reason=reason)
                print(validator.format_final_output(summary), flush=True)
                print(f"REPORT: {validator.root / 'summary.md'}", flush=True)
                print(f"SUMMARY JSON: {validator.root / 'summary.json'}", flush=True)
            except Exception as exc:  # pragma: no cover - preserves failure evidence when possible
                print(f"REPORT FAILURE: {type(exc).__name__}: {exc}", flush=True)
                exit_code = max(exit_code, 1)
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
