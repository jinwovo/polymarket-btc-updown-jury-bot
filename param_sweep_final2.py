"""Ultra-final refinement around DMIN."""
import os, re, subprocess, sys, tempfile
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, "env", "runtime.public.env")

def read_env_file():
    lines = []
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if "=" in stripped and not stripped.startswith("#"):
                lines.append((stripped.split("=", 1)[0], raw))
            else:
                lines.append((None, raw))
    return lines

def write_temp_env(lines, overrides):
    applied = set()
    fd, path = tempfile.mkstemp(suffix=".env", prefix="sweep_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, raw in lines:
            if key and key in overrides:
                f.write(f"{key}={overrides[key]}\n"); applied.add(key)
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
        proc = subprocess.run([sys.executable, "_sweep_runner.py", "--last-hours", "48"],
            capture_output=True, text=True, timeout=300, cwd=SCRIPT_DIR, env=env)
        out = proc.stdout + proc.stderr
        def ex(pat):
            m = re.search(pat, out); return m.group(1) if m else None
        return (int(ex(r"Total trades:\s+(\d+)") or 0),
                float(ex(r"Win rate:\s+([\d.]+)%") or 0)/100,
                float(ex(r"Total PnL:\s+\$([\+\-\d.]+)") or 0),
                float(ex(r"Profit factor:\s+([\d.]+)") or 0),
                float(ex(r"Trades/hour:\s+([\d.]+)") or 0),
                float(ex(r"Max drawdown:\s+\$([\d.]+)") or 0))
    finally:
        try: os.unlink(tmp_env)
        except: pass

env_lines = read_env_file()
BASE = {"JURY_THRESHOLD": "2", "PAPER_ENTRY_START_SEC": "90", "PAPER_DOWN_ENTRY_END_SEC": "200", "PAPER_DOWN_MIN_ENTRY_PRICE": "0.35"}

combos = [
    ("BEST: J2+S90+D200+DMIN=0.35", BASE),
    ("+BD=0.020", {**BASE, "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    ("+BD=0.015", {**BASE, "PAPER_MIN_BOUNDARY_DIST_PCT": "0.015"}),
    ("+BD=0.020+EDGE=0.08", {**BASE, "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020", "MIN_EDGE": "0.08"}),
    ("+ROI=0.03", {**BASE, "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03"}),
    ("+ROI=0.03+BD=0.020", {**BASE, "MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_EXPECTED_ROI": "0.03", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
    ("DMIN=0.32", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.32"}),
    ("DMIN=0.30", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.30"}),
    ("DMIN=0.36", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.36"}),
    ("DMIN=0.37", {**BASE, "PAPER_DOWN_MIN_ENTRY_PRICE": "0.37"}),
    ("+DEND=220", {**BASE, "PAPER_DOWN_ENTRY_END_SEC": "220"}),
    ("+DEND=220+BD=0.020", {**BASE, "PAPER_DOWN_ENTRY_END_SEC": "220", "PAPER_MIN_BOUNDARY_DIST_PCT": "0.020"}),
]

print(f"\n{'='*105}")
print(f"  FINAL REFINEMENT (base: JURY=2, START=90, DEND=200, DMIN=0.35)")
print(f"{'='*105}")
print(f"  {'Label':<35s} | {'Trades':>7s} | {'WR':>7s} | {'PnL':>11s} | {'PF':>7s} | {'Tr/hr':>6s} | {'MaxDD':>9s}")
print(f"  {'─'*35} | {'─'*7} | {'─'*7} | {'─'*11} | {'─'*7} | {'─'*6} | {'─'*9}")

best_pnl, best_label = -999999, ""
for i, (label, ov) in enumerate(combos):
    print(f"  Running {i+1}/{len(combos)}...          ", end="\r", flush=True)
    t, wr, pnl, pf, tph, mdd = run_bt(ov, env_lines)
    if pnl > best_pnl and t > 10:
        best_pnl, best_label = pnl, label
    print(f"  {label:<35s} | {t:7d} | {wr:6.1%} | ${pnl:+10.2f} | {pf:7.2f} | {tph:6.1f} | ${mdd:8.2f}")

print(f"{'='*105}")
print(f"  OVERALL BEST: {best_label}  (PnL=${best_pnl:+.2f})")
print(f"{'='*105}")
