"""PolyBot watchdog: keeps the whole trading stack alive on this Windows box.

One pass per invocation (or --loop to run forever with a 5-min interval).
Responsibilities, in order:
  1. MariaDB reachable (service is Automatic; best-effort Start-Service if down)
  2. dashboard API (dashboard_server.py :8790) alive -> restart if dead
  3. collector (run_collector.py -> data_collector.py) alive AND btc_ticks fresh
  4. Next.js dashboard web (:3100) alive
  5. Managed components (paper/live/signal per market) running via dashboard API

Config:      scripts/watchdog_config.json
Kill switch: create scripts/watchdog_off.flag to disable all actions
State:       scripts/watchdog_state.json (cooldowns)
Log:         logs/watchdog.log

NOTE: ASCII only in all output (Windows cp949 console).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
LOGS = REPO / "logs"
CONFIG_PATH = SCRIPTS / "watchdog_config.json"
STATE_PATH = SCRIPTS / "watchdog_state.json"
LOCK_PATH = SCRIPTS / "watchdog.lock"
OFF_FLAG = SCRIPTS / "watchdog_off.flag"
LOG_PATH = LOGS / "watchdog.log"

LOOP_INTERVAL_SEC = 300
LOCK_STALE_SEC = 900

# Python scripts that dashboard_server manages as children. If the API dies,
# these keep running orphaned with a broken stdout pipe and will eventually
# block on writes -- they must be killed before a fresh API starts.
MANAGED_CHILD_PATTERNS = (
    "main.py|paper_trade_sim.py|paper_sim_btc15|paper_sim_eth5|"
    "signal_generator_btc15|signal_generator_eth5|live_eth5.py|live_btc15.py"
)
COLLECTOR_PATTERNS = "run_collector.py|data_collector.py"


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " [watchdog] " + msg
    try:
        LOGS.mkdir(exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, obj) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
    except Exception as e:
        log("WARN cannot save %s: %s" % (path.name, e))


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def http_json(url: str, payload=None, timeout: float = 12.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def run_ps(script: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return (r.stdout or "").strip()
    except Exception as e:
        log("WARN powershell failed: %s" % e)
        return ""


def list_procs(name: str, cmd_pattern: str):
    """Return [{pid, cmd}] of processes with image name and cmdline regex."""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
        "Where-Object { $_.CommandLine -match '%s' } | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    ) % (name, cmd_pattern)
    out = run_ps(ps)
    if not out:
        return []
    try:
        obj = json.loads(out)
    except Exception:
        return []
    if isinstance(obj, dict):
        obj = [obj]
    return [
        {"pid": int(o.get("ProcessId", 0)), "cmd": str(o.get("CommandLine", ""))}
        for o in obj if o.get("ProcessId")
    ]


def kill_procs(name: str, cmd_pattern: str) -> int:
    procs = list_procs(name, cmd_pattern)
    for p in procs:
        run_ps("Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % p["pid"])
        log("killed %s pid=%d (%s...)" % (name, p["pid"], p["cmd"][:90]))
    return len(procs)


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn_detached(cmd, log_name: str) -> int:
    """Spawn a long-lived child that survives this watchdog process.

    CREATE_NO_WINDOW (not DETACHED_PROCESS): gives the child a hidden console
    that its own children inherit. DETACHED_PROCESS caused every grandchild
    (paper/live/signal spawned by dashboard_server) to open a visible terminal.
    """
    flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    LOGS.mkdir(exist_ok=True)
    logf = open(LOGS / log_name, "ab")
    try:
        p = subprocess.Popen(
            cmd, cwd=str(REPO), stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags, env=child_env(),
        )
    finally:
        logf.close()
    log("spawned %s pid=%d -> logs/%s" % (" ".join(str(c) for c in cmd)[:100], p.pid, log_name))
    return p.pid


def find_key(obj, key):
    """Depth-first search for a key anywhere in nested dict/list JSON."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def cooldown_ok(state: dict, key: str, seconds: int) -> bool:
    return (time.time() - float(state.get(key, 0))) >= seconds


