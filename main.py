"""Launch-day Twitch audience forecaster (make.fun gta6-twitch-ccu-forecast-01).

Idea: a big launch's release-day curve, hour by hour, looks like the release-day
curves of earlier big launches once each curve is divided by its own first-hour
count. So for every past launch shipped inside the case file ("comparables")
this program builds the ratio curve r_h = viewers_h / viewers_1, and forecasts
the target hour's quantiles from the distribution of those ratios at that hour.

Two refinements, both tuned on held-out launches before this file was written:

* Comparables are weighted by how well their last eight pre-release hours
  (offsets -8..-1 against their own release day, in log space, relative to
  their own first hour) match the target case's same eight hours. A rolling
  midnight-local unlock ramps through the evening before the UTC day, while a
  single global daytime unlock sits near zero until it fires; the pre-release
  ramp is the fingerprint that tells those families apart, and the target's
  own history carries it. Weights are soft: w = 0.3 + 0.7 * exp(-mse / 0.32),
  so no comparable is ever fully believed or fully ignored.
* The spread of the log-ratio distribution is shrunk by a factor 0.85 around
  its median: the raw cross-launch spread overstates how uncertain one more
  launch is once the ramp fingerprint has done its sorting.

Fallback: if a case ships no usable comparable, the forecast is persistence
(hour 1 repeated) with a log spread that widens like sqrt(hours ahead).

Deterministic, standard library only, no network, no clock, no randomness.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
ZNORM = [-1.645, -0.674, 0.0, 0.674, 1.645]

# Tuned constants (see the module docstring for how they were chosen).
SIM_SIGMA = 0.4          # log-space scale of the pre-release ramp match
WEIGHT_FLOOR = 0.3       # every comparable keeps at least this share of a vote
SPREAD_SCALE = 0.85      # shrink the log-ratio spread around its median
PRE_LO, PRE_HI = -8, -1  # pre-release offsets used for the ramp fingerprint
MIN_COMMON = 4           # ramp points needed before a match score is trusted
DEFAULT_WEIGHT = 0.35    # weight when the ramp cannot be compared at all
MIN_DAY_HOURS = 20       # a comparable needs this many release-day hours
CALIBRATION_MAX = 24     # comparables used by the self-calibration pass


def to_epoch(text):
    """ISO-8601 timestamp -> epoch seconds (float)."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def hour_offset(epoch, day_start_epoch):
    return int(round((epoch - day_start_epoch) / 3600.0))


def comparable_curve(comp):
    """{hour offset from the comparable's day_start: viewers}."""
    start = to_epoch(comp["day_start"])
    curve = {}
    for point in comp.get("hourly") or []:
        viewers = point.get("viewers")
        if viewers is None:
            continue
        curve[hour_offset(to_epoch(point["from"]), start)] = float(viewers)
    return curve


