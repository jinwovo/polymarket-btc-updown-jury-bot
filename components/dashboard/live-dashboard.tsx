"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, Gauge, Timer } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Sparkline } from "@/components/dashboard/sparkline";

type JudgeVote = "UP" | "DOWN" | "ABSTAIN";

interface SnapshotResponse {
  ok: boolean;
  error?: string;
  server_time: number;
  server_time_utc: string;
  collector?: {
    running: boolean;
    last_tick_age_sec: number | null;
    last_odds_age_sec: number | null;
  };
  window?: {
    slug: string | null;
    window_start: number | null;
    window_end: number | null;
    seconds_elapsed: number;
    seconds_remaining: number;
    progress_pct: number;
  };
  market?: {
    btc_price: number | null;
    btc_start_price: number | null;
    btc_change_pct: number | null;
    up_mid: number | null;
    down_mid: number | null;
    up_bid: number | null;
    up_ask: number | null;
    down_bid: number | null;
    down_ask: number | null;
  };
  signal?: {
    direction: "UP" | "DOWN" | "NO_TRADE";
    actionable: boolean;
    action_label: string;
    avg_confidence: number;
    threshold: number;
    jury_threshold?: number;
    jury_size?: number;
    unanimous: boolean;
    reason: string;
    judges: Array<{
      name: string;
      vote: JudgeVote;
      confidence: number;
      reason: string;
    }>;
  };
  stats?: {
    ticks: number;
    odds: number;
    windows: number;
    resolved_windows: number;
  };
  recent_windows?: Array<{
    window_start: number | null;
    window_end: number | null;
    slug: string;
    btc_start_price: number | null;
    btc_end_price: number | null;
    actual_outcome: "UP" | "DOWN" | null;
    change_pct: number | null;
  }>;
}

interface HistoryResponse {
  ok: boolean;
  error?: string;
  minutes: number;
  btc: Array<{ ts: number; value: number }>;
  up: Array<{ ts: number; value: number }>;
  down: Array<{ ts: number; value: number }>;
}

interface ProcessStatus {
  ok: boolean;
  name?: string;
  running?: boolean;
  pid?: number | null;
  command?: string[];
  started_at?: number | null;
  ended_at?: number | null;
  exit_code?: number | null;
  meta?: Record<string, unknown>;
  output_tail?: string[];
  message?: string;
  error?: string;
}

interface FailedSignalHistoryItem {
  ts: number;
  ts_utc: string;
  window_start: number | null;
  window_end: number | null;
  slug: string | null;
  direction: "UP" | "DOWN" | "NO_TRADE";
  avg_confidence: number;
  threshold: number;
  reason: string;
  market?: {
    btc_change_pct: number | null;
    up_mid: number | null;
    down_mid: number | null;
  };
  judges: Array<{
    name: string;
    vote: JudgeVote;
    confidence: number;
    reason: string;
  }>;
}

