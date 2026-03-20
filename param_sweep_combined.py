"""Second-pass sweep: test parameter interactions around the best values."""
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

# Create runner script
runner_path = os.path.join(SCRIPT_DIR, "_sweep_runner.py")
with open(runner_path, "w", encoding="utf-8") as f:
    f.write('''"""Sweep runner: loads override env file, then runs backtest."""
import os
import sys

sweep_env = os.environ.get("SWEEP_RUNTIME_ENV_PATH")
if sweep_env:
    import env_paths
    env_paths.PUBLIC_RUNTIME_ENV_PATH = sweep_env

from backtest import main
main()
''')

# The two key findings: JURY_THRESHOLD=2 and PAPER_ENTRY_START_SEC=90
# were the only params that changed results. Let's do interaction tests.
combos = [
    # Baseline
    ("BASELINE (current)", {}),
    # Single changes that mattered
    ("JURY=2", {"JURY_THRESHOLD": "2"}),
    ("START=90", {"PAPER_ENTRY_START_SEC": "90"}),
    # Interactions with the two big winners
    ("JURY=2 + START=90", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90"}),
    ("JURY=2 + START=90 + EDGE=0.06", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EDGE": "0.06"}),
    ("JURY=2 + START=90 + EDGE=0.08", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EDGE": "0.08"}),
    ("JURY=2 + START=90 + EDGE=0.12", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EDGE": "0.12"}),
    ("JURY=2 + START=90 + ROI=0.03", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03"}),
    ("JURY=2 + START=90 + ROI=0.07", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EXPECTED_ROI": "0.07", "PAPER_MIN_EXPECTED_ROI": "0.07"}),
    ("JURY=2 + START=90 + BDIST=0.020", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    ("JURY=2 + START=90 + BDIST=0.040", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.040"}),
    ("JURY=2 + START=90 + DEND=140", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_DOWN_ENTRY_END_SEC": "140"}),
    ("JURY=2 + START=90 + DEND=200", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_DOWN_ENTRY_END_SEC": "200"}),
    # Fine-tune entry start around 90
    ("JURY=2 + START=75", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "75"}),
    ("JURY=2 + START=105", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "105"}),
    ("JURY=2 + START=120", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "120"}),
    # Best combo from sweep 1 for reference
    ("FULL COMBINED (sweep1)", {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "MIN_EDGE": "0.06", "MIN_EXPECTED_ROI": "0.02", "PAPER_MIN_EXPECTED_ROI": "0.02", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.015", "PAPER_DOWN_ENTRY_END_SEC": "140"}),
]

print(f"\n{'='*100}")
print(f"  INTERACTION SWEEP: Testing parameter combinations")
print(f"{'='*100}")
print(f"  {'Label':<45s} | {'Trades':>7s} | {'WR':>7s} | {'PnL':>11s} | {'PF':>7s} | {'Tr/hr':>6s} | {'MaxDD':>9s}")
print(f"  {'─'*45} | {'─'*7} | {'─'*7} | {'─'*11} | {'─'*7} | {'─'*6} | {'─'*9}")

for i, (label, overrides) in enumerate(combos):
    print(f"  Running {i+1}/{len(combos)}: {label}...          ", end="\r", flush=True)
    trades, wr, pnl, pf, tph, mdd = run_bt(overrides, env_lines)
    print(f"  {label:<45s} | {trades:7d} | {wr:6.1%} | ${pnl:+10.2f} | {pf:7.2f} | {tph:6.1f} | ${mdd:8.2f}")

print(f"{'='*100}")
