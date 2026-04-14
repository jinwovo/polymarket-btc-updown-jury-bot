"""Auto-restart wrapper for data_collector.py"""
import subprocess
import sys
import time

MAX_RESTARTS = 100
RESTART_DELAY = 5  # seconds

for i in range(MAX_RESTARTS):
    print(f"[restart-wrapper] Starting data_collector.py (attempt {i+1}/{MAX_RESTARTS})")
    proc = subprocess.run(
        [sys.executable, "data_collector.py"],
        cwd=sys.path[0] or ".",
    )
    exit_code = proc.returncode
    print(f"[restart-wrapper] data_collector.py exited with code {exit_code}")
    
    if exit_code == 0:
        print("[restart-wrapper] Clean exit, not restarting")
        break
    
    print(f"[restart-wrapper] Crash detected, restarting in {RESTART_DELAY}s...")
    time.sleep(RESTART_DELAY)

print("[restart-wrapper] Max restarts reached or clean exit")
