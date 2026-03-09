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
    entry_price?: number | null;
    expected_roi?: number | null;
    model_prob?: number | null;
    break_even_prob?: number | null;
    fair_prob_up?: number | null;
    dispersion?: number | null;
    gate?: {
      evaluated: boolean;
      allow: boolean | null;
      reason: string | null;
      expected_roi: number | null;
      model_prob: number | null;
      fair_prob_up: number | null;
      break_even_prob: number | null;
      dispersion: number | null;
      entry_price: number | null;
      per_judge_probs: Record<string, number>;
      blocked_by: string;
      blocked_reason: string | null;
    };
    judges: Array<{
      name: string;
      vote: JudgeVote;
      confidence: number;
      reason: string;
    }>;
  };
  last_actionable_signal?: {
    ts: number | null;
    ts_utc: string;
    window_start: number | null;
    window_end: number | null;
    slug: string | null;
    direction: "UP" | "DOWN" | "NO_TRADE";
    avg_confidence: number;
    reason: string;
    age_sec: number | null;
  } | null;
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

interface LiveControlStatus extends ProcessStatus {
  account?: {
    ok: boolean;
    configured: boolean;
    error?: string | null;
    funder?: string | null;
    collateral_balance?: number | null;
    collateral_allowance?: number | null;
  };
}

