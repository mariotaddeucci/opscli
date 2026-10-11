"""Measure PtyTerminal throughput and event-loop latency under flood workloads.

Runs three workloads inside Textual ``run_test(size=(120, 40))`` for a fixed
sample window with a 1 ms ticker. With ``--runs N`` (default 3), prints per-run
rows and a min-max summary. Figures are only meaningful on the machine that
executes this script.

Usage (from the repository root):

    uv run --no-sync python scripts/measure_pty_throughput.py
    uv run --no-sync python scripts/measure_pty_throughput.py --seconds 20 --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from typing_extensions import override

from curupira.tui.pty_terminal import PtyTerminal


class _MeasureApp(App[None]):
    """Minimal host that mounts one ``PtyTerminal``."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self._argv = argv

    @override
    def compose(self) -> ComposeResult:
        yield PtyTerminal(self._argv)


@dataclass(frozen=True)
class _RunStats:
    """One sample window for a single workload."""

    bytes_per_second: float
    p50_ms: float
    p99_ms: float
    max_ms: float


def _percentile(ordered: list[float], fraction: float) -> float:
    """Return a nearest-rank percentile from a sorted sample."""
    if not ordered:
        return float("nan")
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _cpu_model() -> str:
    """Best-effort CPU model string for the measurement footer."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _machine_banner() -> str:
    """Describe the host that produced the numbers."""
    return (
        f"{platform.system()} {platform.release()} "
        f"({platform.machine()}), {_cpu_model()}, "
        f"{os.cpu_count() or '?'} CPUs, Python {platform.python_version()}"
    )


def _stats_from_gaps(bytes_per_second: float, gaps: list[float]) -> _RunStats:
    """Build latency stats from ticker gaps."""
    ordered = sorted(gaps)
    return _RunStats(
        bytes_per_second=bytes_per_second,
        p50_ms=_percentile(ordered, 0.50) * 1000.0,
        p99_ms=_percentile(ordered, 0.99) * 1000.0,
        max_ms=(ordered[-1] if ordered else float("nan")) * 1000.0,
    )


async def _measure_workload(
    argv: list[str],
    *,
    seconds: float,
    size: tuple[int, int],
) -> _RunStats:
    """Run one workload and return throughput plus loop-latency stats."""
    fed_bytes = 0
    original_feed = PtyTerminal._feed_bytes_under_budget

    def counting_feed(
        self: PtyTerminal,
        data: bytes | bytearray,
        deadline: float,
    ) -> tuple[int, bool]:
        nonlocal fed_bytes
        fed, budget_hit = original_feed(self, data, deadline)
        fed_bytes += fed
        return fed, budget_hit

    # Monkeypatch for byte accounting only; restored in ``finally``.
    PtyTerminal._feed_bytes_under_budget = counting_feed  # type: ignore[method-assign]
    gaps: list[float] = []
    try:
        app = _MeasureApp(argv)
        async with app.run_test(size=size) as pilot:
            terminal = app.query_one(PtyTerminal)
            for _ in range(200):
                if terminal.pid is not None:
                    break
                await pilot.pause(0.01)
            previous = time.perf_counter()
            deadline = previous + seconds

            async def _ticker() -> None:
                nonlocal previous
                while time.perf_counter() < deadline:
                    await asyncio.sleep(0.001)
                    now = time.perf_counter()
                    gaps.append(now - previous)
                    previous = now

            ticker = asyncio.create_task(_ticker())
            await ticker
            app.exit()
    finally:
        PtyTerminal._feed_bytes_under_budget = original_feed  # type: ignore[method-assign]

    return _stats_from_gaps(fed_bytes / seconds, gaps)


def _format_throughput(bytes_per_second: float) -> str:
    """Format a throughput as MB/s and MiB/s."""
    mb_s = bytes_per_second / 1_000_000.0
    mib_s = bytes_per_second / (1024.0 * 1024.0)
    return f"{mb_s:.3f} MB/s ({mib_s:.3f} MiB/s)"


def _format_range(values: list[float], *, unit: str, digits: int = 1) -> str:
    """Format a closed min-max interval."""
    low = min(values)
    high = max(values)
    if low == high:
        return f"{low:.{digits}f} {unit}"
    return f"{low:.{digits}f}-{high:.{digits}f} {unit}"


def _format_run_row(name: str, run_index: int, stats: _RunStats) -> str:
    """Format one per-run results row."""
    return (
        f"| `{name}` (run {run_index}) | {_format_throughput(stats.bytes_per_second)} | "
        f"{stats.p50_ms:.1f} ms / {stats.p99_ms:.1f} ms / {stats.max_ms:.1f} ms |"
    )


def _format_summary_row(name: str, samples: list[_RunStats]) -> str:
    """Format a min-max summary row across consecutive runs."""
    rates = [sample.bytes_per_second / 1_000_000.0 for sample in samples]
    p50s = [sample.p50_ms for sample in samples]
    p99s = [sample.p99_ms for sample in samples]
    maxima = [sample.max_ms for sample in samples]
    return (
        f"| `{name}` | {_format_range(rates, unit='MB/s', digits=3)} | "
        f"{_format_range(p50s, unit='ms')} / "
        f"{_format_range(p99s, unit='ms')} / "
        f"{_format_range(maxima, unit='ms')} |"
    )


async def _run(seconds: float, size: tuple[int, int], runs: int) -> int:
    """Execute the three workloads for ``runs`` consecutive samples."""
    with tempfile.TemporaryDirectory(prefix="curupira-pty-bench-") as tmp:
        blob = Path(tmp) / "forty.bin"
        blob.write_bytes(b"x" * (40 * 1024 * 1024))
        workloads: list[tuple[str, list[str]]] = [
            ("yes | head -c 50000000", ["bash", "-c", "yes | head -c 50000000"]),
            ("seq 2000000", ["bash", "-c", "seq 2000000"]),
            (f"cat {blob.name} (40 MiB)", ["bash", "-c", f"cat '{blob}'"]),
        ]
        print(f"Machine: {_machine_banner()}")
        print(
            f"Sample: Textual run_test size={size}, {seconds:g} s, 1 ms ticker, "
            f"{runs} consecutive run(s)"
        )
        print()
        print("| Workload | Throughput | Loop latency p50 / p99 / max |")
        print("| --- | --- | --- |")
        summaries: dict[str, list[_RunStats]] = {name: [] for name, _ in workloads}
        for run_index in range(1, runs + 1):
            for name, argv in workloads:
                stats = await _measure_workload(argv, seconds=seconds, size=size)
                summaries[name].append(stats)
                print(_format_run_row(name, run_index, stats))
                await asyncio.sleep(0.2)
        if runs > 1:
            print()
            print("| Workload | Throughput (min-max) | Loop p50 / p99 / max (min-max) |")
            print("| --- | --- | --- |")
            for name, _ in workloads:
                print(_format_summary_row(name, summaries[name]))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=20.0,
        help="Sample window per workload (default: 20)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Consecutive full passes over all workloads (default: 3)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=120,
        help="run_test width (default: 120)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=40,
        help="run_test height (default: 40)",
    )
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.runs <= 0:
        parser.error("--runs must be positive")
    return asyncio.run(_run(args.seconds, (args.width, args.height), args.runs))


if __name__ == "__main__":
    raise SystemExit(main())