def target_profile(case, day_start_epoch, p1):
    """The target's own log viewers relative to log p1, by hour offset."""
    profile = {0: 0.0}
    rows = case.get("history_hourly") or []
    for row in rows:
        viewers = row.get("viewers")
        if viewers is None:
            continue
        off = hour_offset(to_epoch(row["from"]), day_start_epoch)
        if off < 0:
            profile[off] = math.log(max(1.0, float(viewers))) - math.log(p1)
    pre = [off for off in range(PRE_LO, PRE_HI + 1) if off in profile]
    if len(pre) < MIN_COMMON:
        # The final case's history is minute reads, not hourly rows: bucket
        # the raw points into whole hours so the ramp fingerprint still has
        # its eight pre-release points when history_hourly runs short.
        buckets = {}
        for point in case.get("history") or []:
            viewers = point.get("viewers")
            if viewers is None:
                continue
            off = int((to_epoch(point["at"]) - day_start_epoch) // 3600)
            if off < 0:
                buckets.setdefault(off, []).append(float(viewers))
        for off, values in buckets.items():
            if off not in profile and values:
                mean = sum(values) / len(values)
                profile[off] = math.log(max(1.0, mean)) - math.log(p1)
    return profile


def ramp_weight(curve, profile):
    """Similarity vote of one comparable against the target's ramp profile."""
    if 0 not in curve or curve[0] <= 0:
        return 0.0
    log_c1 = math.log(curve[0])
    sq, n = 0.0, 0
    for off in range(PRE_LO, PRE_HI + 1):
        if off in curve and off in profile:
            diff = (math.log(max(1.0, curve[off])) - log_c1) - profile[off]
            sq += diff * diff
            n += 1
    if n < MIN_COMMON:
        return DEFAULT_WEIGHT
    mse = sq / n
    return WEIGHT_FLOOR + (1.0 - WEIGHT_FLOOR) * math.exp(
        -mse / (2.0 * SIM_SIGMA * SIM_SIGMA))


def weighted_quantile(sorted_pairs, tau):
    """tau-quantile of (value, weight) pairs, linear in the value."""
    total = sum(w for _, w in sorted_pairs)
    if total <= 0:
        return sorted_pairs[len(sorted_pairs) // 2][0]
    goal = tau * total
    acc = 0.0
    prev_value, prev_acc = sorted_pairs[0][0], 0.0
    for value, w in sorted_pairs:
        acc += w
        if acc >= goal:
            if w <= 0:
                return value
            frac = (goal - prev_acc) / w
            return prev_value + frac * (value - prev_value)
        prev_value, prev_acc = value, acc
    return sorted_pairs[-1][0]


def persistence_row(p1, hour):
    spread = 0.15 * math.sqrt(hour - 1)
    return [p1 * math.exp(z * spread) for z in ZNORM]


def weighted_pool(curves, profile):
    return [(curve, ramp_weight(curve, profile)) for curve in curves]


def ratio_forecast(p1, pool, hours):
    """{hour: [q05..q95]} from a weighted pool of comparable ratio curves."""
    out = {}
    for hour in hours:
        if not pool:
            out[hour] = persistence_row(p1, hour)
            continue
        pairs = sorted(
            (math.log(curve[hour - 1] / curve[0]), w)
            for curve, w in pool
            if (hour - 1) in curve and curve[hour - 1] > 0
        )
        if not pairs:
            out[hour] = persistence_row(p1, hour)
            continue
        logs = [weighted_quantile(pairs, tau) for tau in LEVELS]
        median = logs[2]
        logs = [median + (v - median) * SPREAD_SCALE for v in logs]
        total_weight = sum(w for _, w in pairs)
        if total_weight < 3.0:
            # Thin evidence for this hour: lean halfway back to the
            # persistence spread instead of trusting a sharp shape
            # drawn from almost no comparable weight.
            base = persistence_row(p1, hour)
            logs = [
                0.5 * v + 0.5 * (math.log(b) - math.log(p1))
                for v, b in zip(logs, base)
            ]
        out[hour] = [p1 * math.exp(v) for v in logs]
    return out


def curve_profile(curve):
    """Log viewers relative to the curve's own hour 1, by offset."""
    log_c1 = math.log(curve[0])
    return {off: math.log(max(1.0, v)) - log_c1 for off, v in curve.items()
            if off < 0}


def self_calibration(curves, pool):
    """Per-hour median log bias of the model, measured inside the pool.

    Every comparable with a full release day is forecast from the rest of
    the pool exactly the way the target will be; the median miss per hour
    is a bias this method provably carries on launches like these, so the
    target's forecast is shifted by it. Deterministic: the calibration set
    is the 24 highest-weighted qualifying comparables, in pool order.
    """
    ranked = sorted(range(len(curves)),
                    key=lambda i: (-pool[i][1], i))
    chosen = []
    for i in ranked:
        curve = curves[i]
        have = sum(1 for off in range(24) if off in curve)
        if have >= 23:
            chosen.append(i)
        if len(chosen) >= CALIBRATION_MAX:
            break
    residuals = {h: [] for h in range(2, 25)}
    for i in chosen:
        curve = curves[i]
        rest = [c for j, c in enumerate(curves) if j != i]
        sub_pool = weighted_pool(rest, curve_profile(curve))
        fc = ratio_forecast(curve[0], sub_pool, range(2, 25))
        for h in range(2, 25):
            if (h - 1) in curve and curve[h - 1] > 0:
                residuals[h].append(
                    math.log(curve[h - 1]) - math.log(fc[h][2]))
    bias = {}
    for h, vals in residuals.items():
        if vals:
            vals = sorted(vals)
            bias[h] = vals[len(vals) // 2]
        else:
            bias[h] = 0.0
    return bias


def forecast_case(case):
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError("case has no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))
    day_start_epoch = to_epoch(case["day_start"])
    profile = target_profile(case, day_start_epoch, p1)

    curves = []
    for comp in case.get("comparables") or []:
        curve = comparable_curve(comp)
        if 0 not in curve or curve[0] <= 0:
            continue
        have = sum(1 for off in range(24) if off in curve)
        if have < MIN_DAY_HOURS:
            continue
        curves.append(curve)
    pool = weighted_pool(curves, profile)
    hours = [spec["hour"] for spec in case["forecast"]]
    forecast = ratio_forecast(p1, pool, hours)
    if curves:
        bias = self_calibration(curves, pool)
        for hour in hours:
            shift = math.exp(bias.get(hour, 0.0))
            forecast[hour] = [q * shift for q in forecast[hour]]

    rows = []
    for spec in case["forecast"]:
        hour = spec["hour"]
        quantiles = [min(1e9, max(0.5, q)) for q in forecast[hour]]
        for i in range(1, len(quantiles)):
            quantiles[i] = max(quantiles[i], quantiles[i - 1])
        rows.append({"hour": hour, "q": [round(q, 2) for q in quantiles]})
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        result = forecast_case(case)
        text = json.dumps(result, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / f"{path.stem}.out").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