interface FailedSignalHistoryItem {
  ts: number;
  ts_utc: string;
  window_start: number | null;
  window_end: number | null;
  slug: string | null;
  history_type: "accepted" | "rejected";
  support_direction?: "UP" | "DOWN" | "NONE";
  support_votes?: number;
  direction: "UP" | "DOWN" | "NO_TRADE";
  avg_confidence: number;
  threshold: number;
  reason: string;
  market?: {
    btc_change_pct: number | null;
    up_mid: number | null;
    down_mid: number | null;
  };
  gate?: {
    evaluated?: boolean;
    allow?: boolean | null;
    reason?: string | null;
    expected_roi?: number | null;
    model_prob?: number | null;
    fair_prob_up?: number | null;
    break_even_prob?: number | null;
    dispersion?: number | null;
    entry_price?: number | null;
    per_judge_probs?: Record<string, number>;
    blocked_by?: string;
    blocked_reason?: string | null;
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
  limit: number;
  offset: number;
  history_type: "all" | "accepted" | "rejected";
  error?: string;
}

interface PaperTradeHistoryItem {
  id: number | null;
  window_start: number | null;
  window_end: number | null;
  direction: "UP" | "DOWN" | "NO_TRADE";
  stake: number | null;
  entry_price: number | null;
  entry_side_price_at_signal: number | null;
  payout_multiple: number | null;
  shares: number | null;
  to_win_total: number | null;
  to_win_pnl: number | null;
  signal_confidence: number | null;
  signal_reason: string;
  close_reason?: string | null;
  status: "OPEN" | "CLOSED";
  opened_at: number | null;
  opened_at_utc: string | null;
  closed_at: number | null;
  actual_outcome: "UP" | "DOWN" | null;
  won: number | null;
  pnl: number | null;
  roi_pct: number | null;
  window?: {
    slug: string | null;
    btc_start_price: number | null;
    btc_end_price: number | null;
    actual_outcome: "UP" | "DOWN" | null;
  };
  odds_at_entry?: {
    ts: number | null;
    up_mid: number | null;
    down_mid: number | null;
    up_bid: number | null;
    up_ask: number | null;
    down_bid: number | null;
    down_ask: number | null;
  };
}

interface PaperTradeHistorySummary {
  open: number;
  closed: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  initial_capital?: number;
  current_equity?: number;
  equity_roi_pct?: number;
  bust_count?: number;
  is_account_busted?: boolean;
  max_drawdown_pct?: number;
  max_consecutive_losses?: number;
}

interface PaperTradeHistoryResponse {
  ok: boolean;
  items: PaperTradeHistoryItem[];
  count: number;
  limit: number;
  offset: number;
  summary?: PaperTradeHistorySummary;
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

function formatUsd(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
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
  const paperSummaryInFlightRef = useRef(false);

  const [paperStatus, setPaperStatus] = useState<ProcessStatus | null>(null);
  const [backtestStatus, setBacktestStatus] = useState<ProcessStatus | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveControlStatus | null>(null);

  const [paperStake, setPaperStake] = useState("1000");
  const [paperInterval, setPaperInterval] = useState("2");
  const [paperSizingMode, setPaperSizingMode] = useState<"adaptive" | "all_in_fixed" | "all_in_equity">("adaptive");
  const [liveStake, setLiveStake] = useState("5");
  const [livePositionMode, setLivePositionMode] = useState<"BOTH" | "UP_ONLY" | "DOWN_ONLY">("BOTH");

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
  const [failedHistoryTotal, setFailedHistoryTotal] = useState(0);
  const [failedHistoryOffset, setFailedHistoryOffset] = useState(0);
  const [failedHistoryType, setFailedHistoryType] = useState<"all" | "accepted" | "rejected">("rejected");
  const failedHistoryPageSize = 20;
  const [paperHistoryOpen, setPaperHistoryOpen] = useState(false);
  const [paperHistoryLoading, setPaperHistoryLoading] = useState(false);
  const [paperHistory, setPaperHistory] = useState<PaperTradeHistoryItem[]>([]);
  const [paperHistoryTotal, setPaperHistoryTotal] = useState(0);
  const [paperHistoryOffset, setPaperHistoryOffset] = useState(0);
  const [paperHistorySummary, setPaperHistorySummary] = useState<PaperTradeHistorySummary | null>(null);
  const paperHistoryPageSize = 20;

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

  async function loadLiveStatus() {
    const res = await fetch("/api/control/live", { cache: "no-store" });
    return (await res.json()) as LiveControlStatus;
  }

  async function loadFailedSignalHistory(
    params: { limit?: number; offset?: number; type?: "all" | "accepted" | "rejected" } = {},
  ) {
    const limit = params.limit ?? failedHistoryPageSize;
    const offset = params.offset ?? failedHistoryOffset;
    const type = params.type ?? failedHistoryType;
    setHistoryLoading(true);
    try {
      const res = await fetch(
        `/api/live/signal-history?limit=${limit}&offset=${offset}&type=${type}`,
        { cache: "no-store" },
      );
      const json = (await res.json()) as FailedSignalHistoryResponse;
      if (json.ok) {
        setFailedHistory(json.items ?? []);
        setFailedHistoryTotal(json.count ?? 0);
        setFailedHistoryOffset(json.offset ?? offset);
        setFailedHistoryType(json.history_type ?? type);
      }
    } finally {
      setHistoryLoading(false);
    }
  }

  async function loadPaperTradeHistory(
    params: { limit?: number; offset?: number } = {},
  ) {
    const limit = params.limit ?? paperHistoryPageSize;
    const offset = params.offset ?? paperHistoryOffset;
    setPaperHistoryLoading(true);
    try {
      const res = await fetch(`/api/live/paper-history?limit=${limit}&offset=${offset}`, {
        cache: "no-store",
      });
      const json = (await res.json()) as PaperTradeHistoryResponse;
      if (json.ok) {
        setPaperHistory(json.items ?? []);
        setPaperHistoryTotal(json.count ?? 0);
        setPaperHistoryOffset(json.offset ?? offset);
        setPaperHistorySummary(json.summary ?? null);
      }
    } finally {
      setPaperHistoryLoading(false);
    }
  }

  async function loadPaperTradeSummary() {
    const res = await fetch("/api/live/paper-history?limit=1&offset=0", {
      cache: "no-store",
    });
    const json = (await res.json()) as PaperTradeHistoryResponse;
    if (json.ok) {
      setPaperHistorySummary(json.summary ?? null);
      setPaperHistoryTotal(json.count ?? 0);
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
        sizing_mode: paperSizingMode,
      }),
    });
    const json = (await res.json()) as ProcessStatus;
    setPaperStatus(json);
    const bt = await loadBacktestStatus();
    setBacktestStatus(bt);
    await loadPaperTradeSummary();
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
    await loadPaperTradeSummary();
  }

  async function startLive() {
    const res = await fetch("/api/control/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "start",
        stake: Number(liveStake || "0"),
        position_mode: livePositionMode,
      }),
    });
    const json = (await res.json()) as LiveControlStatus;
    setLiveStatus(json);
  }

  async function stopLive() {
    const res = await fetch("/api/control/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "stop" }),
    });
    const json = (await res.json()) as LiveControlStatus;
    setLiveStatus(json);
  }

  async function resetPaperHistory() {
    const ok = window.confirm(
      "Reset all paper trade history? This will delete all paper trades and cannot be undone.",
    );
    if (!ok) return;

    const res = await fetch("/api/control/paper", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "reset" }),
    });
    const json = (await res.json()) as ProcessStatus;
    setPaperStatus(json);
    setPaperHistory([]);
    setPaperHistoryTotal(0);
    setPaperHistoryOffset(0);
    setPaperHistorySummary(null);
    await Promise.all([
      loadPaperTradeSummary(),
      loadPaperTradeHistory({ limit: paperHistoryPageSize, offset: 0 }),
    ]);

    try {
      const [snap, hist, paper, backtest] = await Promise.all([
        loadSnapshot(),
        loadHistory(),
        loadPaperStatus(),
        loadBacktestStatus(),
      ]);
      setSnapshot(snap);
      setHistory(hist);
      setPaperStatus(paper);
      setBacktestStatus(backtest);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed after reset");
    }
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
    let paperSummaryTimer: number | null = null;

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
        snapshotTimer = window.setTimeout(() => void pollSnapshot(), 1000);
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
        const [paper, backtest, live] = await Promise.all([
          loadPaperStatus(),
          loadBacktestStatus(),
          loadLiveStatus(),
        ]);
        if (mounted) {
          setPaperStatus(paper);
          setBacktestStatus(backtest);
          setLiveStatus(live);
        }
      } catch (_) {
        // Ignore transient control status errors.
      }
    };

    const pollPaperSummary = async () => {
      if (!mounted) return;
      if (!paperSummaryInFlightRef.current) {
        paperSummaryInFlightRef.current = true;
        try {
          await loadPaperTradeSummary();
        } catch (_) {
          // Ignore summary polling errors.
        } finally {
          paperSummaryInFlightRef.current = false;
        }
      }
      if (mounted) {
        paperSummaryTimer = window.setTimeout(() => void pollPaperSummary(), 8000);
      }
    };

    void pollSnapshot();
    void pollHistory();
    void loadControlOnce();
    void pollPaperSummary();

    return () => {
      mounted = false;
      if (snapshotTimer !== null) window.clearTimeout(snapshotTimer);
      if (historyTimer !== null) window.clearTimeout(historyTimer);
      if (paperSummaryTimer !== null) window.clearTimeout(paperSummaryTimer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const signal = snapshot?.signal;
  const gate = signal?.gate;
  const lastActionableSignal = snapshot?.last_actionable_signal;
  const market = snapshot?.market;
  const windowInfo = snapshot?.window;
  const collector = snapshot?.collector;
  const upBuyOdds = market?.up_ask ?? market?.up_mid ?? null;
  const downBuyOdds = market?.down_ask ?? market?.down_mid ?? null;
  const defaultSeedCapital = Number(paperStake || "1000");
  const seedCapital =
    paperHistorySummary?.initial_capital ??
    (Number.isFinite(defaultSeedCapital) && defaultSeedCapital > 0 ? defaultSeedCapital : 1000);
  const realizedPnl = paperHistorySummary?.total_pnl ?? 0;
  const accountEquity = paperHistorySummary?.current_equity ?? (seedCapital + realizedPnl);
  const accountRoiPct =
    paperHistorySummary?.equity_roi_pct ??
    (seedCapital > 0 ? (realizedPnl / seedCapital) * 100.0 : 0.0);
  const liveBalance = liveStatus?.account?.collateral_balance ?? null;
  const liveAllowance = liveStatus?.account?.collateral_allowance ?? null;
  const liveStakeNum = Number(liveStake || "0");
  const liveStakeValid = Number.isFinite(liveStakeNum) && liveStakeNum > 0;
  const liveStakeOverBalance =
    liveStakeValid &&
    liveBalance !== null &&
    liveBalance !== undefined &&
    Number.isFinite(liveBalance) &&
    liveStakeNum > Number(liveBalance);

  const bannerTitle = useMemo(() => {
    if (signal?.actionable) {
      return `${signal.action_label} opportunity detected`;
    }
    if (lastActionableSignal && lastActionableSignal.direction !== "NO_TRADE") {
      return `Last signal was BUY ${lastActionableSignal.direction}`;
    }
    return "No actionable setup right now";
  }, [signal, lastActionableSignal]);

  const bannerSubtitle = useMemo(() => {
    if (signal?.actionable) {
      return signal.reason;
    }
    if (lastActionableSignal && lastActionableSignal.direction !== "NO_TRADE") {
      const age = ageText(lastActionableSignal.age_sec);
      return `${age} | ${lastActionableSignal.reason}`;
    }
    return signal?.reason ?? "Waiting for data...";
  }, [signal, lastActionableSignal]);

  const bannerConfidence = useMemo(() => {
    if (signal?.actionable) return signal.avg_confidence;
    if (lastActionableSignal?.direction && lastActionableSignal.direction !== "NO_TRADE") {
      return lastActionableSignal.avg_confidence;
    }
    return signal?.avg_confidence ?? null;
  }, [signal, lastActionableSignal]);

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

  const gateJudgeProbRows = useMemo(() => {
    const map = gate?.per_judge_probs ?? {};
    return Object.entries(map)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, prob]) => ({ name, prob }));
  }, [gate?.per_judge_probs]);

  const gateBlockLabel = useMemo(() => {
    const code = gate?.blocked_by ?? "none";
    if (code === "entry_gate") return "blocked by entry gate";
    if (code === "paper_filter") return "blocked by paper filter";
    if (code === "invalid_entry_price") return "blocked by invalid ask";
    if (code === "jury_or_timing") return "blocked by jury/timing";
    return "not blocked";
  }, [gate?.blocked_by]);

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
              Real-time BTC/Polymarket signal monitoring
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
              <p className="mt-1 text-2xl font-bold md:text-3xl">{bannerTitle}</p>
              <p className="mt-1 text-sm text-muted-foreground">{bannerSubtitle}</p>
            </div>
            <div className="rounded-xl border border-border/60 bg-background/40 px-4 py-3 text-right">
              <p className="tiny-label">Confidence</p>
              <p className="font-mono text-3xl font-semibold">{formatNumber(bannerConfidence, 3)}</p>
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
              <CardDescription className="tiny-label">Polymarket Buy Odds (Ask)</CardDescription>
              <CardTitle className="font-mono text-2xl">
                UP {formatNumber(upBuyOdds, 3)} / DOWN {formatNumber(downBuyOdds, 3)}
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Mid UP/DOWN {formatNumber(market?.up_mid, 3)} / {formatNumber(market?.down_mid, 3)} | bid UP/DOWN{" "}
              {formatNumber(market?.up_bid, 3)} / {formatNumber(market?.down_bid, 3)}
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

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-3 xl:items-start">
          <Card className="xl:col-span-2 xl:self-start">
            <CardHeader>
              <CardTitle>Market Motion</CardTitle>
              <CardDescription>Last 30 minutes BTC / UP-DOWN odds trend</CardDescription>
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
                    setFailedHistoryOffset(0);
                    setFailedHistoryType("rejected");
                    void loadFailedSignalHistory({ limit: failedHistoryPageSize, offset: 0, type: "rejected" });
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
            <CardContent className="max-h-[36rem] space-y-2.5 overflow-y-auto pr-1">
              {gate?.evaluated ? (
                <div className="rounded-xl border border-border/70 bg-background/40 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-sm font-medium">Gate Diagnostics</p>
                    <Badge variant={gate.allow ? "success" : "danger"}>
                      {gate.allow ? "PASS" : "BLOCKED"}
                    </Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{gateBlockLabel}</p>
                  <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Model Prob</p>
                      <p className="font-mono">{formatNumber(gate.model_prob, 3)}</p>
                    </div>
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Fair P(UP)</p>
                      <p className="font-mono">{formatNumber(gate.fair_prob_up, 3)}</p>
                    </div>
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Break-even P</p>
                      <p className="font-mono">{formatNumber(gate.break_even_prob, 3)}</p>
                    </div>
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Dispersion</p>
                      <p className="font-mono">{formatNumber(gate.dispersion, 3)}</p>
                    </div>
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Expected ROI</p>
                      <p className="font-mono">
                        {gate.expected_roi === null || gate.expected_roi === undefined
                          ? "--"
                          : formatPct(gate.expected_roi * 100, 2)}
                      </p>
                    </div>
                    <div className="rounded-md border border-border/60 bg-background/30 p-2">
                      <p className="text-muted-foreground">Entry Ask</p>
                      <p className="font-mono">{formatNumber(gate.entry_price, 3)}</p>
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {gate.blocked_reason ?? gate.reason ?? "no gate reason"}
                  </p>
                  <div className="mt-2 rounded-md border border-border/60 bg-background/20 p-2">
                    <p className="tiny-label">Per-judge p_up map</p>
                    {gateJudgeProbRows.length === 0 ? (
                      <p className="mt-1 text-xs text-muted-foreground">No probability map yet.</p>
                    ) : (
                      <div className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-2">
                        {gateJudgeProbRows.map((r) => (
                          <div key={r.name} className="flex items-center justify-between text-xs">
                            <span className="text-muted-foreground">{r.name}</span>
                            <span className="font-mono">{formatNumber(r.prob, 3)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <div className="rounded-xl border border-border/70 bg-background/30 p-3">
                  <p className="text-xs text-muted-foreground">
                    Gate diagnostics will appear once entry-gate evaluation starts.
                  </p>
                </div>
              )}
              {(signal?.judges ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">Waiting for enough lookback data...</p>
              ) : (
                signal?.judges?.map((j) => (
                  <div key={j.name} className="rounded-xl border border-border/70 bg-background/40 p-2.5">
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
                    <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{j.reason}</p>
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
              <CardDescription>Recent outcomes and price movement</CardDescription>
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
                      No window data collected yet.
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

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle>Paper Sim Control</CardTitle>
                  <CardDescription>Start/stop virtual entry engine from this UI</CardDescription>
                </div>
                <button
                  onClick={() => {
                    setPaperHistoryOpen(true);
                    setPaperHistoryOffset(0);
                    void loadPaperTradeHistory({ limit: paperHistoryPageSize, offset: 0 });
                  }}
                  className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                >
                  Trade History
                </button>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs text-muted-foreground">
                  Seed Capital (USD)
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
                <label className="text-xs text-muted-foreground col-span-2">
                  Position Mode
                  <select
                    value={paperSizingMode}
                    onChange={(e) =>
                      setPaperSizingMode(
                        e.target.value as "adaptive" | "all_in_fixed" | "all_in_equity",
                      )
                    }
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="adaptive">adaptive (recommended)</option>
                    <option value="all_in_fixed">all_in_fixed (always seed amount)</option>
                    <option value="all_in_equity">all_in_equity (all available equity)</option>
                  </select>
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
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono">{formatUsd(seedCapital)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono">{formatUsd(accountEquity)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Realized PnL</p>
                  <p className="font-mono">{formatUsd(realizedPnl)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono">{formatPct(accountRoiPct, 2)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">All-Loss Hits</p>
                  <p className="font-mono">{paperHistorySummary?.bust_count ?? 0}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Max Drawdown</p>
                  <p className="font-mono">{formatPct(paperHistorySummary?.max_drawdown_pct ?? 0, 2)}</p>
                </div>
              </div>
              {paperHistorySummary?.is_account_busted ? (
                <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                  Account depleted on realized PnL basis. Consider lowering risk per trade.
                </p>
              ) : null}
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(paperStatus?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>

          <Card className="xl:self-start">
            <CardHeader>
              <CardTitle>Live Trading Control</CardTitle>
              <CardDescription>Real Polymarket execution with balance guard</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">API Config</p>
                  <p className="font-mono">
                    {liveStatus?.account?.configured ? "configured" : "missing"}
                  </p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Collateral Balance</p>
                  <p className="font-mono">{formatUsd(liveBalance)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Collateral Allowance</p>
                  <p className="font-mono">{formatUsd(liveAllowance)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Funder</p>
                  <p className="truncate font-mono">{liveStatus?.account?.funder ?? "--"}</p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs text-muted-foreground">
                  Invest Per Trade (USD)
                  <input
                    value={liveStake}
                    onChange={(e) => setLiveStake(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  Position Mode
                  <select
                    value={livePositionMode}
                    onChange={(e) =>
                      setLivePositionMode(e.target.value as "BOTH" | "UP_ONLY" | "DOWN_ONLY")
                    }
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="BOTH">BOTH</option>
                    <option value="UP_ONLY">UP_ONLY</option>
                    <option value="DOWN_ONLY">DOWN_ONLY</option>
                  </select>
                </label>
              </div>

              {!liveStakeValid ? (
                <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                  Invest amount must be a positive number.
                </p>
              ) : liveStakeOverBalance ? (
                <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                  Invest amount must be less than or equal to your collateral balance.
                </p>
              ) : null}

              {liveStatus?.account?.error ? (
                <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                  {liveStatus.account.error}
                </p>
              ) : null}

              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => void startLive()}
                  disabled={!liveStakeValid || Boolean(liveStakeOverBalance)}
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  Start Live
                </button>
                <button
                  onClick={() => void stopLive()}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <button
                  onClick={async () => setLiveStatus(await loadLiveStatus())}
                  className="rounded-md border border-border/70 bg-background/40 px-3 py-1.5 text-sm"
                >
                  Refresh
                </button>
                <Badge variant={liveStatus?.running ? "success" : "neutral"}>
                  {liveStatus?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>

              <p className="font-mono text-xs text-muted-foreground">
                pid={liveStatus?.pid ?? "-"} | exit={liveStatus?.exit_code ?? "-"} | mode={livePositionMode}
              </p>

              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(liveStatus?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>

          <Card className="xl:self-start">
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

        {paperHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Paper Trade History</p>
                  <p className="text-xs text-muted-foreground">
                    Entry, 5m start price, odds at entry, to-win, and realized PnL.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => void resetPaperHistory()}
                    className="rounded-md border border-amber-400/50 bg-amber-500/20 px-2.5 py-1 text-xs"
                  >
                    Reset
                  </button>
                  <button
                    onClick={() =>
                      void loadPaperTradeHistory({
                        limit: paperHistoryPageSize,
                        offset: paperHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setPaperHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-8">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{paperHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{paperHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {paperHistorySummary?.wins ?? 0}/{paperHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((paperHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono text-sm">{formatUsd(paperHistorySummary?.initial_capital ?? seedCapital)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono text-sm">{formatUsd(paperHistorySummary?.current_equity ?? accountEquity)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono text-sm">{formatPct(paperHistorySummary?.equity_roi_pct ?? accountRoiPct, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">All-Loss Hits</p>
                  <p className="font-mono text-sm">{paperHistorySummary?.bust_count ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Max Loss Streak</p>
                  <p className="font-mono text-sm">{paperHistorySummary?.max_consecutive_losses ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Max Drawdown</p>
                  <p className="font-mono text-sm">{formatPct(paperHistorySummary?.max_drawdown_pct ?? 0, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs md:col-span-2">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(paperHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {paperHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : paperHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No paper trades yet.</p>
                ) : (
                  paperHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        <span className="font-mono text-muted-foreground">{t.opened_at_utc ?? "--"}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>

                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Stake / Entry</p>
                          <p className="font-mono">
                            {formatUsd(t.stake)} @ {formatNumber(t.entry_price, 3)}
                          </p>
                          <p className="font-mono text-muted-foreground">
                            signal-side px {formatNumber(t.entry_side_price_at_signal, 3)}
                          </p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">To Win</p>
                          <p className="font-mono">total {formatUsd(t.to_win_total)}</p>
                          <p className="font-mono text-muted-foreground">pnl {formatUsd(t.to_win_pnl)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">5m BTC Start/End</p>
                          <p className="font-mono">
                            {formatNumber(t.window?.btc_start_price)} / {formatNumber(t.window?.btc_end_price)}
                          </p>
                          <p className="font-mono text-muted-foreground">outcome {t.window?.actual_outcome ?? "--"}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">UP / DOWN at Entry</p>
                          <p className="font-mono">
                            {formatNumber(t.odds_at_entry?.up_ask, 3)} / {formatNumber(t.odds_at_entry?.down_ask, 3)}
                          </p>
                          <p className="font-mono text-muted-foreground">
                            mid {formatNumber(t.odds_at_entry?.up_mid, 3)} / {formatNumber(t.odds_at_entry?.down_mid, 3)}
                          </p>
                        </div>
                      </div>

                      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-mono text-muted-foreground">
                          realized pnl {formatUsd(t.pnl)} ({formatPct(t.roi_pct, 2)})
                        </span>
                        <span className="font-mono text-muted-foreground">
                          conf {formatNumber(t.signal_confidence, 3)}
                        </span>
                        {t.close_reason ? (
                          <span className="font-mono text-amber-300/90">exit {t.close_reason}</span>
                        ) : null}
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{t.signal_reason || "no reason"}</p>
                    </div>
                  ))
                )}
              </div>

              <div className="mt-3 flex items-center justify-between border-t border-border/50 pt-3 text-xs">
                <p className="text-muted-foreground">
                  total {paperHistoryTotal} | showing {paperHistoryTotal === 0 ? 0 : paperHistoryOffset + 1}-
                  {Math.min(paperHistoryOffset + paperHistory.length, paperHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={paperHistoryLoading || paperHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, paperHistoryOffset - paperHistoryPageSize);
                      setPaperHistoryOffset(nextOffset);
                      void loadPaperTradeHistory({ limit: paperHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={paperHistoryLoading || paperHistoryOffset + paperHistoryPageSize >= paperHistoryTotal}
                    onClick={() => {
                      const nextOffset = paperHistoryOffset + paperHistoryPageSize;
                      setPaperHistoryOffset(nextOffset);
                      void loadPaperTradeHistory({ limit: paperHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {historyOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-4xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Signal History</p>
                  <p className="text-xs text-muted-foreground">Keep only important events with pagination.</p>
                </div>
                <div className="flex gap-2">
                  <div className="flex items-center gap-1 rounded-md border border-border/70 bg-background/40 p-1">
                    <button
                      onClick={() => {
                        setFailedHistoryOffset(0);
                        setFailedHistoryType("rejected");
                        void loadFailedSignalHistory({ limit: failedHistoryPageSize, offset: 0, type: "rejected" });
                      }}
                      className={`rounded px-2 py-1 text-xs ${
                        failedHistoryType === "rejected" ? "bg-rose-500/20 text-rose-200" : "text-muted-foreground"
                      }`}
                    >
                      Rejected
                    </button>
                    <button
                      onClick={() => {
                        setFailedHistoryOffset(0);
                        setFailedHistoryType("accepted");
                        void loadFailedSignalHistory({ limit: failedHistoryPageSize, offset: 0, type: "accepted" });
                      }}
                      className={`rounded px-2 py-1 text-xs ${
                        failedHistoryType === "accepted" ? "bg-emerald-500/20 text-emerald-200" : "text-muted-foreground"
                      }`}
                    >
                      Accepted
                    </button>
                    <button
                      onClick={() => {
                        setFailedHistoryOffset(0);
                        setFailedHistoryType("all");
                        void loadFailedSignalHistory({ limit: failedHistoryPageSize, offset: 0, type: "all" });
                      }}
                      className={`rounded px-2 py-1 text-xs ${
                        failedHistoryType === "all" ? "bg-cyan-500/20 text-cyan-200" : "text-muted-foreground"
                      }`}
                    >
                      All
                    </button>
                  </div>
                  <button
                    onClick={() =>
                      void loadFailedSignalHistory({
                        limit: failedHistoryPageSize,
                        offset: failedHistoryOffset,
                        type: failedHistoryType,
                      })
                    }
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
                  <p className="text-sm text-muted-foreground">No signal history yet.</p>
                ) : (
                  failedHistory.map((item) => (
                    <div key={`${item.ts}-${item.slug ?? "no-slug"}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={item.history_type === "accepted" ? "success" : "danger"}>
                          {item.history_type.toUpperCase()}
                        </Badge>
                        <Badge variant="neutral">{item.direction}</Badge>
                        <span className="font-mono text-muted-foreground">
                          support {item.support_direction ?? "NONE"} {item.support_votes ?? 0}/5
                        </span>
                        <span className="font-mono text-muted-foreground">{item.ts_utc}</span>
                        <span className="font-mono text-muted-foreground">
                          conf {formatNumber(item.avg_confidence, 3)} / thr {formatNumber(item.threshold, 3)}
                        </span>
                        <span className="font-mono text-muted-foreground">BTC {formatPct(item.market?.btc_change_pct)}</span>
                      </div>
                      <p className="mt-1 text-sm">{item.reason}</p>
                      <div className="mt-2 rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">Gate</span>
                          <Badge
                            variant={
                              item.gate?.allow === true
                                ? "success"
                                : item.gate?.allow === false
                                  ? "danger"
                                  : "neutral"
                            }
                          >
                            {item.gate?.allow === true ? "PASS" : item.gate?.allow === false ? "BLOCKED" : "N/A"}
                          </Badge>
                          <span className="font-mono text-muted-foreground">
                            by {item.gate?.blocked_by ?? "--"}
                          </span>
                        </div>
                        <div className="mt-1 grid grid-cols-2 gap-2 md:grid-cols-3">
                          <span className="font-mono text-muted-foreground">
                            p={formatNumber(item.gate?.model_prob, 3)}
                          </span>
                          <span className="font-mono text-muted-foreground">
                            fair_up={formatNumber(item.gate?.fair_prob_up, 3)}
                          </span>
                          <span className="font-mono text-muted-foreground">
                            breakeven={formatNumber(item.gate?.break_even_prob, 3)}
                          </span>
                          <span className="font-mono text-muted-foreground">
                            disp={formatNumber(item.gate?.dispersion, 3)}
                          </span>
                          <span className="font-mono text-muted-foreground">
                            ev={item.gate?.expected_roi === null || item.gate?.expected_roi === undefined
                              ? "--"
                              : formatPct(item.gate.expected_roi * 100, 2)}
                          </span>
                          <span className="font-mono text-muted-foreground">
                            ask={formatNumber(item.gate?.entry_price, 3)}
                          </span>
                        </div>
                        <p className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">
                          {item.gate?.blocked_reason || item.gate?.reason || "no gate reason"}
                        </p>
                        {Object.entries(item.gate?.per_judge_probs ?? {}).length > 0 ? (
                          <div className="mt-1 grid grid-cols-1 gap-1 md:grid-cols-2">
                            {Object.entries(item.gate?.per_judge_probs ?? {})
                              .sort(([a], [b]) => a.localeCompare(b))
                              .map(([name, prob]) => (
                                <div key={`${item.ts}-${name}-prob`} className="flex items-center justify-between">
                                  <span className="text-muted-foreground">{name}</span>
                                  <span className="font-mono">{formatNumber(prob, 3)}</span>
                                </div>
                              ))}
                          </div>
                        ) : null}
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
                        {(item.judges ?? []).map((j) => (
                          <div key={`${item.ts}-${j.name}`} className="rounded-md border border-border/60 bg-background/40 p-2">
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-xs font-medium">{j.name}</p>
                              <Badge variant={j.vote === "UP" ? "success" : j.vote === "DOWN" ? "danger" : "neutral"}>
                                {j.vote}
                              </Badge>
                            </div>
                            <p className="mt-1 font-mono text-[11px] text-muted-foreground">conf {formatNumber(j.confidence, 3)}</p>
                            <p className="mt-1 max-h-10 overflow-hidden text-[11px] text-muted-foreground">{j.reason}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))
                )}
              </div>
              <div className="mt-3 flex items-center justify-between border-t border-border/50 pt-3 text-xs">
                <p className="text-muted-foreground">
                  total {failedHistoryTotal} | showing {failedHistoryTotal === 0 ? 0 : failedHistoryOffset + 1}-
                  {Math.min(failedHistoryOffset + failedHistory.length, failedHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={historyLoading || failedHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, failedHistoryOffset - failedHistoryPageSize);
                      setFailedHistoryOffset(nextOffset);
                      void loadFailedSignalHistory({
                        limit: failedHistoryPageSize,
                        offset: nextOffset,
                        type: failedHistoryType,
                      });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={historyLoading || failedHistoryOffset + failedHistoryPageSize >= failedHistoryTotal}
                    onClick={() => {
                      const nextOffset = failedHistoryOffset + failedHistoryPageSize;
                      setFailedHistoryOffset(nextOffset);
                      void loadFailedSignalHistory({
                        limit: failedHistoryPageSize,
                        offset: nextOffset,
                        type: failedHistoryType,
                      });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
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


