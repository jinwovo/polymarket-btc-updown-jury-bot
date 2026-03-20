"""Final fine-tuning sweep around the best combo: JURY=2, START=90, DEND=200."""
import os
import re
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, "env", "runtime.public.env")

def read_env_file():
    lines = []
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0]
                lines.append((key, raw))
            else:
                lines.append((None, raw))
    return lines

def write_temp_env(lines, overrides):
    applied = set()
    fd, path = tempfile.mkstemp(suffix=".env", prefix="sweep_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, raw in lines:
            if key and key in overrides:
                f.write(f"{key}={overrides[key]}\n")
                applied.add(key)
            else:
                f.write(raw)
        for k, v in overrides.items():
            if k not in applied:
                f.write(f"{k}={v}\n")
    return path

def run_bt(overrides, env_lines):
    tmp_env = write_temp_env(env_lines, overrides)
    try:
        env = os.environ.copy()
        env["SWEEP_RUNTIME_ENV_PATH"] = tmp_env
        proc = subprocess.run(
            [sys.executable, "_sweep_runner.py", "--last-hours", "48"],
            capture_output=True, text=True, timeout=300, cwd=SCRIPT_DIR, env=env,
        )
        out = proc.stdout + proc.stderr
        def ex(pat):
            m = re.search(pat, out)
            return m.group(1) if m else None
        trades = int(ex(r"Total trades:\s+(\d+)") or 0)
        wr = float(ex(r"Win rate:\s+([\d.]+)%") or 0) / 100
        pnl_s = ex(r"Total PnL:\s+\$([\+\-\d.]+)")
        pnl = float(pnl_s) if pnl_s else 0
        pf = float(ex(r"Profit factor:\s+([\d.]+)") or 0)
        tph = float(ex(r"Trades/hour:\s+([\d.]+)") or 0)
        mdd = float(ex(r"Max drawdown:\s+\$([\d.]+)") or 0)
        return trades, wr, pnl, pf, tph, mdd
    finally:
        try:
            os.unlink(tmp_env)
        except Exception:
            pass

env_lines = read_env_file()

# Base combo: JURY=2, START=90, DEND=200
BASE = {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_DOWN_ENTRY_END_SEC": "200"}

combos = [
    # Reference points
    ("BASELINE (current prod)", {}),
    ("BEST so far: J2+S90+D200", BASE),
    # Fine-tune DEND around 200
    ("J2+S90+D190", {**BASE, "PAPER_DOWN_ENTRY_END_SEC": "190"}),
    ("J2+S90+D210", {**BASE, "PAPER_DOWN_ENTRY_END_SEC": "210"}),
    ("J2+S90+D220", {**BASE, "PAPER_DOWN_ENTRY_END_SEC": "220"}),
    # Add ROI=0.03 (was good in interaction sweep)
    ("J2+S90+D200+ROI=0.03", {**BASE, "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03"}),
    # Add BDIST=0.020 (was good in interaction sweep)
    ("J2+S90+D200+BD=0.020", {**BASE, "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    # Both ROI + BDIST
    ("J2+S90+D200+ROI=0.03+BD=0.020", {**BASE, "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    # EDGE variations on best
    ("J2+S90+D200+EDGE=0.06", {**BASE, "MIN_EDGE": "0.06"}),
    ("J2+S90+D200+EDGE=0.08", {**BASE, "MIN_EDGE": "0.08"}),
    # Kitchen sink: all best values
    ("KITCHEN SINK", {**BASE, "MIN_EDGE": "0.08", "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    # DOWN min price variations
    ("J2+S90+D200+DMIN=0.35", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.35"}),
    ("J2+S90+D200+DMIN=0.42", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.42"}),
]

print(f"\n{'='*105}")
print(f"  FINAL FINE-TUNING SWEEP")
print(f"{'='*105}")
print(f"  {'Label':<40s} | {'Trades':>7s} | {'WR':>7s} | {'PnL':>11s} | {'PF':>7s} | {'Tr/hr':>6s} | {'MaxDD':>9s}")
print(f"  {'─'*40} | {'─'*7} | {'─'*7} | {'─'*11} | {'─'*7} | {'─'*6} | {'─'*9}")

best_pnl = -999999
best_label = ""
for i, (label, overrides) in enumerate(combos):
    print(f"  Running {i+1}/{len(combos)}: {label}...          ", end="\r", flush=True)
    trades, wr, pnl, pf, tph, mdd = run_bt(overrides, env_lines)
    marker = ""
    if pnl > best_pnl and trades > 10:
        best_pnl = pnl
        best_label = label
    print(f"  {label:<40s} | {trades:7d} | {wr:6.1%} | ${pnl:+10.2f} | {pf:7.2f} | {tph:6.1f} | ${mdd:8.2f}")

print(f"{'='*105}")
print(f"\n  OVERALL BEST: {best_label}  (PnL=${best_pnl:+.2f})")
print(f"{'='*105}")