def mark(state: dict, key: str) -> None:
    state[key] = time.time()


# ---------------------------------------------------------------- checks

def ensure_db(cfg, state) -> bool:
    if port_open(cfg.get("db_port", 3400)):
        return True
    log("ERROR MariaDB port %s closed - trying Start-Service MariaDB" % cfg.get("db_port"))
    run_ps("try { Start-Service MariaDB -ErrorAction Stop; 'started' } catch { $_.Exception.Message }")
    time.sleep(5)
    ok = port_open(cfg.get("db_port", 3400))
    log("MariaDB after start attempt: %s" % ("up" if ok else "STILL DOWN"))
    return ok


def ensure_api(cfg, state) -> bool:
    base = cfg["api_base"]
    try:
        http_json(base + "/healthz", timeout=6)
        return True
    except Exception:
        pass
    if not cooldown_ok(state, "api_restart_ts", cfg.get("api_restart_cooldown_sec", 300)):
        log("API down but within restart cooldown - skip")
        return False
    log("API down -> restarting dashboard_server (and killing orphaned managed children)")
    mark(state, "api_restart_ts")
    kill_procs("python.exe", "dashboard_server.py")
    kill_procs("python.exe", MANAGED_CHILD_PATTERNS)
    spawn_detached(
        [sys.executable, str(REPO / "dashboard_server.py"), "--host", "127.0.0.1", "--port", "8790"],
        "api_console.log",
    )
    for _ in range(20):
        time.sleep(2)
        try:
            http_json(base + "/healthz", timeout=4)
            log("API is back up")
            return True
        except Exception:
            continue
    log("ERROR API did not come up within 40s")
    return False


def get_tick_age(cfg):
    """btc tick age in seconds via /api/snapshot, or None if unknown."""
    try:
        snap = http_json(cfg["api_base"] + "/api/snapshot", timeout=12)
    except Exception as e:
        log("WARN snapshot fetch failed: %s" % e)
        return None
    age = find_key(snap, "last_tick_age_sec")
    try:
        return float(age)
    except (TypeError, ValueError):
        return None


def ensure_collector(cfg, state, tick_age) -> bool:
    """Keep the collector alive. Returns True if it was (re)started this pass."""
    procs = list_procs("python.exe", COLLECTOR_PATTERNS)
    if not procs:
        log("collector not running -> spawning run_collector.py")
        spawn_detached([sys.executable, str(REPO / "run_collector.py")], "collector_console.log")
        return True

    if tick_age is None:
        return False
    stale_after = cfg.get("tick_stale_restart_sec", 180)
    if tick_age <= stale_after:
        return False
    if not cooldown_ok(state, "collector_kill_ts", cfg.get("collector_kill_cooldown_sec", 600)):
        log("ticks stale (%.0fs) but within collector cooldown - skip" % tick_age)
        return False
    log("btc_ticks stale (%.0fs > %ds) -> killing data_collector.py (wrapper restarts it)"
        % (tick_age, stale_after))
    mark(state, "collector_kill_ts")
    killed = kill_procs("python.exe", "data_collector.py")
    if killed == 0 or not list_procs("python.exe", "run_collector.py"):
        log("run_collector wrapper missing -> spawning fresh")
        kill_procs("python.exe", COLLECTOR_PATTERNS)
        spawn_detached([sys.executable, str(REPO / "run_collector.py")], "collector_console.log")
    return True