interface FailedSignalHistoryResponse {
  ok: boolean;
  items: FailedSignalHistoryItem[];
  count: number;
  error?: string;
}

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPct(value: number | null | undefined, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function formatCountdown(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--:--";
  const s = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function ageText(sec: number | null | undefined) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return "n/a";
  if (sec < 1) return "just now";
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  return `${Math.floor(sec / 60)}m ago`;
}

export function LiveDashboard() {
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nowSec, setNowSec] = useState<number>(() => Date.now() / 1000);
  const snapshotInFlightRef = useRef(false);
  const historyInFlightRef = useRef(false);

  const [paperStatus, setPaperStatus] = useState<ProcessStatus | null>(null);
  const [backtestStatus, setBacktestStatus] = useState<ProcessStatus | null>(null);

  const [paperStake, setPaperStake] = useState("1000");
  const [paperInterval, setPaperInterval] = useState("2");

  const [lastHours, setLastHours] = useState("24");
  const [runMode, setRunMode] = useState<"single" | "auto_sweep">("auto_sweep");
  const [minEdgeInput, setMinEdgeInput] = useState("0.08");
  const [juryThresholdInput, setJuryThresholdInput] = useState("3");
  const [edgeGridInput, setEdgeGridInput] = useState("0.04,0.06,0.08,0.10,0.12,0.15");
  const [juryGridInput, setJuryGridInput] = useState("2,3,4,5");
  const [minTradesInput, setMinTradesInput] = useState("10");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [failedHistory, setFailedHistory] = useState<FailedSignalHistoryItem[]>([]);

  async function loadSnapshot() {
    const res = await fetch("/api/live/snapshot", { cache: "no-store" });
    const json = (await res.json()) as SnapshotResponse;
    if (!json.ok) throw new Error(json.error || "Snapshot unavailable");
    return json;
  }

  async function loadHistory() {
    const res = await fetch("/api/live/history?minutes=30", { cache: "no-store" });
    const json = (await res.json()) as HistoryResponse;
    if (!json.ok) throw new Error(json.error || "History unavailable");
    return json;
  }

  async function loadPaperStatus() {
    const res = await fetch("/api/control/paper", { cache: "no-store" });
    return (await res.json()) as ProcessStatus;
  }

  async function loadBacktestStatus() {
    const res = await fetch("/api/control/backtest", { cache: "no-store" });
    return (await res.json()) as ProcessStatus;
  }

  async function loadFailedSignalHistory(limit = 40) {
    setHistoryLoading(true);
    try {
      const res = await fetch(`/api/live/signal-history?limit=${limit}`, { cache: "no-store" });
      const json = (await res.json()) as FailedSignalHistoryResponse;
      if (json.ok) {
        setFailedHistory(json.items ?? []);
      }
    } finally {
      setHistoryLoading(false);
    }
  }

  async function startPaper() {
    const res = await fetch("/api/control/paper", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "start",
        stake: Number(paperStake || "1000"),
        interval: Number(paperInterval || "2"),
      }),
    });
    const json = (await res.json()) as ProcessStatus;
    setPaperStatus(json);
    const bt = await loadBacktestStatus();
    setBacktestStatus(bt);
  }

  async function stopPaper() {
    const res = await fetch("/api/control/paper", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "stop" }),
    });
    const json = (await res.json()) as ProcessStatus;
    setPaperStatus(json);
    const bt = await loadBacktestStatus();
    setBacktestStatus(bt);
  }

  async function runBacktest() {
    const payload: Record<string, unknown> = {
      action: "run",
      mode: runMode,
      last_hours: Number(lastHours || "24"),
    };

    if (runMode === "single") {
      payload.min_edge = Number(minEdgeInput || "0.08");
      payload.jury_threshold = Number(juryThresholdInput || "3");
    } else {
      payload.edge_grid = edgeGridInput;
      payload.jury_grid = juryGridInput;
      payload.min_trades = Number(minTradesInput || "10");
      payload.top = 10;
      payload.json_out = "sweep_best.json";
    }

    const res = await fetch("/api/control/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const json = (await res.json()) as ProcessStatus;
    setBacktestStatus(json);
    const paper = await loadPaperStatus();
    setPaperStatus(paper);
  }

  async function stopBacktest() {
    const res = await fetch("/api/control/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "stop" }),
    });
    const json = (await res.json()) as ProcessStatus;
    setBacktestStatus(json);
    const paper = await loadPaperStatus();
    setPaperStatus(paper);
  }

  useEffect(() => {
    let mounted = true;
    let snapshotTimer: number | null = null;
    let historyTimer: number | null = null;

    const pollSnapshot = async () => {
      if (!mounted) return;
      if (!snapshotInFlightRef.current) {
        snapshotInFlightRef.current = true;
        try {
          const data = await loadSnapshot();
          if (mounted) {
            setSnapshot(data);
            setError(null);
          }
        } catch (e) {
          if (mounted) setError(e instanceof Error ? e.message : "Snapshot fetch error");
        } finally {
          snapshotInFlightRef.current = false;
        }
      }
      if (mounted) {
        snapshotTimer = window.setTimeout(() => void pollSnapshot(), 2000);
      }
    };

    const pollHistory = async () => {
      if (!mounted) return;
      if (!historyInFlightRef.current) {
        historyInFlightRef.current = true;
        try {
          const data = await loadHistory();
          if (mounted) setHistory(data);
        } catch (_) {
          // Keep last chart data on transient errors.
        } finally {
          historyInFlightRef.current = false;
        }
      }
      if (mounted) {
        historyTimer = window.setTimeout(() => void pollHistory(), 6000);
      }
    };

    const loadControlOnce = async () => {
      if (!mounted) return;
      try {
        const [paper, backtest] = await Promise.all([loadPaperStatus(), loadBacktestStatus()]);
        if (mounted) {
          setPaperStatus(paper);
          setBacktestStatus(backtest);
        }
      } catch (_) {
        // Ignore transient control status errors.
      }
    };

    void pollSnapshot();
    void pollHistory();
    void loadControlOnce();

    return () => {
      mounted = false;
      if (snapshotTimer !== null) window.clearTimeout(snapshotTimer);
      if (historyTimer !== null) window.clearTimeout(historyTimer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const signal = snapshot?.signal;
  const market = snapshot?.market;
  const windowInfo = snapshot?.window;
  const collector = snapshot?.collector;

  const computedRemaining = useMemo(() => {
    if (windowInfo?.window_end) {
      return Math.max(0, windowInfo.window_end - nowSec);
    }
    return windowInfo?.seconds_remaining ?? 0;
  }, [windowInfo?.window_end, windowInfo?.seconds_remaining, nowSec]);

  const computedProgress = useMemo(() => {
    if (windowInfo?.window_start && windowInfo?.window_end) {
      const total = Math.max(1, windowInfo.window_end - windowInfo.window_start);
      const elapsed = Math.min(total, Math.max(0, nowSec - windowInfo.window_start));
      return (elapsed / total) * 100;
    }
    return windowInfo?.progress_pct ?? 0;
  }, [windowInfo?.window_start, windowInfo?.window_end, windowInfo?.progress_pct, nowSec]);

  const bannerTone = useMemo(() => {
    if (!signal) return "neutral";
    if (signal.actionable && signal.direction === "UP") return "up";
    if (signal.actionable && signal.direction === "DOWN") return "down";
    return "neutral";
  }, [signal]);

  const bannerClasses =
    bannerTone === "up"
      ? "border-emerald-400/40 bg-gradient-to-r from-emerald-500/20 to-teal-500/10 animate-soft-pulse"
      : bannerTone === "down"
        ? "border-rose-400/40 bg-gradient-to-r from-rose-500/20 to-orange-500/10"
        : "border-slate-600/50 bg-gradient-to-r from-slate-700/20 to-slate-800/20";

  const btcSeries = history?.btc?.map((p) => p.value) ?? [];
  const upSeries = history?.up?.map((p) => p.value) ?? [];
  const downSeries = history?.down?.map((p) => p.value) ?? [];

  return (
    <main className="pb-8">
      <div className="container space-y-5 pt-6">
        <header className="flex flex-col gap-4 rounded-2xl border border-border/70 bg-background/45 p-4 backdrop-blur-xl md:flex-row md:items-center md:justify-between">
          <div>
            <p className="tiny-label">Next.js + shadcn/ui</p>
            <h1 className="text-2xl font-bold md:text-3xl">Future Pulse Trading Station</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              실시간 BTC/Polymarket 시그널 모니터링
            </p>
          </div>
          <div className="flex items-center gap-3">
            {collector?.running ? (
              <Badge variant="success">Collector Live</Badge>
            ) : (
              <Badge variant="danger">Collector Delayed</Badge>
            )}
            <div className="rounded-lg border border-border/70 bg-secondary/40 px-3 py-2 text-right">
              <p className="font-mono text-xs text-muted-foreground">
                tick {ageText(collector?.last_tick_age_sec)} | odds {ageText(collector?.last_odds_age_sec)}
              </p>
              <p className="font-mono text-xs text-muted-foreground">{snapshot?.server_time_utc ?? "no time"}</p>
            </div>
          </div>
        </header>

        <section className={`panel border p-4 md:p-5 ${bannerClasses}`}>
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="tiny-label">Signal Engine</p>
              <p className="mt-1 text-2xl font-bold md:text-3xl">
                {signal?.actionable
                  ? `${signal.action_label} opportunity detected`
                  : "No actionable setup right now"}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">{signal?.reason ?? "Waiting for data..."}</p>
            </div>
            <div className="rounded-xl border border-border/60 bg-background/40 px-4 py-3 text-right">
              <p className="tiny-label">Confidence</p>
              <p className="font-mono text-3xl font-semibold">
                {formatNumber(signal?.avg_confidence ?? null, 3)}
              </p>
            </div>
          </div>
        </section>

        {error ? (
          <Card className="border-rose-500/40 bg-rose-500/10">
            <CardContent className="flex items-center gap-2 p-4">
              <AlertTriangle className="h-5 w-5 text-rose-300" />
              <span className="text-sm text-rose-100">{error}</span>
            </CardContent>
          </Card>
        ) : null}

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Card>
            <CardHeader>
              <CardDescription className="tiny-label">Current Action</CardDescription>
              <CardTitle className="text-3xl">{signal?.action_label ?? "WAIT"}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">{signal?.reason ?? "No signal yet"}</CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardDescription className="tiny-label">BTC Window Move</CardDescription>
              <CardTitle className="font-mono text-3xl">{formatPct(market?.btc_change_pct)}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              BTC {formatNumber(market?.btc_price)} | Start {formatNumber(market?.btc_start_price)}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardDescription className="tiny-label">Mid Odds</CardDescription>
              <CardTitle className="font-mono text-2xl">
                UP {formatNumber(market?.up_mid, 3)} / DOWN {formatNumber(market?.down_mid, 3)}
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              UP bid/ask {formatNumber(market?.up_bid, 3)} / {formatNumber(market?.up_ask, 3)}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardDescription className="tiny-label">Window Countdown</CardDescription>
              <CardTitle className="font-mono text-3xl">{formatCountdown(computedRemaining)}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="truncate text-sm text-muted-foreground">{windowInfo?.slug ?? "no active window"}</p>
              <Progress value={computedProgress} />
            </CardContent>
          </Card>
        </section>

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Card className="xl:col-span-2">
            <CardHeader>
              <CardTitle>Market Motion</CardTitle>
              <CardDescription>최근 30분 BTC / UP-DOWN odds 추이</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <p className="tiny-label mb-2 flex items-center gap-2">
                  <Activity className="h-3.5 w-3.5 text-cyan-300" />
                  BTC Price
                </p>
                <Sparkline values={btcSeries} stroke="#22d3ee" fillGradient="#22d3ee" />
              </div>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div>
                  <p className="tiny-label mb-2 flex items-center gap-2">
                    <ArrowUpRight className="h-3.5 w-3.5 text-emerald-300" />
                    UP Mid
                  </p>
                  <Sparkline values={upSeries} stroke="#34d399" fillGradient="#34d399" />
                </div>
                <div>
                  <p className="tiny-label mb-2 flex items-center gap-2">
                    <ArrowDownRight className="h-3.5 w-3.5 text-rose-300" />
                    DOWN Mid
                  </p>
                  <Sparkline values={downSeries} stroke="#fb7185" fillGradient="#fb7185" />
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <CardTitle>Judge Votes</CardTitle>
                <button
                  onClick={() => {
                    setHistoryOpen(true);
                    void loadFailedSignalHistory(40);
                  }}
                  className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                >
                  History
                </button>
              </div>
              <CardDescription>
                Live {signal?.jury_size ?? signal?.judges?.length ?? 0}-judge consensus
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2.5">
              {(signal?.judges ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">Waiting for enough lookback data...</p>
              ) : (
                signal?.judges?.map((j) => (
                  <div key={j.name} className="rounded-xl border border-border/70 bg-background/40 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium">{j.name}</p>
                      <Badge
                        variant={
                          j.vote === "UP" ? "success" : j.vote === "DOWN" ? "danger" : "neutral"
                        }
                      >
                        {j.vote}
                      </Badge>
                    </div>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      confidence {formatNumber(j.confidence, 3)}
                    </p>
                    <p className="mt-1 max-h-10 overflow-hidden text-xs text-muted-foreground">{j.reason}</p>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </section>

        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle>Recent 5m Windows</CardTitle>
              <CardDescription>최근 결과와 변동률</CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <Gauge className="h-4 w-4 text-cyan-300" />
              <span className="font-mono text-xs text-muted-foreground">
                ticks {snapshot?.stats?.ticks ?? 0} | odds {snapshot?.stats?.odds ?? 0} | windows{" "}
                {snapshot?.stats?.windows ?? 0}
              </span>
            </div>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full min-w-[740px] text-left">
              <thead>
                <tr className="border-b border-border/80 text-xs uppercase tracking-[0.15em] text-muted-foreground">
                  <th className="py-2 pr-2">Start(UTC)</th>
                  <th className="py-2 pr-2">Outcome</th>
                  <th className="py-2 pr-2">Start</th>
                  <th className="py-2 pr-2">End</th>
                  <th className="py-2 pr-2">Move</th>
                  <th className="py-2 pr-2">Slug</th>
                </tr>
              </thead>
              <tbody className="font-mono text-sm">
                {(snapshot?.recent_windows ?? []).length === 0 ? (
                  <tr>
                    <td className="py-4 text-muted-foreground" colSpan={6}>
                      아직 수집된 윈도우가 없습니다.
                    </td>
                  </tr>
                ) : (
                  snapshot?.recent_windows?.map((w) => (
                    <tr key={`${w.window_start}-${w.slug}`} className="border-b border-border/40">
                      <td className="py-2 pr-2">
                        {w.window_start ? new Date(w.window_start * 1000).toISOString().replace("T", " ").slice(0, 19) : "--"}
                      </td>
                      <td className="py-2 pr-2">
                        {w.actual_outcome === "UP" ? (
                          <Badge variant="success">UP</Badge>
                        ) : w.actual_outcome === "DOWN" ? (
                          <Badge variant="danger">DOWN</Badge>
                        ) : (
                          <Badge variant="neutral">PENDING</Badge>
                        )}
                      </td>
                      <td className="py-2 pr-2">{formatNumber(w.btc_start_price)}</td>
                      <td className="py-2 pr-2">{formatNumber(w.btc_end_price)}</td>
                      <td className="py-2 pr-2">{formatPct(w.change_pct, 4)}</td>
                      <td className="max-w-[280px] truncate py-2 pr-2 text-muted-foreground">{w.slug}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </CardContent>
        </Card>

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Paper Sim Control</CardTitle>
              <CardDescription>Start/stop virtual entry engine from this UI</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs text-muted-foreground">
                  Stake (USD)
                  <input
                    value={paperStake}
                    onChange={(e) => setPaperStake(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  Interval (sec)
                  <input
                    value={paperInterval}
                    onChange={(e) => setPaperInterval(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => void startPaper()}
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm"
                >
                  Start
                </button>
                <button
                  onClick={() => void stopPaper()}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={paperStatus?.running ? "success" : "neutral"}>
                  {paperStatus?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={paperStatus?.pid ?? "-"} | exit={paperStatus?.exit_code ?? "-"}
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(paperStatus?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Backtest/Sweep Control</CardTitle>
              <CardDescription>Run single backtest or auto sweep from this UI</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs text-muted-foreground">
                  Mode
                  <select
                    value={runMode}
                    onChange={(e) => setRunMode(e.target.value as "single" | "auto_sweep")}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="auto_sweep">auto_sweep</option>
                    <option value="single">single</option>
                  </select>
                </label>
                <label className="text-xs text-muted-foreground">
                  Last Hours
                  <input
                    value={lastHours}
                    onChange={(e) => setLastHours(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
              </div>

              {runMode === "single" ? (
                <div className="grid grid-cols-2 gap-3">
                  <label className="text-xs text-muted-foreground">
                    MIN_EDGE
                    <input
                      value={minEdgeInput}
                      onChange={(e) => setMinEdgeInput(e.target.value)}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                  <label className="text-xs text-muted-foreground">
                    JURY_THRESHOLD
                    <input
                      value={juryThresholdInput}
                      onChange={(e) => setJuryThresholdInput(e.target.value)}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-3">
                  <label className="text-xs text-muted-foreground">
                    Edge Grid (comma)
                    <input
                      value={edgeGridInput}
                      onChange={(e) => setEdgeGridInput(e.target.value)}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                  <label className="text-xs text-muted-foreground">
                    Jury Grid (comma)
                    <input
                      value={juryGridInput}
                      onChange={(e) => setJuryGridInput(e.target.value)}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                  <label className="text-xs text-muted-foreground">
                    Min Trades
                    <input
                      value={minTradesInput}
                      onChange={(e) => setMinTradesInput(e.target.value)}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                </div>
              )}

              <div className="flex gap-2">
                <button
                  onClick={() => void runBacktest()}
                  className="rounded-md border border-cyan-400/50 bg-cyan-500/20 px-3 py-1.5 text-sm"
                >
                  Run
                </button>
                <button
                  onClick={() => void stopBacktest()}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={backtestStatus?.running ? "success" : "neutral"}>
                  {backtestStatus?.running ? "RUNNING" : "IDLE"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={backtestStatus?.pid ?? "-"} | exit={backtestStatus?.exit_code ?? "-"}
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(backtestStatus?.output_tail ?? []).slice(-24).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>
        </section>

        {historyOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-4xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Rejected Signal History</p>
                  <p className="text-xs text-muted-foreground">
                    최근 judge 불통과 / 비액셔너블 시그널 이력
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => void loadFailedSignalHistory(40)}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="max-h-[65vh] space-y-2 overflow-auto pr-1">
                {historyLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : failedHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No rejected signal history yet.</p>
                ) : (
                  failedHistory.map((item) => (
                    <div key={`${item.ts}-${item.slug ?? "no-slug"}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant="neutral">{item.direction}</Badge>
                        <span className="font-mono text-muted-foreground">{item.ts_utc}</span>
                        <span className="font-mono text-muted-foreground">
                          conf {formatNumber(item.avg_confidence, 3)} / thr {formatNumber(item.threshold, 3)}
                        </span>
                        <span className="font-mono text-muted-foreground">
                          BTC {formatPct(item.market?.btc_change_pct)}
                        </span>
                      </div>
                      <p className="mt-1 text-sm">{item.reason}</p>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
                        {(item.judges ?? []).map((j) => (
                          <div key={`${item.ts}-${j.name}`} className="rounded-md border border-border/60 bg-background/40 p-2">
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-xs font-medium">{j.name}</p>
                              <Badge
                                variant={j.vote === "UP" ? "success" : j.vote === "DOWN" ? "danger" : "neutral"}
                              >
                                {j.vote}
                              </Badge>
                            </div>
                            <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                              conf {formatNumber(j.confidence, 3)}
                            </p>
                            <p className="mt-1 max-h-10 overflow-hidden text-[11px] text-muted-foreground">{j.reason}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        ) : null}

        <footer className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
          <Timer className="h-3.5 w-3.5" />
          <span>Auto refresh: snapshot 2s / history 6s</span>
        </footer>
      </div>
    </main>
  );
}
