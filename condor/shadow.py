"""Run the Derive mainnet public-data shadow validation session.

The command enables only the virtual shadow engine for the current process.
The committed profile remains disabled by default, and this module has no
private Derive client or exchange mutation path.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

from derive_options_mm.shadow import (
    SHADOW_BANNER,
    SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
    MainnetPublicDataSource,
    ShadowConfig,
    ShadowEnvironmentError,
    ShadowSession,
    require_shadow_environment,
)

_DURATION_PATTERN = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[smhd])$", re.I)


def parse_duration(value: str) -> float:
    """Parse a compact duration such as ``15m`` or ``48h``."""

    match = _DURATION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("duration must use seconds, minutes, hours, or days")
    multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86_400.0}[match.group("unit").lower()]
    return float(match.group("value")) * multiplier


def resolve_profile(value: str, project_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    profile_name = candidate.name
    shadow_name = profile_name if profile_name.startswith("shadow_") else f"shadow_{profile_name}"
    candidates = (
        project_root / "configs" / shadow_name,
        project_root / "configs" / f"{shadow_name}.yml"
        if not shadow_name.endswith((".yml", ".yaml"))
        else project_root / "configs" / shadow_name,
        project_root / "configs" / value,
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"shadow profile not found: {value}")


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
        help="bounded run duration, for example 15m or 48h; defaults to profile duration",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0, help="seconds between public snapshots"
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
        "--trade-transport",
        choices=("websocket", "rest"),
        default="websocket",
        help="public trade transport; WebSocket is preferred",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    profile_path = resolve_profile(args.profile, project_root)
    config = ShadowConfig.from_yaml(profile_path)
    # The file is disabled by default. Explicit CLI invocation is the human
    # gate for a paper session; it cannot turn on exchange execution.
    config = config.model_copy(update={"enabled": True})
    duration = args.duration if args.duration is not None else config.session_duration_seconds
    if args.interval <= 0:
        raise ValueError("interval must be positive")
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("cycles must be positive")

    source = MainnetPublicDataSource(
        trade_history_enabled=not args.no_trades,
        trade_transport=args.trade_transport,
        trade_sample_interval_seconds=args.interval,
    )
    session = ShadowSession(config)
    started = time.monotonic()
    session.start()
    print(SHADOW_BANNER, flush=True)
    print(SHADOW_ENVIRONMENT_CONSISTENCY_PASS, flush=True)
    print(f"SESSION: {session.session_id}", flush=True)
    print(f"PROFILE: {profile_path}", flush=True)
    print(f"MARKETS: {', '.join(config.markets)}", flush=True)
    print("REAL EXCHANGE ACTIONS: 0", flush=True)

    cycles = 0
    try:
        while True:
            if args.cycles is not None and cycles >= args.cycles:
                break
            if args.cycles is None and time.monotonic() - started >= duration:
                break
            frames, options = await source.fetch_bundle(config.markets)
            status = (
                require_shadow_environment([*frames.values(), {"environment": options.environment}])
                if options
                else require_shadow_environment(frames.values())
            )
            print(status.to_record()["message"], flush=True)
            session.run_cycle(frames)
            cycles += 1
            metrics = session.engine.metrics()
            print(
                f"cycle={cycles} active_orders={metrics['active_orders']} "
                f"fills={metrics['fills']} paper_equity={metrics['paper_equity']} "
                f"real_exchange_mutation_calls={metrics['real_exchange_mutation_calls']}",
                flush=True,
            )
            if args.cycles is None and time.monotonic() - started >= duration:
                break
            await asyncio.sleep(args.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        reason = "INTERRUPTED"
    except ShadowEnvironmentError:
        session.stop(reason="ENVIRONMENT_ERROR")
        raise
    else:
        reason = "DURATION_COMPLETE" if args.cycles is None else "CYCLE_LIMIT_COMPLETE"
    finally:
        await source.close()
    report = session.stop(reason=reason)
    print(f"REPORT: {report}", flush=True)
    return 0


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
