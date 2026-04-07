"use client";

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, Gauge, Timer } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Sparkline } from "@/components/dashboard/sparkline";
// import AccountManager from "@/components/dashboard/account-manager";

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
  telegram?: {
    enabled?: boolean;
    configured?: boolean;
    has_token?: boolean;
    has_chat_id?: boolean;
    uses_live_telegram?: boolean;
    token_masked?: string | null;
    chat_id?: string | null;
  };
}

interface LiveControlStatus extends ProcessStatus {
  account?: {
    ok: boolean;
    configured: boolean;
    error?: string | null;
    funder?: string | null;
    funder_source?: string | null;
    signature_type?: number | null;
    signature_type_source?: string | null;
    creds_source?: string | null;
    private_key_set?: boolean;
    warnings?: string[];
    api_credentials?: {
      exists?: boolean;
      source?: string | null;
      api_key?: string | null;
      api_secret?: string | null;
      api_passphrase?: string | null;
      path?: string | null;
    };
    builder_credentials?: {
      exists?: boolean;
      source?: string | null;
      api_key?: string | null;
      api_secret?: string | null;
      api_passphrase?: string | null;
    };
    collateral_balance?: number | null;
    collateral_allowance?: number | null;
  };
  auth?: {
    ok?: boolean;
    persisted_env?: string;
    generated_env?: string;
  };
  telegram?: {
    enabled?: boolean;
    configured?: boolean;
    has_token?: boolean;
    has_chat_id?: boolean;
    token_masked?: string | null;
    chat_id?: string | null;
  };
  telegram_test?: {
    ok?: boolean;
    chat_id?: string | null;
    error?: string | null;
  };
  daily_risk?: {
    seed_capital: number;
    daily_pnl: number;
    daily_trades: number;
    daily_loss_limit: number;
    daily_loss_remaining: number;
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
  odds_at_close?: {
    ts: number | null;
    up_mid: number | null;
    down_mid: number | null;
    up_bid: number | null;
    up_ask: number | null;
    down_bid: number | null;
    down_ask: number | null;
  };
  exit?: {
    kind: string;
    market_px: number | null;
    exit_px: number | null;
    fill_px: number | null;
    fill_notional: number | null;
    settlement_px: number | null;
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

interface LiveTradeHistoryItem extends PaperTradeHistoryItem {
  entry_source?: string | null;
}

interface LiveTradeHistorySummary {
  open: number;
  closed: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_roi_pct?: number;
  avg_stake?: number;
}

interface LiveTradeHistoryResponse {
  ok: boolean;
  items: LiveTradeHistoryItem[];
  count: number;
  limit: number;
  offset: number;
  summary?: LiveTradeHistorySummary;
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

/** Format a Date to "2026. 3. 13. 17:50:59" KST style. */
function _fmtKST(d: Date): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Seoul",
    year: "numeric", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}. ${get("month")}. ${get("day")}. ${get("hour")}:${get("minute")}:${get("second")}`;
}

/** Convert an ISO-8601 UTC string to KST display. */
function toKST(utcStr: string | null | undefined): string {
  if (!utcStr) return "--";
  try {
    const d = new Date(utcStr);
    if (Number.isNaN(d.getTime())) return utcStr;
    return _fmtKST(d);
  } catch {
    return utcStr;
  }
}

/** Convert a unix timestamp (seconds) to KST display string. */
function unixToKST(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || Number.isNaN(ts) || ts <= 0) return "--";
  try {
    return _fmtKST(new Date(ts * 1000));
  } catch {
    return "--";
  }
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

function maskSecret(value: string | null | undefined) {
  const s = String(value ?? "");
  if (!s) return "--";
  if (s.length <= 10) return `${s.slice(0, 2)}***${s.slice(-2)}`;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

function windowSeries(
  points: Array<{ ts: number; value: number }> | null | undefined,
  windowSec: number,
): number[] {
  if (!points || points.length === 0) return [];
  const lastTs = Number(points[points.length - 1]?.ts ?? 0);
  if (!Number.isFinite(lastTs) || lastTs <= 0) {
    return points.map((p) => p.value);
  }
  const cutoff = lastTs - Math.max(1, Math.floor(windowSec));
  return points.filter((p) => Number.isFinite(p.ts) && p.ts >= cutoff).map((p) => p.value);
}

export function LiveDashboard() {
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nowSec, setNowSec] = useState<number>(() => Date.now() / 1000);
  const snapshotInFlightRef = useRef(false);
  const historyInFlightRef = useRef(false);
  const paperSummaryInFlightRef = useRef(false);
  const liveStatusInFlightRef = useRef(false);
  const lastSnapshotWindowSlugRef = useRef<string | null>(null);

  const [paperStatus, setPaperStatus] = useState<ProcessStatus | null>(null);
  const [backtestStatus, setBacktestStatus] = useState<ProcessStatus | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveControlStatus | null>(null);

  const [paperStake, setPaperStake] = useState("1000");
  const [paperInterval, setPaperInterval] = useState("2");
  const [paperSizingMode, setPaperSizingMode] = useState<"adaptive" | "fixed" | "all_in_fixed" | "all_in_equity">("fixed");
  const [paperTelegramNotify, setPaperTelegramNotify] = useState(false);
  const [liveStake, setLiveStake] = useState("5");
  const [liveFixedStake, setLiveFixedStake] = useState("15");
  const [liveSizingMode, setLiveSizingMode] = useState<"adaptive" | "adaptive_seed" | "fixed">("fixed");
  const [livePositionMode, setLivePositionMode] = useState<"BOTH" | "UP_ONLY" | "DOWN_ONLY">("BOTH");
  const [selectedAccountId, setSelectedAccountId] = useState(0);
  const [accountList, setAccountList] = useState<Array<{id: number; name: string}>>([]);
  const [showAddAccount, setShowAddAccount] = useState(false);
  const [newAccountName, setNewAccountName] = useState("");
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [authPrivateKey, setAuthPrivateKey] = useState("");
  const [authFunder, setAuthFunder] = useState("");
  const [authSignatureType, setAuthSignatureType] = useState("-1");
  const [authSaving, setAuthSaving] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authEditEnabled, setAuthEditEnabled] = useState(false);
  const [authShowSecrets, setAuthShowSecrets] = useState(false);
  const [telegramModalOpen, setTelegramModalOpen] = useState(false);
  const [telegramEnabled, setTelegramEnabled] = useState(false);
  const [telegramBotToken, setTelegramBotToken] = useState("");
  const [telegramChatId, setTelegramChatId] = useState("");
  const [telegramSaving, setTelegramSaving] = useState(false);
  const [telegramError, setTelegramError] = useState<string | null>(null);
  const [telegramTestMessage, setTelegramTestMessage] = useState("");

  const [seedCapitalInput, setSeedCapitalInput] = useState("");
  const [seedCapitalSaving, setSeedCapitalSaving] = useState(false);

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
  const [liveHistoryOpen, setLiveHistoryOpen] = useState(false);
  const [liveHistoryLoading, setLiveHistoryLoading] = useState(false);
  const [liveHistory, setLiveHistory] = useState<LiveTradeHistoryItem[]>([]);
  const [liveHistoryTotal, setLiveHistoryTotal] = useState(0);
  const [liveHistoryOffset, setLiveHistoryOffset] = useState(0);
  const [liveHistorySummary, setLiveHistorySummary] = useState<LiveTradeHistorySummary | null>(null);
  const liveHistoryPageSize = 20;
  const marketMotionCardRef = useRef<HTMLDivElement | null>(null);
  const [marketMotionCardHeight, setMarketMotionCardHeight] = useState<number | null>(null);

  // ---- BTC 15min & ETH 5min market control state ----
  interface MarketControlStatus {
    signal: { ok: boolean; running?: boolean; pid?: number | null; output_tail?: string[] };
    paper: { ok: boolean; running?: boolean; pid?: number | null; output_tail?: string[] };
    live: { ok: boolean; running?: boolean; pid?: number | null; output_tail?: string[] };
  }
  const [btc15Status, setBtc15Status] = useState<MarketControlStatus | null>(null);
  const [btc15PaperStake, setBtc15PaperStake] = useState("100");
  const [btc15PaperSizing, setBtc15PaperSizing] = useState("fixed");
  const [btc15LiveStake, setBtc15LiveStake] = useState("15");
  const [btc15LiveSizing, setBtc15LiveSizing] = useState("fixed");

  const [eth5Status, setEth5Status] = useState<MarketControlStatus | null>(null);
  const [eth5PaperStake, setEth5PaperStake] = useState("100");
  const [eth5PaperSizing, setEth5PaperSizing] = useState("fixed");
  const [eth5LiveStake, setEth5LiveStake] = useState("15");
  const [eth5LiveSizing, setEth5LiveSizing] = useState("fixed");

  const [btc15SelectedAccountId, setBtc15SelectedAccountId] = useState(0);
  const [eth5SelectedAccountId, setEth5SelectedAccountId] = useState(0);

  // ---- BTC 15min & ETH 5min paper equity + trade history state ----
  interface MarketPaperSummary {
    total_pnl: number;
    initial_capital: number;
    current_equity: number;
    equity_roi_pct: number;
    bust_count: number;
    max_drawdown_pct: number;
    wins: number;
    losses: number;
    open: number;
    closed: number;
    win_rate: number;
    max_consecutive_losses?: number;
  }
  interface MarketLiveDailyPnl {
    daily_pnl: number;
    daily_trades: number;
  }

  const [btc15PaperSummary, setBtc15PaperSummary] = useState<MarketPaperSummary | null>(null);
  const [eth5PaperSummary, setEth5PaperSummary] = useState<MarketPaperSummary | null>(null);
  const [btc15LiveDaily, setBtc15LiveDaily] = useState<MarketLiveDailyPnl | null>(null);
  const [eth5LiveDaily, setEth5LiveDaily] = useState<MarketLiveDailyPnl | null>(null);

  // Trade history modals for btc15/eth5
  const [btc15PaperHistoryOpen, setBtc15PaperHistoryOpen] = useState(false);
  const [btc15PaperHistoryLoading, setBtc15PaperHistoryLoading] = useState(false);
  const [btc15PaperHistory, setBtc15PaperHistory] = useState<PaperTradeHistoryItem[]>([]);
  const [btc15PaperHistoryTotal, setBtc15PaperHistoryTotal] = useState(0);
  const [btc15PaperHistoryOffset, setBtc15PaperHistoryOffset] = useState(0);
  const [btc15PaperHistorySummary, setBtc15PaperHistorySummary] = useState<PaperTradeHistorySummary | null>(null);

  const [btc15LiveHistoryOpen, setBtc15LiveHistoryOpen] = useState(false);
  const [btc15LiveHistoryLoading, setBtc15LiveHistoryLoading] = useState(false);
  const [btc15LiveHistory, setBtc15LiveHistory] = useState<LiveTradeHistoryItem[]>([]);
  const [btc15LiveHistoryTotal, setBtc15LiveHistoryTotal] = useState(0);
  const [btc15LiveHistoryOffset, setBtc15LiveHistoryOffset] = useState(0);
  const [btc15LiveHistorySummary, setBtc15LiveHistorySummary] = useState<LiveTradeHistorySummary | null>(null);

  const [eth5PaperHistoryOpen, setEth5PaperHistoryOpen] = useState(false);
  const [eth5PaperHistoryLoading, setEth5PaperHistoryLoading] = useState(false);
  const [eth5PaperHistory, setEth5PaperHistory] = useState<PaperTradeHistoryItem[]>([]);
  const [eth5PaperHistoryTotal, setEth5PaperHistoryTotal] = useState(0);
  const [eth5PaperHistoryOffset, setEth5PaperHistoryOffset] = useState(0);
  const [eth5PaperHistorySummary, setEth5PaperHistorySummary] = useState<PaperTradeHistorySummary | null>(null);

  const [eth5LiveHistoryOpen, setEth5LiveHistoryOpen] = useState(false);
  const [eth5LiveHistoryLoading, setEth5LiveHistoryLoading] = useState(false);
  const [eth5LiveHistory, setEth5LiveHistory] = useState<LiveTradeHistoryItem[]>([]);
  const [eth5LiveHistoryTotal, setEth5LiveHistoryTotal] = useState(0);
  const [eth5LiveHistoryOffset, setEth5LiveHistoryOffset] = useState(0);
  const [eth5LiveHistorySummary, setEth5LiveHistorySummary] = useState<LiveTradeHistorySummary | null>(null);

  const marketHistoryPageSize = 20;

  async function loadMarketControlStatus(market: "btc15" | "eth5") {
    try {
      const res = await fetch(`/api/control/${market}`, { cache: "no-store" });
      return (await res.json()) as MarketControlStatus & { ok: boolean };
    } catch {
      return null;
    }
  }

  async function marketControlAction(
    market: "btc15" | "eth5",
    action: string,
    extra: Record<string, unknown> = {},
  ) {
    const res = await fetch(`/api/control/${market}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...extra }),
    });
    const json = await res.json();
    // Refresh status after action
    const status = await loadMarketControlStatus(market);
    if (market === "btc15" && status) setBtc15Status(status);
    if (market === "eth5" && status) setEth5Status(status);
    return json;
  }

  // ---- Market paper/live history loaders ----
  async function loadMarketPaperHistory(
    market: "btc15" | "eth5",
    params: { limit?: number; offset?: number } = {},
  ) {
    const limit = params.limit ?? marketHistoryPageSize;
    const offset = params.offset ?? 0;
    const setLoading = market === "btc15" ? setBtc15PaperHistoryLoading : setEth5PaperHistoryLoading;
    const setItems = market === "btc15" ? setBtc15PaperHistory : setEth5PaperHistory;
    const setTotal = market === "btc15" ? setBtc15PaperHistoryTotal : setEth5PaperHistoryTotal;
    const setOff = market === "btc15" ? setBtc15PaperHistoryOffset : setEth5PaperHistoryOffset;
    const setSummary = market === "btc15" ? setBtc15PaperHistorySummary : setEth5PaperHistorySummary;
    setLoading(true);
    try {
      const res = await fetch(`/api/${market}/paper-history?limit=${limit}&offset=${offset}`, { cache: "no-store" });
      const json = (await res.json()) as PaperTradeHistoryResponse;
      if (json.ok) {
        setItems(json.items ?? []);
        setTotal(json.count ?? 0);
        setOff(json.offset ?? offset);
        setSummary(json.summary ?? null);
      }
    } finally {
      setLoading(false);
    }
  }

  async function loadMarketPaperSummary(market: "btc15" | "eth5") {
    try {
      const res = await fetch(`/api/${market}/paper-history?limit=1&offset=0`, { cache: "no-store" });
      const json = (await res.json()) as PaperTradeHistoryResponse;
      if (json.ok && json.summary) {
        const s = json.summary;
        const summary: MarketPaperSummary = {
          total_pnl: s.total_pnl ?? 0,
          initial_capital: s.initial_capital ?? 100,
          current_equity: s.current_equity ?? 100,
          equity_roi_pct: s.equity_roi_pct ?? 0,
          bust_count: s.bust_count ?? 0,
          max_drawdown_pct: s.max_drawdown_pct ?? 0,
          wins: s.wins ?? 0,
          losses: s.losses ?? 0,
          open: s.open ?? 0,
          closed: s.closed ?? 0,
          win_rate: s.win_rate ?? 0,
          max_consecutive_losses: s.max_consecutive_losses ?? 0,
        };
        if (market === "btc15") setBtc15PaperSummary(summary);
        else setEth5PaperSummary(summary);
      }
    } catch { /* ignore */ }
  }

  async function loadMarketLiveHistory(
    market: "btc15" | "eth5",
    params: { limit?: number; offset?: number } = {},
  ) {
    const limit = params.limit ?? marketHistoryPageSize;
    const offset = params.offset ?? 0;
    const setLoading = market === "btc15" ? setBtc15LiveHistoryLoading : setEth5LiveHistoryLoading;
    const setItems = market === "btc15" ? setBtc15LiveHistory : setEth5LiveHistory;
    const setTotal = market === "btc15" ? setBtc15LiveHistoryTotal : setEth5LiveHistoryTotal;
    const setOff = market === "btc15" ? setBtc15LiveHistoryOffset : setEth5LiveHistoryOffset;
    const setSummary = market === "btc15" ? setBtc15LiveHistorySummary : setEth5LiveHistorySummary;
    setLoading(true);
    try {
      const res = await fetch(`/api/${market}/live-trade-history?limit=${limit}&offset=${offset}`, { cache: "no-store" });
      const json = (await res.json()) as LiveTradeHistoryResponse;
      if (json.ok) {
        setItems(json.items ?? []);
        setTotal(json.count ?? 0);
        setOff(json.offset ?? offset);
        setSummary(json.summary ?? null);
      }
    } finally {
      setLoading(false);
    }
  }

  async function loadMarketLiveDailyPnl(market: "btc15" | "eth5") {
    try {
      const res = await fetch(`/api/${market}/live-trade-history?limit=1&offset=0`, { cache: "no-store" });
      const json = (await res.json()) as LiveTradeHistoryResponse;
      if (json.ok && json.summary) {
        const pnl: MarketLiveDailyPnl = {
          daily_pnl: json.summary.total_pnl ?? 0,
          daily_trades: (json.summary.open ?? 0) + (json.summary.closed ?? 0),
        };
        if (market === "btc15") setBtc15LiveDaily(pnl);
        else setEth5LiveDaily(pnl);
      }
    } catch { /* ignore */ }
  }

  async function loadSnapshot() {
    const res = await fetch("/api/live/snapshot", { cache: "no-store" });
    const json = (await res.json()) as SnapshotResponse;
    if (!json.ok) throw new Error(json.error || "Snapshot unavailable");
    return json;
  }

  const fetchAccountList = async () => {
    try {
      const res = await fetch("/api/accounts");
      const data = await res.json();
      if (data.accounts) setAccountList(data.accounts);
    } catch {}
  };
  useEffect(() => { fetchAccountList(); const iv = setInterval(fetchAccountList, 15000); return () => clearInterval(iv); }, []);

  useEffect(() => {
    const el = marketMotionCardRef.current;
    if (!el) return;

    let raf = 0;
    const syncHeight = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const next = Math.round(el.getBoundingClientRect().height);
        if (next > 0) {
          setMarketMotionCardHeight((prev) => (prev === next ? prev : next));
        }
      });
    };

    syncHeight();
    const ro = new ResizeObserver(syncHeight);
    ro.observe(el);
    window.addEventListener("resize", syncHeight);
    return () => {
      window.removeEventListener("resize", syncHeight);
      ro.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [history, snapshot]);

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

  async function refreshLiveStatus() {
    if (liveStatusInFlightRef.current) return null;
    liveStatusInFlightRef.current = true;
    try {
      const live = await loadLiveStatus();
      setLiveStatus(live);
      return live;
    } finally {
      liveStatusInFlightRef.current = false;
    }
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

  async function loadLiveTradeHistory(
    params: { limit?: number; offset?: number } = {},
  ) {
    const limit = params.limit ?? liveHistoryPageSize;
    const offset = params.offset ?? liveHistoryOffset;
    setLiveHistoryLoading(true);
    try {
      const res = await fetch(`/api/live/live-trade-history?limit=${limit}&offset=${offset}`, {
        cache: "no-store",
      });
      const json = (await res.json()) as LiveTradeHistoryResponse;
      if (json.ok) {
        setLiveHistory(json.items ?? []);
        setLiveHistoryTotal(json.count ?? 0);
        setLiveHistoryOffset(json.offset ?? offset);
        setLiveHistorySummary(json.summary ?? null);
      }
    } finally {
      setLiveHistoryLoading(false);
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

  async function savePaperTelegramNotify() {
    const res = await fetch("/api/control/paper", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "telegram_config",
        enabled: paperTelegramNotify,
      }),
    });
    const json = (await res.json()) as ProcessStatus;
    setPaperStatus(json);
  }

  async function saveSeedCapital() {
    const val = Number(seedCapitalInput);
    if (!Number.isFinite(val) || val <= 0) return;
    setSeedCapitalSaving(true);
    try {
      await fetch("/api/control/live", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "seed_capital", seed_capital: val }),
      });
      // Refresh live status so daily_risk updates immediately
      // Small delay to let backend process the env change
      await new Promise((r) => setTimeout(r, 300));
      liveStatusInFlightRef.current = false; // force refresh
      await refreshLiveStatus();
    } finally {
      setSeedCapitalSaving(false);
    }
  }

  async function startLive() {
    const parsedStake = liveSizingMode === "fixed"
      ? Number(liveFixedStake || "15")
      : Number(liveStake || "0");
    const stakeForRequest = Number.isFinite(parsedStake) ? parsedStake : 0;
    const res = await fetch("/api/control/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "start",
        stake: stakeForRequest,
        sizing_mode: liveSizingMode,
        position_mode: livePositionMode,
        fixed_stake: liveSizingMode === "fixed" ? stakeForRequest : undefined,
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

  async function saveLiveAuth() {
    const pk = authPrivateKey.trim();
    const hasExistingPrivateKey = Boolean(liveStatus?.account?.private_key_set);
    if (!pk && !hasExistingPrivateKey) {
      setAuthError("Private key is required for first-time setup.");
      return;
    }
    setAuthSaving(true);
    setAuthError(null);
    try {
      const sig = Number(authSignatureType || "-1");
      const payload = {
        action: "auth_config",
        private_key: pk,
        funder: authFunder.trim(),
        signature_type: Number.isFinite(sig) ? sig : -1,
      };
      const res = await fetch("/api/control/live", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const json = (await res.json()) as LiveControlStatus;
      setLiveStatus(json);
      if (!json.ok) {
        setAuthError(json.message || json.error || "Failed to save auth");
        return;
      }
      setAuthPrivateKey("");
      setAuthEditEnabled(false);
      setAuthModalOpen(false);
    } catch (e) {
      setAuthError(e instanceof Error ? e.message : "Failed to save auth");
    } finally {
      setAuthSaving(false);
    }
  }

  function openAuthModal() {
    setAuthError(null);
    setAuthPrivateKey("");
    setAuthFunder(liveStatus?.account?.funder ?? "");
    const sig = liveStatus?.account?.signature_type;
    setAuthSignatureType(sig === 0 || sig === 1 || sig === 2 ? String(sig) : "-1");
    setAuthEditEnabled(!Boolean(liveStatus?.account?.private_key_set));
    setAuthShowSecrets(false);
    setAuthModalOpen(true);
  }

  function enableAuthEdit() {
    const hasExistingCreds = Boolean(liveStatus?.account?.api_credentials?.exists);
    if (hasExistingCreds) {
      const ok = window.confirm(
        "기존 API Key/Secret/Passphrase가 교체됩니다. 계속 수정할까요?",
      );
      if (!ok) return;
    }
    setAuthEditEnabled(true);
    setAuthError(null);
  }

  function openTelegramModal() {
    setTelegramError(null);
    setTelegramEnabled(liveTelegram?.enabled ?? true);
    setTelegramBotToken("");
    setTelegramChatId(liveTelegram?.chat_id ?? "");
    setTelegramTestMessage("");
    setTelegramModalOpen(true);
  }

  async function saveTelegramConfig(sendTest = false) {
    setTelegramSaving(true);
    setTelegramError(null);
    try {
      const tokenInput = telegramBotToken.trim();
      const hasExistingToken = Boolean(liveTelegram?.has_token);
      if (!tokenInput && !hasExistingToken) {
        setTelegramError("Bot token is required for first-time setup.");
        return;
      }
      const payload = {
        action: "telegram_config",
        enabled: telegramEnabled,
        bot_token: tokenInput,
        chat_id: telegramChatId.trim(),
        send_test: sendTest,
        test_message: telegramTestMessage.trim(),
      };
      const res = await fetch("/api/control/live", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const json = (await res.json()) as LiveControlStatus;
      setLiveStatus(json);
      const test = json.telegram_test;
      if (!json.ok) {
        const rawErr =
          json.message ||
          json.error ||
          test?.error ||
          "Failed to save Telegram settings";
        const lowered = String(rawErr).toLowerCase();
        if (
          sendTest &&
          (lowered.includes("no updates found") ||
            lowered.includes("send /start") ||
            lowered.includes("chat id auto-resolve failed"))
        ) {
          setTelegramError(
            "봇 채팅에서 /start 를 먼저 보내고, 'Verify /start + Send Test'를 다시 눌러주세요.",
          );
          return;
        }
        setTelegramError(
          rawErr,
        );
        return;
      }
      if (sendTest && test && !test.ok) {
        setTelegramError(test.error || "Telegram test failed");
        return;
      }
      if (sendTest) {
        const resolvedChat = json.telegram?.chat_id ?? test?.chat_id ?? "";
        if (resolvedChat) setTelegramChatId(String(resolvedChat));
        setTelegramModalOpen(false);
      }
    } catch (e) {
      setTelegramError(
        e instanceof Error ? e.message : "Failed to save Telegram settings",
      );
    } finally {
      setTelegramSaving(false);
    }
  }

  async function sendTelegramTestOnly() {
    setTelegramSaving(true);
    setTelegramError(null);
    try {
      const payload = {
        action: "telegram_test",
        bot_token: telegramBotToken.trim(),
        chat_id: telegramChatId.trim(),
        message: telegramTestMessage.trim(),
      };
      const res = await fetch("/api/control/live", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const json = (await res.json()) as LiveControlStatus;
      setLiveStatus(json);
      if (!json.ok) {
        setTelegramError(
          json.telegram_test?.error ||
            json.message ||
            json.error ||
            "Telegram test failed",
        );
        return;
      }
      const resolvedChat = json.telegram_test?.chat_id ?? "";
      if (resolvedChat) setTelegramChatId(String(resolvedChat));
    } catch (e) {
      setTelegramError(e instanceof Error ? e.message : "Telegram test failed");
    } finally {
      setTelegramSaving(false);
    }
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
    let liveStatusTimer: number | null = null;
    let controlStatusTimer: number | null = null;
    let marketControlTimer: number | null = null;

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
          const nextSlug = data.window?.slug ?? null;
          const prevSlug = lastSnapshotWindowSlugRef.current;
          if (
            mounted &&
            prevSlug !== null &&
            nextSlug !== null &&
            nextSlug !== prevSlug
          ) {
            try {
              await refreshLiveStatus();
            } catch (_) {
              // Ignore transient live status refresh errors on window rollover.
            }
          }
          lastSnapshotWindowSlugRef.current = nextSlug;
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

    const pollLiveStatus = async () => {
      if (!mounted) return;
      let nextDelayMs = 30000;
      try {
        const live = await refreshLiveStatus();
        if (live?.running) nextDelayMs = 10000;
      } catch (_) {
        // Ignore transient live status errors.
      }
      if (mounted) {
        // Balance/account snapshot refresh cadence:
        // - running: 10s (faster reflection after trade close)
        // - stopped: 30s
        liveStatusTimer = window.setTimeout(() => void pollLiveStatus(), nextDelayMs);
      }
    };

    const pollControlStatus = async () => {
      if (!mounted) return;
      let nextDelayMs = 10000;
      try {
        const [paper, backtest] = await Promise.all([loadPaperStatus(), loadBacktestStatus()]);
        if (mounted) {
          setPaperStatus(paper);
          setBacktestStatus(backtest);
        }
        if (paper?.running || backtest?.running) {
          // Keep log tail lively while a subprocess is active.
          nextDelayMs = 2000;
        }
      } catch (_) {
        // Ignore transient control status errors.
      }
      if (mounted) {
        controlStatusTimer = window.setTimeout(() => void pollControlStatus(), nextDelayMs);
      }
    };

    const pollMarketControl = async () => {
      if (!mounted) return;
      try {
        const [btc15, eth5] = await Promise.all([
          loadMarketControlStatus("btc15"),
          loadMarketControlStatus("eth5"),
        ]);
        if (mounted) {
          if (btc15) setBtc15Status(btc15);
          if (eth5) setEth5Status(eth5);
        }
      } catch (_) {
        // Ignore transient market control status errors.
      }
      if (mounted) {
        marketControlTimer = window.setTimeout(() => void pollMarketControl(), 10000);
      }
    };

    let marketSummaryTimer: number | null = null;
    const pollMarketSummaries = async () => {
      if (!mounted) return;
      try {
        await Promise.all([
          loadMarketPaperSummary("btc15"),
          loadMarketPaperSummary("eth5"),
          loadMarketLiveDailyPnl("btc15"),
          loadMarketLiveDailyPnl("eth5"),
        ]);
      } catch (_) { /* ignore */ }
      if (mounted) {
        marketSummaryTimer = window.setTimeout(() => void pollMarketSummaries(), 30000);
      }
    };

    void pollSnapshot();
    void pollHistory();
    void loadControlOnce();
    void pollPaperSummary();
    void pollLiveStatus();
    void pollControlStatus();
    void pollMarketControl();
    void pollMarketSummaries();

    return () => {
      mounted = false;
      if (snapshotTimer !== null) window.clearTimeout(snapshotTimer);
      if (historyTimer !== null) window.clearTimeout(historyTimer);
      if (paperSummaryTimer !== null) window.clearTimeout(paperSummaryTimer);
      if (liveStatusTimer !== null) window.clearTimeout(liveStatusTimer);
      if (controlStatusTimer !== null) window.clearTimeout(controlStatusTimer);
      if (marketControlTimer !== null) window.clearTimeout(marketControlTimer);
      if (marketSummaryTimer !== null) window.clearTimeout(marketSummaryTimer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const meta = (liveStatus?.meta ?? {}) as Record<string, unknown>;
    const rawSizing = String(meta.sizing_mode ?? "").toLowerCase();
    if (rawSizing === "adaptive" || rawSizing === "adaptive_seed" || rawSizing === "fixed") {
      setLiveSizingMode(rawSizing);
    }
    const savedFixedStake = Number(meta.stake_per_trade ?? 0);
    if (savedFixedStake > 0) {
      setLiveFixedStake(String(savedFixedStake));
    }
    const rawPos = String(meta.position_mode ?? "").toUpperCase();
    if (rawPos === "BOTH" || rawPos === "UP_ONLY" || rawPos === "DOWN_ONLY") {
      setLivePositionMode(rawPos);
    }
    const running = Boolean(liveStatus?.running);
    const rawStake = Number(meta.requested_stake ?? meta.stake_per_trade);
    if (!running && Number.isFinite(rawStake) && rawStake > 0) {
      setLiveStake(String(rawStake));
    }
    // Populate seed capital input from server if user hasn't typed anything
    const serverSeed = liveStatus?.daily_risk?.seed_capital;
    if (serverSeed && serverSeed > 0 && !seedCapitalInput) {
      setSeedCapitalInput(String(serverSeed));
    }
  }, [liveStatus]);

  useEffect(() => {
    const enabled = paperStatus?.telegram?.enabled;
    if (typeof enabled === "boolean") {
      setPaperTelegramNotify(enabled);
    }
  }, [paperStatus?.telegram?.enabled]);

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
  const paperRunning = Boolean(paperStatus?.running);
  const paperTelegram = paperStatus?.telegram;
  const liveBalance = liveStatus?.account?.collateral_balance ?? null;
  const liveAllowance = liveStatus?.account?.collateral_allowance ?? null;
  const liveApiCreds = liveStatus?.account?.api_credentials;
  const liveTelegram = liveStatus?.telegram;
  const hasLiveApiCreds = Boolean(liveApiCreds?.exists);
  const liveStakeNum = Number(liveStake || "0");
  const liveStakeValid = liveSizingMode === "adaptive" || liveSizingMode === "adaptive_seed" || (Number.isFinite(liveStakeNum) && liveStakeNum > 0);
  const liveStakeOverBalance =
    liveSizingMode === "fixed" &&
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
  const btcSeries5m = useMemo(() => windowSeries(history?.btc, 5 * 60), [history?.btc]);

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
              <p className="font-mono text-xs text-muted-foreground">{toKST(snapshot?.server_time_utc) ?? "no time"}</p>
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
          <Card ref={marketMotionCardRef} className="xl:col-span-2">
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
              <div className="rounded-xl border border-border/70 bg-background/20 p-3">
                <p className="tiny-label mb-3">Last 5 minutes (entry horizon)</p>
                <div className="grid grid-cols-1 gap-4">
                  <div>
                    <p className="tiny-label mb-2">BTC 5m</p>
                    <Sparkline
                      values={btcSeries5m}
                      stroke="#22d3ee"
                      fillGradient="#22d3ee"
                      className="h-[120px]"
                    />
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card
            className="flex flex-col xl:h-[var(--market-motion-h)]"
            style={
              marketMotionCardHeight
                ? ({ "--market-motion-h": `${marketMotionCardHeight}px` } as CSSProperties)
                : undefined
            }
          >
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
            <CardContent className="flex-1 min-h-0 space-y-2.5 overflow-y-auto pr-1">
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
                        {unixToKST(w.window_start)}
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
                {/* Interval removed — fixed at 0.1s in backend */}
                <label className="text-xs text-muted-foreground col-span-2">
                  Sizing Mode
                  <select
                    value={paperSizingMode}
                    onChange={(e) =>
                      setPaperSizingMode(
                        e.target.value as "adaptive" | "fixed" | "all_in_fixed" | "all_in_equity",
                      )
                    }
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive</option>
                    <option value="all_in_fixed">all_in_fixed (always seed amount)</option>
                    <option value="all_in_equity">all_in_equity (all available equity)</option>
                  </select>
                </label>
              </div>
              <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                <label className="flex items-center gap-2 text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={paperTelegramNotify}
                    onChange={(e) => setPaperTelegramNotify(e.target.checked)}
                    disabled={paperRunning}
                    className="h-4 w-4 rounded border-border/70 bg-background/40 disabled:cursor-not-allowed"
                  />
                  Send Telegram on Paper OPEN fill
                </label>
                <p className="mt-1 text-muted-foreground">
                  Uses Live Telegram token/chat settings. Change is allowed only when Paper is stopped.
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    onClick={() => void savePaperTelegramNotify()}
                    disabled={paperRunning}
                    className="rounded-md border border-sky-400/50 bg-sky-500/20 px-2.5 py-1 text-xs disabled:opacity-40"
                  >
                    Save Alert Option
                  </button>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    configured {paperTelegram?.configured ? "yes" : "no"} | token{" "}
                    {paperTelegram?.has_token ? "set" : "missing"} | chat{" "}
                    {paperTelegram?.has_chat_id ? "set" : "missing"}
                  </span>
                </div>
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
                <Badge variant={paperRunning ? "success" : "neutral"}>
                  {paperRunning ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={paperStatus?.pid ?? "-"} | exit={paperStatus?.exit_code ?? "-"}
              </p>
              {paperStatus?.message ? (
                <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                  {paperStatus.message}
                </p>
              ) : null}
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
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Live Trading Control</CardTitle>
                  <CardDescription>Real Polymarket execution with balance guard</CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    value={selectedAccountId}
                    onChange={(e) => {
                      const val = e.target.value;
                      if (val === "__add__") {
                        setShowAddAccount(true);
                      } else {
                        setSelectedAccountId(Number(val));
                      }
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2 py-1 text-sm"
                  >
                    <option value={0}>Main Account</option>
                    {accountList.map((a) => (
                      <option key={a.id} value={a.id}>{a.name || `Account ${a.id}`}</option>
                    ))}
                    <option value="__add__">+ Add Account</option>
                  </select>
                  {selectedAccountId > 0 && (
                    <button
                      onClick={async () => {
                        if (!confirm("Delete this account?")) return;
                        await fetch("/api/accounts", {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ action: "delete", account_id: selectedAccountId }),
                        });
                        setSelectedAccountId(0);
                        fetchAccountList();
                      }}
                      className="px-2 py-1 text-xs bg-red-700 text-white rounded hover:bg-red-600"
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
            </CardHeader>
            {showAddAccount && (
              <div className="mx-4 mb-3 p-3 rounded-lg border border-border bg-background/50">
                <div className="flex items-center gap-2">
                  <input
                    placeholder="Account name"
                    value={newAccountName}
                    onChange={(e) => setNewAccountName(e.target.value)}
                    className="flex-1 rounded border border-border bg-background px-2 py-1 text-sm"
                  />
                  <button
                    onClick={async () => {
                      if (!newAccountName.trim()) return;
                      await fetch("/api/accounts", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ action: "save", name: newAccountName.trim() }),
                      });
                      setNewAccountName("");
                      setShowAddAccount(false);
                      fetchAccountList();
                    }}
                    className="px-3 py-1 bg-green-600 text-white rounded text-sm"
                  >
                    Create
                  </button>
                  <button onClick={() => setShowAddAccount(false)} className="px-3 py-1 bg-muted rounded text-sm">Cancel</button>
                </div>
              </div>
            )}
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
                  <p className="text-muted-foreground">Seed Capital</p>
                  <div className="flex items-center gap-1">
                    <input
                      type="number"
                      step="0.01"
                      min="0"
                      placeholder={formatUsd(liveBalance) ?? "--"}
                      value={seedCapitalInput}
                      onChange={(e) => setSeedCapitalInput(e.target.value)}
                      className="w-full min-w-0 bg-transparent font-mono outline-none placeholder:text-muted-foreground/50"
                    />
                    <button
                      onClick={() => void saveSeedCapital()}
                      disabled={seedCapitalSaving || !seedCapitalInput}
                      className="shrink-0 rounded border border-emerald-400/50 bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-medium disabled:opacity-40"
                    >
                      {seedCapitalSaving ? "..." : "Save"}
                    </button>
                  </div>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Funder</p>
                  <p className="truncate font-mono">{liveStatus?.account?.funder ?? "--"}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Today&apos;s PnL</p>
                  <p className={`font-mono font-semibold ${(liveStatus?.daily_risk?.daily_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    ${(liveStatus?.daily_risk?.daily_pnl ?? 0).toFixed(2)} ({liveStatus?.daily_risk?.daily_trades ?? 0} trades)
                  </p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Daily Loss Limit</p>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      step="1"
                      min="1"
                      defaultValue={liveStatus?.daily_risk?.daily_loss_limit ?? 60}
                      id="dailyLossLimitInput"
                      className="w-20 rounded border border-border/70 bg-background/40 px-1 py-0.5 font-mono text-xs"
                    />
                    <button
                      className="rounded bg-yellow-600 px-2 py-0.5 text-xs text-white hover:bg-yellow-500"
                      onClick={async () => {
                        const val = parseFloat((document.getElementById("dailyLossLimitInput") as HTMLInputElement)?.value || "0");
                        if (val > 0) {
                          await fetch("/api/control/live/daily-loss-limit", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ daily_loss_limit: val }),
                          });
                          void refreshLiveStatus();
                        }
                      }}
                    >Save</button>
                  </div>
                  <p className={`font-mono mt-1 ${(liveStatus?.daily_risk?.daily_loss_remaining ?? 50) < (liveStatus?.daily_risk?.daily_loss_limit ?? 50) * 0.3 ? "text-red-400" : ""}`}>
                    -${(liveStatus?.daily_risk?.daily_loss_limit ?? 0).toFixed(2)} | left ${(liveStatus?.daily_risk?.daily_loss_remaining ?? 0).toFixed(2)}
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <label className="text-xs text-muted-foreground">
                  {liveSizingMode === "fixed" ? "Fixed Invest Per Trade (USD)" : "Invest Per Trade (USD)"}
                  <input
                    value={liveSizingMode === "fixed" ? liveFixedStake : liveStake}
                    onChange={(e) => liveSizingMode === "fixed" ? setLiveFixedStake(e.target.value) : setLiveStake(e.target.value)}
                    disabled={liveSizingMode === "adaptive" || liveSizingMode === "adaptive_seed"}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  Sizing Mode
                  <select
                    value={liveSizingMode}
                    onChange={(e) =>
                      setLiveSizingMode(e.target.value as "adaptive" | "adaptive_seed" | "fixed")
                    }
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive (balance)</option>
                    <option value="adaptive_seed">adaptive (seed capital)</option>
                  </select>
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
              {liveSizingMode === "adaptive" ? (
                <p className="text-xs text-muted-foreground">
                  Adaptive mode sizes entries proportionally from confidence/edge and account balance cap.
                </p>
              ) : liveSizingMode === "adaptive_seed" ? (
                <p className="text-xs text-muted-foreground">
                  Adaptive mode sizes entries proportionally from confidence/edge and Seed Capital amount.
                </p>
              ) : null}

              {liveSizingMode === "fixed" && !liveStakeValid ? (
                <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                  Invest amount must be a positive number.
                </p>
              ) : liveSizingMode === "fixed" && liveStakeOverBalance ? (
                <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                  Invest amount must be less than or equal to your collateral balance.
                </p>
              ) : null}

              {liveStatus?.account?.error ? (
                <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                  {liveStatus.account.error}
                </p>
              ) : null}
              {(liveStatus?.account?.warnings ?? []).length > 0 ? (
                <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                  {(liveStatus?.account?.warnings ?? []).join(" | ")}
                </p>
              ) : null}
              <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                Telegram: {liveTelegram?.enabled ? "enabled" : "disabled"} | token{" "}
                {liveTelegram?.has_token ? "set" : "missing"} | chat{" "}
                {liveTelegram?.has_chat_id ? "set" : "missing"}
              </p>

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
                  onClick={() => void refreshLiveStatus()}
                  className="rounded-md border border-border/70 bg-background/40 px-3 py-1.5 text-sm"
                >
                  Refresh
                </button>
                <button
                  onClick={() => {
                    setLiveHistoryOpen(true);
                    setLiveHistoryOffset(0);
                    void loadLiveTradeHistory({ limit: liveHistoryPageSize, offset: 0 });
                  }}
                  className="rounded-md border border-border/70 bg-background/40 px-3 py-1.5 text-sm"
                >
                  Trade History
                </button>
                <button
                  onClick={openAuthModal}
                  className="rounded-md border border-cyan-400/50 bg-cyan-500/20 px-3 py-1.5 text-sm"
                >
                  Private Key Edit
                </button>
                <button
                  onClick={openTelegramModal}
                  className="rounded-md border border-sky-400/50 bg-sky-500/20 px-3 py-1.5 text-sm"
                >
                  Telegram Bot
                </button>
                <Badge variant={liveStatus?.running ? "success" : "neutral"}>
                  {liveStatus?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>

              <p className="font-mono text-xs text-muted-foreground">
                pid={liveStatus?.pid ?? "-"} | exit={liveStatus?.exit_code ?? "-"} | sizing={liveSizingMode} | position={livePositionMode}
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

        {/* ---- BTC 15min: Paper Sim + Live Trading ---- */}
        <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle>Paper Sim - BTC 15min</CardTitle>
                  <CardDescription>Virtual trading for BTC 15-minute windows</CardDescription>
                </div>
                <button
                  onClick={() => {
                    setBtc15PaperHistoryOpen(true);
                    setBtc15PaperHistoryOffset(0);
                    void loadMarketPaperHistory("btc15", { limit: marketHistoryPageSize, offset: 0 });
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
                    value={btc15PaperStake}
                    onChange={(e) => setBtc15PaperStake(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs text-muted-foreground col-span-2">
                  Sizing Mode
                  <select
                    value={btc15PaperSizing}
                    onChange={(e) => setBtc15PaperSizing(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive</option>
                    <option value="all_in_fixed">all_in_fixed (always seed amount)</option>
                    <option value="all_in_equity">all_in_equity (all available equity)</option>
                  </select>
                </label>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() =>
                    void marketControlAction("btc15", "paper_start", {
                      stake: Number(btc15PaperStake || "100"),
                      sizing_mode: btc15PaperSizing,
                    })
                  }
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm"
                >
                  Start
                </button>
                <button
                  onClick={() => void marketControlAction("btc15", "paper_stop")}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={btc15Status?.paper?.running ? "success" : "neutral"}>
                  {btc15Status?.paper?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={btc15Status?.paper?.pid ?? "-"}
              </p>
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono">{formatUsd(btc15PaperSummary?.initial_capital ?? Number(btc15PaperStake || "100"))}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono">{formatUsd(btc15PaperSummary?.current_equity ?? Number(btc15PaperStake || "100"))}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Realized PnL</p>
                  <p className="font-mono">{formatUsd(btc15PaperSummary?.total_pnl ?? 0)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono">{formatPct(btc15PaperSummary?.equity_roi_pct ?? 0, 2)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">All-Loss Hits</p>
                  <p className="font-mono">{btc15PaperSummary?.bust_count ?? 0}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Max Drawdown</p>
                  <p className="font-mono">{formatPct(btc15PaperSummary?.max_drawdown_pct ?? 0, 2)}</p>
                </div>
              </div>
              <p className="text-xs text-muted-foreground italic">
                Signal Generator starts automatically with paper/live.
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(btc15Status?.paper?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>

          <Card className="xl:self-start">
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle>Live Trading - BTC 15min</CardTitle>
                  <CardDescription>Real execution for BTC 15-minute windows</CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    value={btc15SelectedAccountId}
                    onChange={(e) => setBtc15SelectedAccountId(Number(e.target.value))}
                    className="rounded-md border border-border/70 bg-background/40 px-2 py-1 text-sm"
                  >
                    <option value={0}>Main Account</option>
                    {accountList.map((a) => (
                      <option key={a.id} value={a.id}>{a.name || `Account ${a.id}`}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => {
                      setBtc15LiveHistoryOpen(true);
                      setBtc15LiveHistoryOffset(0);
                      void loadMarketLiveHistory("btc15", { limit: marketHistoryPageSize, offset: 0 });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Trade History
                  </button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Today&apos;s PnL</p>
                  <p className={`font-mono font-semibold ${(btc15LiveDaily?.daily_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    ${(btc15LiveDaily?.daily_pnl ?? 0).toFixed(2)} ({btc15LiveDaily?.daily_trades ?? 0} trades)
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-xs text-muted-foreground">
                  Fixed Invest Per Trade (USD)
                  <input
                    value={btc15LiveStake}
                    onChange={(e) => setBtc15LiveStake(e.target.value)}
                    disabled={btc15LiveSizing !== "fixed"}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  Sizing Mode
                  <select
                    value={btc15LiveSizing}
                    onChange={(e) => setBtc15LiveSizing(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive (balance)</option>
                    <option value="adaptive_seed">adaptive (seed capital)</option>
                  </select>
                </label>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => {
                    if (btc15SelectedAccountId > 0) {
                      void fetch("/api/accounts/start", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ account_id: btc15SelectedAccountId, market: "btc15" }),
                      });
                    } else {
                      void marketControlAction("btc15", "live_start", {
                        stake: Number(btc15LiveStake || "15"),
                        sizing_mode: btc15LiveSizing,
                        dry_run: false,
                      });
                    }
                  }}
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm"
                >
                  Start Live
                </button>
                <button
                  onClick={() => {
                    if (btc15SelectedAccountId > 0) {
                      void fetch("/api/accounts/stop", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ account_id: btc15SelectedAccountId, market: "btc15" }),
                      });
                    } else {
                      void marketControlAction("btc15", "live_stop");
                    }
                  }}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={btc15Status?.live?.running ? "success" : "neutral"}>
                  {btc15Status?.live?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={btc15Status?.live?.pid ?? "-"} | sizing={btc15LiveSizing}
                {btc15SelectedAccountId > 0 ? ` | account=${btc15SelectedAccountId}` : ""}
              </p>
              <p className="text-xs text-muted-foreground italic">
                {btc15SelectedAccountId > 0
                  ? `Using Account #${btc15SelectedAccountId} (${accountList.find((a) => a.id === btc15SelectedAccountId)?.name || ""})`
                  : "Uses Main Account API + Telegram settings."}
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(btc15Status?.live?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>
        </section>

        {/* ---- ETH 5min: Paper Sim + Live Trading ---- */}
        <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle>Paper Sim - ETH 5min</CardTitle>
                  <CardDescription>Virtual trading for ETH 5-minute windows</CardDescription>
                </div>
                <button
                  onClick={() => {
                    setEth5PaperHistoryOpen(true);
                    setEth5PaperHistoryOffset(0);
                    void loadMarketPaperHistory("eth5", { limit: marketHistoryPageSize, offset: 0 });
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
                    value={eth5PaperStake}
                    onChange={(e) => setEth5PaperStake(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs text-muted-foreground col-span-2">
                  Sizing Mode
                  <select
                    value={eth5PaperSizing}
                    onChange={(e) => setEth5PaperSizing(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive</option>
                    <option value="all_in_fixed">all_in_fixed (always seed amount)</option>
                    <option value="all_in_equity">all_in_equity (all available equity)</option>
                  </select>
                </label>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() =>
                    void marketControlAction("eth5", "paper_start", {
                      stake: Number(eth5PaperStake || "100"),
                      sizing_mode: eth5PaperSizing,
                    })
                  }
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm"
                >
                  Start
                </button>
                <button
                  onClick={() => void marketControlAction("eth5", "paper_stop")}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={eth5Status?.paper?.running ? "success" : "neutral"}>
                  {eth5Status?.paper?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={eth5Status?.paper?.pid ?? "-"}
              </p>
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono">{formatUsd(eth5PaperSummary?.initial_capital ?? Number(eth5PaperStake || "100"))}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono">{formatUsd(eth5PaperSummary?.current_equity ?? Number(eth5PaperStake || "100"))}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Realized PnL</p>
                  <p className="font-mono">{formatUsd(eth5PaperSummary?.total_pnl ?? 0)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono">{formatPct(eth5PaperSummary?.equity_roi_pct ?? 0, 2)}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">All-Loss Hits</p>
                  <p className="font-mono">{eth5PaperSummary?.bust_count ?? 0}</p>
                </div>
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Max Drawdown</p>
                  <p className="font-mono">{formatPct(eth5PaperSummary?.max_drawdown_pct ?? 0, 2)}</p>
                </div>
              </div>
              <p className="text-xs text-muted-foreground italic">
                Signal Generator starts automatically with paper/live.
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(eth5Status?.paper?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>

          <Card className="xl:self-start">
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle>Live Trading - ETH 5min</CardTitle>
                  <CardDescription>Real execution for ETH 5-minute windows</CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    value={eth5SelectedAccountId}
                    onChange={(e) => setEth5SelectedAccountId(Number(e.target.value))}
                    className="rounded-md border border-border/70 bg-background/40 px-2 py-1 text-sm"
                  >
                    <option value={0}>Main Account</option>
                    {accountList.map((a) => (
                      <option key={a.id} value={a.id}>{a.name || `Account ${a.id}`}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => {
                      setEth5LiveHistoryOpen(true);
                      setEth5LiveHistoryOffset(0);
                      void loadMarketLiveHistory("eth5", { limit: marketHistoryPageSize, offset: 0 });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Trade History
                  </button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <p className="text-muted-foreground">Today&apos;s PnL</p>
                  <p className={`font-mono font-semibold ${(eth5LiveDaily?.daily_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    ${(eth5LiveDaily?.daily_pnl ?? 0).toFixed(2)} ({eth5LiveDaily?.daily_trades ?? 0} trades)
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-xs text-muted-foreground">
                  Fixed Invest Per Trade (USD)
                  <input
                    value={eth5LiveStake}
                    onChange={(e) => setEth5LiveStake(e.target.value)}
                    disabled={eth5LiveSizing !== "fixed"}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  Sizing Mode
                  <select
                    value={eth5LiveSizing}
                    onChange={(e) => setEth5LiveSizing(e.target.value)}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  >
                    <option value="fixed">fixed (manual amount, mega 3x)</option>
                    <option value="adaptive">adaptive (balance)</option>
                    <option value="adaptive_seed">adaptive (seed capital)</option>
                  </select>
                </label>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => {
                    if (eth5SelectedAccountId > 0) {
                      void fetch("/api/accounts/start", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ account_id: eth5SelectedAccountId, market: "eth5" }),
                      });
                    } else {
                      void marketControlAction("eth5", "live_start", {
                        stake: Number(eth5LiveStake || "15"),
                        sizing_mode: eth5LiveSizing,
                        dry_run: false,
                      });
                    }
                  }}
                  className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm"
                >
                  Start Live
                </button>
                <button
                  onClick={() => {
                    if (eth5SelectedAccountId > 0) {
                      void fetch("/api/accounts/stop", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ account_id: eth5SelectedAccountId, market: "eth5" }),
                      });
                    } else {
                      void marketControlAction("eth5", "live_stop");
                    }
                  }}
                  className="rounded-md border border-rose-400/50 bg-rose-500/20 px-3 py-1.5 text-sm"
                >
                  Stop
                </button>
                <Badge variant={eth5Status?.live?.running ? "success" : "neutral"}>
                  {eth5Status?.live?.running ? "RUNNING" : "STOPPED"}
                </Badge>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                pid={eth5Status?.live?.pid ?? "-"} | sizing={eth5LiveSizing}
                {eth5SelectedAccountId > 0 ? ` | account=${eth5SelectedAccountId}` : ""}
              </p>
              <p className="text-xs text-muted-foreground italic">
                {eth5SelectedAccountId > 0
                  ? `Using Account #${eth5SelectedAccountId} (${accountList.find((a) => a.id === eth5SelectedAccountId)?.name || ""})`
                  : "Uses Main Account API + Telegram settings."}
              </p>
              <pre className="max-h-52 overflow-auto rounded-md border border-border/70 bg-background/30 p-2 font-mono text-[11px]">
                {(eth5Status?.live?.output_tail ?? []).slice(-20).join("\n") || "No logs yet"}
              </pre>
            </CardContent>
          </Card>
        </section>

        {/* ---- BTC 15min Paper Trade History Modal ---- */}
        {btc15PaperHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Paper Trade History - BTC 15min</p>
                  <p className="text-xs text-muted-foreground">
                    Entry, start price, odds at entry, to-win, and realized PnL.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() =>
                      void loadMarketPaperHistory("btc15", {
                        limit: marketHistoryPageSize,
                        offset: btc15PaperHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setBtc15PaperHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-8">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{btc15PaperHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{btc15PaperHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {btc15PaperHistorySummary?.wins ?? 0}/{btc15PaperHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((btc15PaperHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono text-sm">{formatUsd(btc15PaperHistorySummary?.initial_capital ?? Number(btc15PaperStake || "100"))}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono text-sm">{formatUsd(btc15PaperHistorySummary?.current_equity ?? Number(btc15PaperStake || "100"))}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono text-sm">{formatPct(btc15PaperHistorySummary?.equity_roi_pct ?? 0, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(btc15PaperHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {btc15PaperHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : btc15PaperHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No paper trades yet.</p>
                ) : (
                  btc15PaperHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Stake / Entry</p>
                          <p className="font-mono">{formatUsd(t.stake)} @ {formatNumber(t.entry_price, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">To Win</p>
                          <p className="font-mono">total {formatUsd(t.to_win_total)}</p>
                          <p className="font-mono text-muted-foreground">pnl {formatUsd(t.to_win_pnl)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">BTC Start/End</p>
                          <p className="font-mono">{formatNumber(t.window?.btc_start_price)} / {formatNumber(t.window?.btc_end_price)}</p>
                          <p className="font-mono text-muted-foreground">outcome {t.window?.actual_outcome ?? "--"}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">UP / DOWN at Entry</p>
                          <p className="font-mono">{formatNumber(t.odds_at_entry?.up_ask, 3)} / {formatNumber(t.odds_at_entry?.down_ask, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">fill {formatNumber(t.exit?.fill_px, 3)} | settle {formatNumber(t.exit?.settlement_px, 3)}</p>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-mono text-muted-foreground">
                          realized pnl {formatUsd(t.pnl)} ({formatPct(t.roi_pct, 2)})
                        </span>
                        <span className="font-mono text-muted-foreground">conf {formatNumber(t.signal_confidence, 3)}</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{t.signal_reason || "no reason"}</p>
                    </div>
                  ))
                )}
              </div>

              <div className="mt-3 flex items-center justify-between border-t border-border/50 pt-3 text-xs">
                <p className="text-muted-foreground">
                  total {btc15PaperHistoryTotal} | showing {btc15PaperHistoryTotal === 0 ? 0 : btc15PaperHistoryOffset + 1}-
                  {Math.min(btc15PaperHistoryOffset + btc15PaperHistory.length, btc15PaperHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={btc15PaperHistoryLoading || btc15PaperHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, btc15PaperHistoryOffset - marketHistoryPageSize);
                      setBtc15PaperHistoryOffset(nextOffset);
                      void loadMarketPaperHistory("btc15", { limit: marketHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={btc15PaperHistoryLoading || btc15PaperHistoryOffset + marketHistoryPageSize >= btc15PaperHistoryTotal}
                    onClick={() => {
                      const nextOffset = btc15PaperHistoryOffset + marketHistoryPageSize;
                      setBtc15PaperHistoryOffset(nextOffset);
                      void loadMarketPaperHistory("btc15", { limit: marketHistoryPageSize, offset: nextOffset });
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

        {/* ---- BTC 15min Live Trade History Modal ---- */}
        {btc15LiveHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Live Trade History - BTC 15min</p>
                  <p className="text-xs text-muted-foreground">
                    Filled entries and realized PnL from live execution.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() =>
                      void loadMarketLiveHistory("btc15", {
                        limit: marketHistoryPageSize,
                        offset: btc15LiveHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setBtc15LiveHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-7">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{btc15LiveHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{btc15LiveHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {btc15LiveHistorySummary?.wins ?? 0}/{btc15LiveHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((btc15LiveHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg Stake</p>
                  <p className="font-mono text-sm">{formatUsd(btc15LiveHistorySummary?.avg_stake)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg ROI</p>
                  <p className="font-mono text-sm">{formatPct(btc15LiveHistorySummary?.avg_roi_pct, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(btc15LiveHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {btc15LiveHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : btc15LiveHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No live trades yet.</p>
                ) : (
                  btc15LiveHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        {t.entry_source ? (
                          <Badge variant="neutral">{t.entry_source.toUpperCase()}</Badge>
                        ) : null}
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Stake / Entry</p>
                          <p className="font-mono">{formatUsd(t.stake)} @ {formatNumber(t.entry_price, 3)}</p>
                          <p className="font-mono text-muted-foreground">signal-side px {formatNumber(t.entry_side_price_at_signal, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">To Win</p>
                          <p className="font-mono">total {formatUsd(t.to_win_total)}</p>
                          <p className="font-mono text-muted-foreground">pnl {formatUsd(t.to_win_pnl)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">BTC Start/End</p>
                          <p className="font-mono">{formatNumber(t.window?.btc_start_price)} / {formatNumber(t.window?.btc_end_price)}</p>
                          <p className="font-mono text-muted-foreground">outcome {t.window?.actual_outcome ?? "--"}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">UP / DOWN at Entry</p>
                          <p className="font-mono">{formatNumber(t.odds_at_entry?.up_ask, 3)} / {formatNumber(t.odds_at_entry?.down_ask, 3)}</p>
                          <p className="font-mono text-muted-foreground">mid {formatNumber(t.odds_at_entry?.up_mid, 3)} / {formatNumber(t.odds_at_entry?.down_mid, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">fill {formatNumber(t.exit?.fill_px, 3)} | mkt {formatNumber(t.exit?.market_px, 3)}</p>
                          <p className="font-mono text-muted-foreground">settle {formatNumber(t.exit?.settlement_px, 3)} | {t.exit?.kind ?? "--"}</p>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-mono text-muted-foreground">
                          realized pnl {formatUsd(t.pnl)} ({formatPct(t.roi_pct, 2)})
                        </span>
                        <span className="font-mono text-muted-foreground">conf {formatNumber(t.signal_confidence, 3)}</span>
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
                  total {btc15LiveHistoryTotal} | showing {btc15LiveHistoryTotal === 0 ? 0 : btc15LiveHistoryOffset + 1}-
                  {Math.min(btc15LiveHistoryOffset + btc15LiveHistory.length, btc15LiveHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={btc15LiveHistoryLoading || btc15LiveHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, btc15LiveHistoryOffset - marketHistoryPageSize);
                      setBtc15LiveHistoryOffset(nextOffset);
                      void loadMarketLiveHistory("btc15", { limit: marketHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={btc15LiveHistoryLoading || btc15LiveHistoryOffset + marketHistoryPageSize >= btc15LiveHistoryTotal}
                    onClick={() => {
                      const nextOffset = btc15LiveHistoryOffset + marketHistoryPageSize;
                      setBtc15LiveHistoryOffset(nextOffset);
                      void loadMarketLiveHistory("btc15", { limit: marketHistoryPageSize, offset: nextOffset });
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

        {/* ---- ETH 5min Paper Trade History Modal ---- */}
        {eth5PaperHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Paper Trade History - ETH 5min</p>
                  <p className="text-xs text-muted-foreground">
                    Entry, start price, odds at entry, to-win, and realized PnL.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() =>
                      void loadMarketPaperHistory("eth5", {
                        limit: marketHistoryPageSize,
                        offset: eth5PaperHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setEth5PaperHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-8">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{eth5PaperHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{eth5PaperHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {eth5PaperHistorySummary?.wins ?? 0}/{eth5PaperHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((eth5PaperHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Seed Capital</p>
                  <p className="font-mono text-sm">{formatUsd(eth5PaperHistorySummary?.initial_capital ?? Number(eth5PaperStake || "100"))}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Equity</p>
                  <p className="font-mono text-sm">{formatUsd(eth5PaperHistorySummary?.current_equity ?? Number(eth5PaperStake || "100"))}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Account Return</p>
                  <p className="font-mono text-sm">{formatPct(eth5PaperHistorySummary?.equity_roi_pct ?? 0, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(eth5PaperHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {eth5PaperHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : eth5PaperHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No paper trades yet.</p>
                ) : (
                  eth5PaperHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Stake / Entry</p>
                          <p className="font-mono">{formatUsd(t.stake)} @ {formatNumber(t.entry_price, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">To Win</p>
                          <p className="font-mono">total {formatUsd(t.to_win_total)}</p>
                          <p className="font-mono text-muted-foreground">pnl {formatUsd(t.to_win_pnl)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">ETH Start/End</p>
                          <p className="font-mono">{formatNumber(t.window?.btc_start_price)} / {formatNumber(t.window?.btc_end_price)}</p>
                          <p className="font-mono text-muted-foreground">outcome {t.window?.actual_outcome ?? "--"}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">UP / DOWN at Entry</p>
                          <p className="font-mono">{formatNumber(t.odds_at_entry?.up_ask, 3)} / {formatNumber(t.odds_at_entry?.down_ask, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">fill {formatNumber(t.exit?.fill_px, 3)} | settle {formatNumber(t.exit?.settlement_px, 3)}</p>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-mono text-muted-foreground">
                          realized pnl {formatUsd(t.pnl)} ({formatPct(t.roi_pct, 2)})
                        </span>
                        <span className="font-mono text-muted-foreground">conf {formatNumber(t.signal_confidence, 3)}</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{t.signal_reason || "no reason"}</p>
                    </div>
                  ))
                )}
              </div>

              <div className="mt-3 flex items-center justify-between border-t border-border/50 pt-3 text-xs">
                <p className="text-muted-foreground">
                  total {eth5PaperHistoryTotal} | showing {eth5PaperHistoryTotal === 0 ? 0 : eth5PaperHistoryOffset + 1}-
                  {Math.min(eth5PaperHistoryOffset + eth5PaperHistory.length, eth5PaperHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={eth5PaperHistoryLoading || eth5PaperHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, eth5PaperHistoryOffset - marketHistoryPageSize);
                      setEth5PaperHistoryOffset(nextOffset);
                      void loadMarketPaperHistory("eth5", { limit: marketHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={eth5PaperHistoryLoading || eth5PaperHistoryOffset + marketHistoryPageSize >= eth5PaperHistoryTotal}
                    onClick={() => {
                      const nextOffset = eth5PaperHistoryOffset + marketHistoryPageSize;
                      setEth5PaperHistoryOffset(nextOffset);
                      void loadMarketPaperHistory("eth5", { limit: marketHistoryPageSize, offset: nextOffset });
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

        {/* ---- ETH 5min Live Trade History Modal ---- */}
        {eth5LiveHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Live Trade History - ETH 5min</p>
                  <p className="text-xs text-muted-foreground">
                    Filled entries and realized PnL from live execution.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() =>
                      void loadMarketLiveHistory("eth5", {
                        limit: marketHistoryPageSize,
                        offset: eth5LiveHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setEth5LiveHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-7">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{eth5LiveHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{eth5LiveHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {eth5LiveHistorySummary?.wins ?? 0}/{eth5LiveHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((eth5LiveHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg Stake</p>
                  <p className="font-mono text-sm">{formatUsd(eth5LiveHistorySummary?.avg_stake)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg ROI</p>
                  <p className="font-mono text-sm">{formatPct(eth5LiveHistorySummary?.avg_roi_pct, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(eth5LiveHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {eth5LiveHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : eth5LiveHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No live trades yet.</p>
                ) : (
                  eth5LiveHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        {t.entry_source ? (
                          <Badge variant="neutral">{t.entry_source.toUpperCase()}</Badge>
                        ) : null}
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Stake / Entry</p>
                          <p className="font-mono">{formatUsd(t.stake)} @ {formatNumber(t.entry_price, 3)}</p>
                          <p className="font-mono text-muted-foreground">signal-side px {formatNumber(t.entry_side_price_at_signal, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">To Win</p>
                          <p className="font-mono">total {formatUsd(t.to_win_total)}</p>
                          <p className="font-mono text-muted-foreground">pnl {formatUsd(t.to_win_pnl)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">ETH Start/End</p>
                          <p className="font-mono">{formatNumber(t.window?.btc_start_price)} / {formatNumber(t.window?.btc_end_price)}</p>
                          <p className="font-mono text-muted-foreground">outcome {t.window?.actual_outcome ?? "--"}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">UP / DOWN at Entry</p>
                          <p className="font-mono">{formatNumber(t.odds_at_entry?.up_ask, 3)} / {formatNumber(t.odds_at_entry?.down_ask, 3)}</p>
                          <p className="font-mono text-muted-foreground">mid {formatNumber(t.odds_at_entry?.up_mid, 3)} / {formatNumber(t.odds_at_entry?.down_mid, 3)}</p>
                        </div>
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">fill {formatNumber(t.exit?.fill_px, 3)} | mkt {formatNumber(t.exit?.market_px, 3)}</p>
                          <p className="font-mono text-muted-foreground">settle {formatNumber(t.exit?.settlement_px, 3)} | {t.exit?.kind ?? "--"}</p>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-mono text-muted-foreground">
                          realized pnl {formatUsd(t.pnl)} ({formatPct(t.roi_pct, 2)})
                        </span>
                        <span className="font-mono text-muted-foreground">conf {formatNumber(t.signal_confidence, 3)}</span>
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
                  total {eth5LiveHistoryTotal} | showing {eth5LiveHistoryTotal === 0 ? 0 : eth5LiveHistoryOffset + 1}-
                  {Math.min(eth5LiveHistoryOffset + eth5LiveHistory.length, eth5LiveHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={eth5LiveHistoryLoading || eth5LiveHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, eth5LiveHistoryOffset - marketHistoryPageSize);
                      setEth5LiveHistoryOffset(nextOffset);
                      void loadMarketLiveHistory("eth5", { limit: marketHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={eth5LiveHistoryLoading || eth5LiveHistoryOffset + marketHistoryPageSize >= eth5LiveHistoryTotal}
                    onClick={() => {
                      const nextOffset = eth5LiveHistoryOffset + marketHistoryPageSize;
                      setEth5LiveHistoryOffset(nextOffset);
                      void loadMarketLiveHistory("eth5", { limit: marketHistoryPageSize, offset: nextOffset });
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

        {authModalOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Polymarket Auth Setup</p>
                  <p className="text-xs text-muted-foreground">
                    Save private key/funder to local .env.secrets, derive API creds, no server restart.
                  </p>
                </div>
                <button
                  onClick={() => {
                    if (!authSaving) setAuthModalOpen(false);
                  }}
                  className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                >
                  Close
                </button>
              </div>

              <div className="space-y-3">
                {!authEditEnabled ? (
                  <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-200">
                    수정하면 기존 API Key / Secret / Passphrase가 지워지고 새 값으로 재생성됩니다.
                  </div>
                ) : null}

                <label className="block text-xs text-muted-foreground">
                  Private Key (required for first-time setup)
                  <input
                    type="password"
                    value={authPrivateKey}
                    onChange={(e) => setAuthPrivateKey(e.target.value)}
                    placeholder={
                      authEditEnabled
                        ? "Leave blank to keep existing key"
                        : "Click 'Enable Edit' first"
                    }
                    disabled={!authEditEnabled}
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 font-mono text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <label className="text-xs text-muted-foreground">
                    Funder Address (optional)
                    <input
                      value={authFunder}
                      onChange={(e) => setAuthFunder(e.target.value)}
                      placeholder="0x... (Polymarket shown wallet)"
                      disabled={!authEditEnabled}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 font-mono text-sm disabled:cursor-not-allowed disabled:opacity-60"
                    />
                  </label>
                  <label className="text-xs text-muted-foreground">
                    Signature Type
                    <select
                      value={authSignatureType}
                      onChange={(e) => setAuthSignatureType(e.target.value)}
                      disabled={!authEditEnabled}
                      className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <option value="-1">auto (-1)</option>
                      <option value="0">EOA (0)</option>
                      <option value="1">POLY_PROXY (1)</option>
                      <option value="2">POLY_GNOSIS_SAFE (2)</option>
                    </select>
                  </label>
                </div>

                <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                  Save updates <span className="font-mono">.env.secrets</span>. Trading auth uses private-key derived
                  API creds in <span className="font-mono">.env.polymarket.generated</span>.
                </p>

                <div className="rounded-md border border-border/60 bg-background/30 p-2 text-xs">
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <p className="text-muted-foreground">
                      Trading API Credentials ({liveApiCreds?.source ?? "none"})
                    </p>
                    <button
                      onClick={() => setAuthShowSecrets((prev) => !prev)}
                      className="rounded border border-border/70 px-2 py-0.5 text-[11px]"
                    >
                      {authShowSecrets ? "Hide" : "Show"}
                    </button>
                  </div>
                  {hasLiveApiCreds ? (
                    <div className="space-y-1 font-mono text-[11px]">
                      <p>apiKey: {authShowSecrets ? liveApiCreds?.api_key : maskSecret(liveApiCreds?.api_key)}</p>
                      <p>secret: {authShowSecrets ? liveApiCreds?.api_secret : maskSecret(liveApiCreds?.api_secret)}</p>
                      <p>
                        passphrase:{" "}
                        {authShowSecrets ? liveApiCreds?.api_passphrase : maskSecret(liveApiCreds?.api_passphrase)}
                      </p>
                    </div>
                  ) : (
                    <p className="text-muted-foreground">No derived credentials yet.</p>
                  )}
                </div>

                {authError ? (
                  <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                    {authError}
                  </p>
                ) : null}
                {liveStatus?.message ? (
                  <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                    {liveStatus.message}
                  </p>
                ) : null}

                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => void saveLiveAuth()}
                    disabled={authSaving || !authEditEnabled}
                    className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    {authSaving ? "Saving..." : "Save Auth"}
                  </button>
                  <button
                    onClick={enableAuthEdit}
                    disabled={authSaving || authEditEnabled}
                    className="rounded-md border border-amber-400/50 bg-amber-500/20 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    Enable Edit
                  </button>
                  <button
                    onClick={() => void refreshLiveStatus()}
                    disabled={authSaving}
                    className="rounded-md border border-border/70 bg-background/40 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    Refresh Status
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {telegramModalOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Telegram Bot Setup</p>
                  <p className="text-xs text-muted-foreground">
                    Live OPEN/CLOSED fill alerts. Save to local <span className="font-mono">.env.secrets</span>.
                  </p>
                </div>
                <button
                  onClick={() => {
                    if (!telegramSaving) setTelegramModalOpen(false);
                  }}
                  className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                >
                  Close
                </button>
              </div>

              <div className="space-y-3">
                <div className="rounded-md border border-sky-500/40 bg-sky-500/10 px-2 py-1.5 text-xs text-sky-100">
                  <p>1) Bot token 입력 후 저장</p>
                  <p>2) Telegram에서 봇 채팅 열고 <span className="font-mono">/start</span> 전송</p>
                  <p>3) <span className="font-medium">Verify /start + Send Test</span> 클릭</p>
                  <p>성공하면 이 창은 자동으로 닫힙니다.</p>
                </div>

                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={telegramEnabled}
                    onChange={(e) => setTelegramEnabled(e.target.checked)}
                    className="h-4 w-4 rounded border-border/70 bg-background/40"
                  />
                  Enable Telegram notifications
                </label>

                <label className="block text-xs text-muted-foreground">
                  Bot Token
                  <input
                    type="password"
                    value={telegramBotToken}
                    onChange={(e) => setTelegramBotToken(e.target.value)}
                    placeholder="Leave blank to keep existing token"
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 font-mono text-sm"
                  />
                </label>

                <label className="block text-xs text-muted-foreground">
                  Chat ID
                  <input
                    value={telegramChatId}
                    onChange={(e) => setTelegramChatId(e.target.value)}
                    placeholder="e.g. 123456789 (blank -> try auto-detect via getUpdates)"
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 font-mono text-sm"
                  />
                </label>

                <label className="block text-xs text-muted-foreground">
                  Test Message (optional)
                  <input
                    value={telegramTestMessage}
                    onChange={(e) => setTelegramTestMessage(e.target.value)}
                    placeholder="Default test message will be used if empty"
                    className="mt-1 w-full rounded-md border border-border/70 bg-background/40 px-2 py-1.5 text-sm"
                  />
                </label>

                <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                  Current: token {liveTelegram?.token_masked ?? "--"} | chat{" "}
                  {liveTelegram?.chat_id ?? "--"} | configured{" "}
                  {liveTelegram?.configured ? "yes" : "no"}
                </p>

                {telegramError ? (
                  <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-200">
                    {telegramError}
                  </p>
                ) : null}
                {liveStatus?.message ? (
                  <p className="rounded-md border border-border/60 bg-background/30 px-2 py-1 text-xs text-muted-foreground">
                    {liveStatus.message}
                  </p>
                ) : null}

                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => void saveTelegramConfig(false)}
                    disabled={telegramSaving}
                    className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    {telegramSaving ? "Saving..." : "Save Telegram"}
                  </button>
                  <button
                    onClick={() => void saveTelegramConfig(true)}
                    disabled={telegramSaving}
                    className="rounded-md border border-sky-400/50 bg-sky-500/20 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    Verify /start + Send Test
                  </button>
                  <button
                    onClick={() => void sendTelegramTestOnly()}
                    disabled={telegramSaving}
                    className="rounded-md border border-border/70 bg-background/40 px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    Send Test Only
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {liveHistoryOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
            <div className="w-full max-w-5xl rounded-xl border border-border/80 bg-slate-950 p-4 shadow-2xl">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <p className="text-lg font-semibold">Live Trade History</p>
                  <p className="text-xs text-muted-foreground">
                    Filled entries, 5m market context, and realized PnL from live execution.
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() =>
                      void loadLiveTradeHistory({
                        limit: liveHistoryPageSize,
                        offset: liveHistoryOffset,
                      })
                    }
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 text-xs"
                  >
                    Refresh
                  </button>
                  <button
                    onClick={() => setLiveHistoryOpen(false)}
                    className="rounded-md border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 text-xs"
                  >
                    Close
                  </button>
                </div>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-7">
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Open</p>
                  <p className="font-mono text-sm">{liveHistorySummary?.open ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Closed</p>
                  <p className="font-mono text-sm">{liveHistorySummary?.closed ?? 0}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Wins/Losses</p>
                  <p className="font-mono text-sm">
                    {liveHistorySummary?.wins ?? 0}/{liveHistorySummary?.losses ?? 0}
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Win Rate</p>
                  <p className="font-mono text-sm">{formatPct((liveHistorySummary?.win_rate ?? 0) * 100, 1)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg Stake</p>
                  <p className="font-mono text-sm">{formatUsd(liveHistorySummary?.avg_stake)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Avg ROI</p>
                  <p className="font-mono text-sm">{formatPct(liveHistorySummary?.avg_roi_pct, 2)}</p>
                </div>
                <div className="rounded-lg border border-border/60 bg-background/40 p-2 text-xs">
                  <p className="text-muted-foreground">Realized Total PnL</p>
                  <p className="font-mono text-sm">{formatUsd(liveHistorySummary?.total_pnl)}</p>
                </div>
              </div>

              <div className="max-h-[62vh] space-y-2 overflow-auto pr-1">
                {liveHistoryLoading ? (
                  <p className="text-sm text-muted-foreground">Loading...</p>
                ) : liveHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No live trades yet.</p>
                ) : (
                  liveHistory.map((t) => (
                    <div key={`${t.id}-${t.window_start}`} className="rounded-lg border border-border/60 bg-background/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant={t.status === "OPEN" ? "neutral" : t.won === 1 ? "success" : "danger"}>
                          {t.status === "OPEN" ? "OPEN" : t.won === 1 ? "WIN" : "LOSS"}
                        </Badge>
                        <Badge variant={t.direction === "UP" ? "success" : t.direction === "DOWN" ? "danger" : "neutral"}>
                          {t.direction}
                        </Badge>
                        {t.entry_source ? (
                          <Badge variant="neutral">{t.entry_source.toUpperCase()}</Badge>
                        ) : null}
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>

                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
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
                          <p className="text-muted-foreground">5m BTC Start/End (Binance)</p>
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
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">
                            fill {formatNumber(t.exit?.fill_px, 3)} | mkt {formatNumber(t.exit?.market_px, 3)}
                          </p>
                          <p className="font-mono text-muted-foreground">
                            settle {formatNumber(t.exit?.settlement_px, 3)} | {t.exit?.kind ?? "--"}
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
                  total {liveHistoryTotal} | showing {liveHistoryTotal === 0 ? 0 : liveHistoryOffset + 1}-
                  {Math.min(liveHistoryOffset + liveHistory.length, liveHistoryTotal)}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={liveHistoryLoading || liveHistoryOffset <= 0}
                    onClick={() => {
                      const nextOffset = Math.max(0, liveHistoryOffset - liveHistoryPageSize);
                      setLiveHistoryOffset(nextOffset);
                      void loadLiveTradeHistory({ limit: liveHistoryPageSize, offset: nextOffset });
                    }}
                    className="rounded-md border border-border/70 bg-background/40 px-2.5 py-1 disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    disabled={liveHistoryLoading || liveHistoryOffset + liveHistoryPageSize >= liveHistoryTotal}
                    onClick={() => {
                      const nextOffset = liveHistoryOffset + liveHistoryPageSize;
                      setLiveHistoryOffset(nextOffset);
                      void loadLiveTradeHistory({ limit: liveHistoryPageSize, offset: nextOffset });
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
                        <span className="font-mono text-muted-foreground">{toKST(t.opened_at_utc)}</span>
                        <span className="truncate font-mono text-muted-foreground">{t.window?.slug ?? "--"}</span>
                      </div>

                      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
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
                          <p className="text-muted-foreground">5m BTC Start/End (Binance)</p>
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
                        <div className="rounded-md border border-border/60 bg-background/40 p-2 text-xs">
                          <p className="text-muted-foreground">Exit / Settle</p>
                          <p className="font-mono">
                            fill {formatNumber(t.exit?.fill_px, 3)} | mkt {formatNumber(t.exit?.market_px, 3)}
                          </p>
                          <p className="font-mono text-muted-foreground">
                            settle {formatNumber(t.exit?.settlement_px, 3)} | {t.exit?.kind ?? "--"}
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
                        <span className="font-mono text-muted-foreground">{toKST(item.ts_utc)}</span>
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
          <span>Auto refresh: snapshot 1s / history 6s / control logs 2-10s</span>
        </footer>
      </div>
    </main>
  );
}


