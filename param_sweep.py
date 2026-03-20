"""
Parameter sweep for backtest.py — tests different env var combinations
and reports which settings produce the best results over the last 48 hours.

Strategy: writes a temporary override file and passes its path via
SWEEP_OVERRIDE_JSON env var. The wrapper (_sweep_runner.py) applies
overrides AFTER dotenv has loaded but BEFORE config is read.

Alternative approach: we write overrides directly into a temp copy of
runtime.public.env so load_dotenv picks them up with override=True.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, "env", "runtime.public.env")

# ── defaults (current production values from env file) ─────────────────────
DEFAULTS = {
    "MIN_EDGE": "0.10",
    "MIN_EXPECTED_ROI": "0.050",
    "PAPER_MIN_EXPECTED_ROI": "0.050",
    "PAPER_MIN_BOUNDARY_DIST_PCT": "0.030",
    "PAPER_DOWN_MIN_BOUNDARY_DIST_PCT": "0.040",
    "PAPER_MIN_RECENT_MOVE_PCT": "0.004",
    "PAPER_ENTRY_START_SEC": "45",
    "PAPER_ENTRY_END_SEC": "240",
    "PAPER_DOWN_ENTRY_END_SEC": "180",
    "PAPER_MAX_ENTRY_PRICE": "0.58",
    "PAPER_DOWN_MIN_ENTRY_PRICE": "0.38",
    "PAPER_MACRO_TREND_BLOCK_PCT": "0.10",
    "JURY_THRESHOLD": "3",
}

@dataclass
class Result:
    label: str
    params: dict
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0
    trades_per_hour: float = 0.0
    max_drawdown: float = 0.0
    error: str = ""


def parse_backtest_output(output: str) -> dict:
    """Parse the backtest report text to extract key metrics."""
    def ex(pat):
        m = re.search(pat, output)
        return m.group(1) if m else None

    trades = int(ex(r"Total trades:\s+(\d+)") or 0)
    tph = float(ex(r"Trades/hour:\s+([\d.]+)") or 0)
    wr = float(ex(r"Win rate:\s+([\d.]+)%") or 0) / 100.0
    pnl_s = ex(r"Total PnL:\s+\$([\+\-\d.]+)")
    pnl = float(pnl_s) if pnl_s else 0.0
    pf = float(ex(r"Profit factor:\s+([\d.]+)") or 0)
    mdd = float(ex(r"Max drawdown:\s+\$([\d.]+)") or 0)
    return dict(total_trades=trades, win_rate=wr, total_pnl=pnl,
                profit_factor=pf, trades_per_hour=tph, max_drawdown=mdd)


def read_env_file():
    """Read the env file into an ordered list of (key, value, raw_line) tuples."""
    lines = []
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0]
                val = stripped.split("=", 1)[1]
                lines.append((key, val, raw))
            else:
                lines.append((None, None, raw))
    return lines


def write_temp_env(lines, overrides):
    """Write a modified env file with overrides applied. Returns temp path."""
    applied = set()
    fd, path = tempfile.mkstemp(suffix=".env", prefix="sweep_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, val, raw in lines:
            if key and key in overrides:
                f.write(f"{key}={overrides[key]}\n")
                applied.add(key)
            else:
                f.write(raw)
        # Add any overrides not already in the file
        for k, v in overrides.items():
            if k not in applied:
                f.write(f"{k}={v}\n")
    return path


def run_backtest(overrides: dict, env_lines: list) -> dict:
    """Run backtest.py with a temp env file containing overrides."""
    tmp_env = write_temp_env(env_lines, overrides)
    try:
        env = os.environ.copy()
        env["SWEEP_RUNTIME_ENV_PATH"] = tmp_env
        proc = subprocess.run(
            [sys.executable, "_sweep_runner.py", "--last-hours", "48"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=SCRIPT_DIR,
            env=env,
        )
        output = proc.stdout + proc.stderr
        if "No trades" in output:
            return dict(total_trades=0, win_rate=0, total_pnl=0,
                       profit_factor=0, trades_per_hour=0, max_drawdown=0)
        if proc.returncode != 0:
            return dict(error=output[-500:])
        return parse_backtest_output(output)
    except subprocess.TimeoutExpired:
        return dict(error="TIMEOUT")
    except Exception as e:
        return dict(error=str(e))
    finally:
        try:
            os.unlink(tmp_env)
        except Exception:
            pass


def print_sweep_table(name: str, param_key: str, results: list[dict]):
    """Print a formatted table for one sweep dimension. Returns best result."""
    print(f"\n{'='*90}")
    print(f"  SWEEP: {name}")
    print(f"{'='*90}")
    print(f"  {'Value':>12s} | {'Trades':>7s} | {'WR':>7s} | {'PnL':>11s} | {'PF':>7s} | {'Tr/hr':>6s} | {'MaxDD':>9s}")
    print(f"  {'─'*12} | {'─'*7} | {'─'*7} | {'─'*11} | {'─'*7} | {'─'*6} | {'─'*9}")

    best_pnl = -999999
    best_idx = 0
    for i, (val, r) in enumerate(results):
        if "error" in r:
            print(f"  {str(val):>12s} | ERROR: {r['error'][:60]}")
            continue
        if r["total_pnl"] > best_pnl:
            best_pnl = r["total_pnl"]
            best_idx = i
        print(
            f"  {str(val):>12s} | {r['total_trades']:7d} | {r['win_rate']:6.1%} | "
            f"${r['total_pnl']:+10.2f} | {r['profit_factor']:7.2f} | "
            f"{r['trades_per_hour']:6.1f} | ${r['max_drawdown']:8.2f}"
        )

    best_val, best_r = results[best_idx]
    print(f"  >>> BEST: {param_key}={best_val}  (PnL=${best_r['total_pnl']:+.2f}, PF={best_r['profit_factor']:.2f})")
    return best_val, best_r


def main():
    start = time.time()

    # Create the runner script that loads our temp env file
    runner_path = os.path.join(SCRIPT_DIR, "_sweep_runner.py")
    with open(runner_path, "w", encoding="utf-8") as f:
        f.write('''"""Sweep runner: loads override env file, then runs backtest."""
import os
import sys

# Load the sweep override env BEFORE anything else
sweep_env = os.environ.get("SWEEP_RUNTIME_ENV_PATH")
if sweep_env:
    # Monkey-patch env_paths so backtest.py loads our temp file
    import env_paths
    env_paths.PUBLIC_RUNTIME_ENV_PATH = sweep_env

# Now run the actual backtest main
# We need to re-import after patching
from backtest import main
main()
''')

    env_lines = read_env_file()

    # ── Sweeps ─────────────────────────────────────────────────────────────
    sweeps = [
        ("MIN_EDGE", "MIN_EDGE", [0.06, 0.08, 0.10, 0.12, 0.15]),
        ("MIN_EXPECTED_ROI (PAPER_MIN_EXPECTED_ROI)", "MIN_EXPECTED_ROI",
         [0.02, 0.03, 0.05, 0.07, 0.10]),
        ("PAPER_MIN_BOUNDARY_DIST_PCT", "PAPER_MIN_BOUNDARY_DIST_PCT",
         [0.015, 0.020, 0.025, 0.030, 0.040]),
        ("JURY_THRESHOLD", "JURY_THRESHOLD", [2, 3]),
        ("PAPER_ENTRY_START_SEC", "PAPER_ENTRY_START_SEC", [30, 45, 60, 90]),
        ("PAPER_DOWN_ENTRY_END_SEC", "PAPER_DOWN_ENTRY_END_SEC", [140, 160, 180, 200]),
    ]

    total_runs = sum(len(vals) for _, _, vals in sweeps) + 2  # +baseline +combined
    run_count = 0

    # ── Baseline ───────────────────────────────────────────────────────────
    run_count += 1
    print(f"\n{'='*90}")
    print(f"  BASELINE (current production parameters)")
    print(f"{'='*90}")
    print(f"  Running baseline ({run_count}/{total_runs})...", end="", flush=True)
    baseline = run_backtest({}, env_lines)
    if "error" in baseline:
        print(f"\n  ERROR: {baseline['error']}")
    else:
        print(
            f"\r  Trades={baseline['total_trades']}  WR={baseline['win_rate']:.1%}  "
            f"PnL=${baseline['total_pnl']:+.2f}  PF={baseline['profit_factor']:.2f}  "
            f"Tr/hr={baseline['trades_per_hour']:.1f}  MaxDD=${baseline['max_drawdown']:.2f}"
        )

    # ── Individual sweeps ──────────────────────────────────────────────────
    best_values = {}

    for sweep_name, param_key, values in sweeps:
        results = []
        for val in values:
            run_count += 1
            print(f"  Running {run_count}/{total_runs}: {param_key}={val}...     ", end="\r", flush=True)
            # For MIN_EXPECTED_ROI, also set PAPER_MIN_EXPECTED_ROI and vice versa
            overrides = {param_key: str(val)}
            if param_key == "MIN_EXPECTED_ROI":
                overrides["PAPER_MIN_EXPECTED_ROI"] = str(val)
            r = run_backtest(overrides, env_lines)
            results.append((val, r))

        print(" " * 80, end="\r")  # clear progress line
        best_val, best_r = print_sweep_table(sweep_name, param_key, results)
        best_values[param_key] = best_val

    # ── Combined best ──────────────────────────────────────────────────────
    run_count += 1
    print(f"\n{'='*90}")
    print(f"  COMBINED BEST FROM EACH SWEEP")
    print(f"{'='*90}")
    combined_overrides = {}
    for k, v in best_values.items():
        combined_overrides[k] = str(v)
        print(f"  {k} = {v}")
    # Sync ROI keys
    if "MIN_EXPECTED_ROI" in combined_overrides:
        combined_overrides["PAPER_MIN_EXPECTED_ROI"] = combined_overrides["MIN_EXPECTED_ROI"]

    print(f"\n  Running combined ({run_count}/{total_runs})...", end="", flush=True)
    combined = run_backtest(combined_overrides, env_lines)
    if "error" in combined:
        print(f"\n  ERROR: {combined['error']}")
    else:
        print(
            f"\r  Trades={combined['total_trades']}  WR={combined['win_rate']:.1%}  "
            f"PnL=${combined['total_pnl']:+.2f}  PF={combined['profit_factor']:.2f}  "
            f"Tr/hr={combined['trades_per_hour']:.1f}  MaxDD=${combined['max_drawdown']:.2f}"
        )

    # ── Comparison ─────────────────────────────────────────────────────────
    if "error" not in baseline and "error" not in combined:
        print(f"\n{'='*90}")
        print(f"  COMPARISON: BASELINE vs COMBINED BEST")
        print(f"{'='*90}")
        print(f"  {'Metric':<20s} | {'Baseline':>12s} | {'Combined':>12s} | {'Delta':>12s}")
        print(f"  {'─'*20} | {'─'*12} | {'─'*12} | {'─'*12}")

        rows = [
            ("Total trades", "total_trades", "d"),
            ("Win rate", "win_rate", "%"),
            ("Total PnL", "total_pnl", "$"),
            ("Profit factor", "profit_factor", "f"),
            ("Trades/hour", "trades_per_hour", "f1"),
            ("Max drawdown", "max_drawdown", "$"),
        ]
        for label, key, fmt in rows:
            bv = baseline[key]
            cv = combined[key]
            if fmt == "d":
                print(f"  {label:<20s} | {bv:>12d} | {cv:>12d} | {cv - bv:>+12d}")
            elif fmt == "%":
                print(f"  {label:<20s} | {bv:>11.1%} | {cv:>11.1%} | {(cv-bv)*100:>+11.1f}pp")
            elif fmt == "$":
                print(f"  {label:<20s} | ${bv:>+11.2f} | ${cv:>+11.2f} | ${cv-bv:>+11.2f}")
            elif fmt == "f":
                print(f"  {label:<20s} | {bv:>12.2f} | {cv:>12.2f} | {cv-bv:>+12.2f}")
            else:
                print(f"  {label:<20s} | {bv:>12.1f} | {cv:>12.1f} | {cv-bv:>+12.1f}")

    elapsed = time.time() - start
    print(f"\n  Total sweep time: {elapsed:.0f}s ({total_runs} backtest runs)")
    print(f"{'='*90}")

    # Cleanup runner
    try:
        os.unlink(runner_path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