def ensure_web(cfg, state) -> None:
    if port_open(cfg.get("web_port", 3100)):
        return
    if not cooldown_ok(state, "web_restart_ts", cfg.get("web_restart_cooldown_sec", 900)):
        return
    mark(state, "web_restart_ts")
    node = None
    for cand in ("node", "node.exe"):
        import shutil
        node = shutil.which(cand)
        if node:
            break
    if not node:
        log("WARN node not found on PATH - web dashboard skipped")
        return
    next_bin = REPO / "node_modules" / "next" / "dist" / "bin" / "next"
    if not (REPO / ".next").exists():
        log("no .next build -> spawning one-time npm build (web starts on a later pass)")
        import shutil
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if npm:
            spawn_detached(["cmd.exe", "/d", "/c", npm, "run", "build"], "web_build.log")
        return
    log("web :%s down -> starting next" % cfg.get("web_port", 3100))
    kill_procs("node.exe", "next")
    spawn_detached([node, str(next_bin), "start", "-p", str(cfg.get("web_port", 3100))],
                   "web_console.log")


# Live traders read signal_cache; on a cold boot the cached row is stale until
# the collector / signal generator warms up. Never start a live component in
# the same pass that (re)started its data dependency -- wait for the next pass.
LIVE_DEPS = {
    "live_btc5": ("collector",),
    "live_eth5": ("collector", "signal_eth5"),
    "live_btc15": ("collector", "signal_btc15"),
}
LIVE_MAX_TICK_AGE_SEC = 30.0


def ensure_components(cfg, state, tick_age, started_this_pass) -> None:
    base = cfg["api_base"]
    for comp in cfg.get("components", []):
        if not comp.get("enabled", False):
            continue
        name = comp.get("name", "?")
        if name in LIVE_DEPS:
            deps_fresh_started = [d for d in LIVE_DEPS[name] if d in started_this_pass]
            if deps_fresh_started:
                log("defer %s: deps just started (%s) - next pass" % (name, ",".join(deps_fresh_started)))
                continue
            if tick_age is None or tick_age > LIVE_MAX_TICK_AGE_SEC:
                log("defer %s: btc tick age %s not fresh enough" % (name, tick_age))
                continue
        try:
            status = http_json(base + comp["status"], timeout=15)
            if find_key(status, "running"):
                continue
        except Exception as e:
            log("WARN %s status check failed: %s" % (name, e))
            continue
        try:
            resp = http_json(base + comp["start"], payload=comp.get("payload", {}), timeout=45)
            ok = bool(resp.get("ok"))
            msg = str(resp.get("message") or resp.get("status") or resp.get("error") or "")[:200]
            if ok or "already running" in msg:
                log("started %s: %s" % (name, msg or "ok"))
                started_this_pass.add(name)
            else:
                log("ERROR failed to start %s: %s" % (name, msg))
        except Exception as e:
            log("ERROR start call for %s failed: %s" % (name, e))


# ---------------------------------------------------------------- main

def one_pass() -> None:
    cfg = load_json(CONFIG_PATH, None)
    if not cfg or not cfg.get("enabled", False):
        log("config missing or disabled - nothing to do")
        return
    if OFF_FLAG.exists():
        log("watchdog_off.flag present - skipping all actions")
        return
    state = load_json(STATE_PATH, {})
    started_this_pass = set()
    db_ok = ensure_db(cfg, state)
    api_ok = ensure_api(cfg, state) if db_ok else False
    tick_age = get_tick_age(cfg) if api_ok else None
    if ensure_collector(cfg, state, tick_age):
        started_this_pass.add("collector")
    ensure_web(cfg, state)
    if api_ok:
        ensure_components(cfg, state, tick_age, started_this_pass)
    state["last_run_ts"] = time.time()
    save_json(STATE_PATH, state)


def acquire_lock() -> bool:
    try:
        if LOCK_PATH.exists():
            if time.time() - LOCK_PATH.stat().st_mtime < LOCK_STALE_SEC:
                return False
            LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except Exception:
        return False


def release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    loop = "--loop" in sys.argv
    while True:
        if acquire_lock():
            try:
                one_pass()
            except Exception as e:
                log("ERROR pass crashed: %r" % e)
            finally:
                release_lock()
        else:
            log("another watchdog holds the lock - skipping this pass")
        if not loop:
            break
        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    main()
