"""Weekly allocation: capital goes only to members beating buy-and-hold live.

Weekly re-allocation on thin live data is a noise-chasing machine unless the
bar is explicit. A member needs 20 daily samples, at least one entry, a total
return above the benchmark's over the same days, and a t-statistic of daily
excess returns above :func:`min_t` — 1.0 when two members compete, higher
with more, so adding members does not add lucky winners. Weight follows that t
(the edge's reliability, not its size), capped at 60%; whatever nobody earned
holds the benchmark.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

MIN_SAMPLES = 20
MIN_T = 1.0
MAX_WEIGHT = 0.60
HYSTERESIS = 0.10
# The desk launched with two competing members at t > 1.0. Under no edge each
# clears that ~16% of the time, so ~29% of weeks at least one of two looks like
# a winner by luck. min_t holds that family-wide rate as members are added.
BASE_MEMBERS = 2

# Members the desk judges but may not fund yet. Their trades happen on paper in
# another process, and no live order path exists for them. Lifting this is a
# code change in the live sub-project, never a config switch.
UNFUNDED = frozenset({"switchboard"})


def hold_unfunded(weights: dict[str, float], *, benchmark: str) -> tuple[dict[str, float], dict[str, float]]:
    """Move unfunded members' weight to the benchmark; report what they would have earned."""
    held = dict(weights)
    would: dict[str, float] = {}
    for name in sorted(UNFUNDED):
        if name not in held:
            continue
        weight = held[name]
        would[name] = round(weight, 6)
        if weight > 0:
            held[benchmark] = round(held.get(benchmark, 0.0) + weight, 6)
            held[name] = 0.0
    return held, would


def min_t(members: int) -> float:
    """The t a member must beat when ``members`` non-benchmark members compete."""
    normal = NormalDist()
    keep = normal.cdf(MIN_T) ** (BASE_MEMBERS / max(1, members))
    return max(MIN_T, normal.inv_cdf(keep))


@dataclass(frozen=True)
class MemberRecord:
    name: str
    samples: list[tuple[str, float]]
    entries: int


@dataclass
class Allocation:
    weights: dict[str, float]
    changed: bool
    reasons: dict[str, str] = field(default_factory=dict)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def allocate(
    records: list[MemberRecord], *, benchmark: str, previous: dict[str, float]
) -> Allocation:
    bench = next((record for record in records if record.name == benchmark), None)
    bench_equity = dict(bench.samples) if bench else {}
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    stats: dict[str, dict[str, Any]] = {}
    competing = sum(1 for record in records if record.name != benchmark)
    bar = min_t(competing)
    for record in records:
        if record.name == benchmark:
            continue
        mine = dict(record.samples)
        days = sorted(day for day in set(mine) & set(bench_equity) if mine[day] > 0 and bench_equity[day] > 0)
        stats[record.name] = {"samples": len(days), "entries": record.entries}
        if len(days) < MIN_SAMPLES:
            reasons[record.name] = f"{len(days)} daily samples (needs {MIN_SAMPLES})"
            continue
        if record.entries < 1:
            reasons[record.name] = "no entries yet"
            continue
        member_total = mine[days[-1]] / mine[days[0]] - 1
        bench_total = bench_equity[days[-1]] / bench_equity[days[0]] - 1
        stats[record.name]["excess_pct"] = round((member_total - bench_total) * 100, 3)
        if member_total <= bench_total:
            reasons[record.name] = (
                f"trails the benchmark by {(bench_total - member_total) * 100:.2f} points"
            )
            continue
        excess = [
            (mine[b] / mine[a] - 1) - (bench_equity[b] / bench_equity[a] - 1)
            for a, b in zip(days, days[1:])
        ]
        spread = statistics.stdev(excess) if len(excess) > 1 else 0.0
        if spread <= 0:
            reasons[record.name] = "excess returns never vary (t undefined)"
            continue
        t = statistics.mean(excess) / (spread / math.sqrt(len(excess)))
        stats[record.name]["t"] = round(t, 3)
        if t <= bar:
            reasons[record.name] = (
                f"t={t:.2f} is not above {bar:.2f} ({competing} members compete)"
            )
            continue
        scores[record.name] = t
        reasons[record.name] = f"qualifies: t={t:.2f}"

    total = sum(scores.values())
    weights = {name: min(MAX_WEIGHT, t / total) for name, t in scores.items()} if total > 0 else {}
    weights[benchmark] = max(0.0, 1.0 - sum(weights.values()))
    for record in records:
        weights.setdefault(record.name, 0.0)
    weights = {name: round(value, 6) for name, value in weights.items()}

    if previous:
        keys = set(weights) | set(previous)
        moved = max(abs(weights.get(k, 0.0) - previous.get(k, 0.0)) for k in keys)
        # Weights are floats: 0.6 - 0.5 is 0.0999…, which must still count as
        # the 10-point move the rule names.
        if moved < HYSTERESIS - 1e-9:
            return Allocation(dict(previous), False, reasons, stats)
    return Allocation(weights, weights != previous, reasons, stats)
