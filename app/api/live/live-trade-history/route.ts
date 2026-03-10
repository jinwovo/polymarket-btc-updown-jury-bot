import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";
const HISTORY_TTL_MS = 2000;

type LiveHistoryCache = {
  ts: number;
  payloadByKey: Map<string, unknown>;
  inFlightByKey: Map<string, Promise<unknown>>;
};

const globalCache = globalThis as typeof globalThis & {
  __fp_live_trade_history_cache?: LiveHistoryCache;
};

function cacheState(): LiveHistoryCache {
  if (!globalCache.__fp_live_trade_history_cache) {
    globalCache.__fp_live_trade_history_cache = {
      ts: 0,
      payloadByKey: new Map(),
      inFlightByKey: new Map(),
    };
  }
  return globalCache.__fp_live_trade_history_cache;
}

export async function GET(request: NextRequest) {
  const state = cacheState();
  const limit = request.nextUrl.searchParams.get("limit") || "20";
  const offset = request.nextUrl.searchParams.get("offset") || "0";
  const key = `${limit}|${offset}`;
  const now = Date.now();
  const cached = state.payloadByKey.get(key);

  if (cached && now - state.ts < HISTORY_TTL_MS) {
    return NextResponse.json(cached);
  }

  const inFlight = state.inFlightByKey.get(key);
  if (inFlight) {
    const payload = await inFlight;
    return NextResponse.json(payload);
  }

  try {
    const req = (async () => {
      const response = await fetch(
        `${PY_API_BASE}/api/live-trade-history?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
        { cache: "no-store" },
      );
      const payload = await response.json();
      state.payloadByKey.set(key, payload);
      state.ts = Date.now();
      return payload;
    })();

    state.inFlightByKey.set(key, req);
    const payload = await req;
    return NextResponse.json(payload, { status: 200 });
  } catch (error) {
    return NextResponse.json(
      {
        ok: false,
        error: `Failed to reach Python API (${PY_API_BASE}): ${
          error instanceof Error ? error.message : String(error)
        }`,
      },
      { status: 502 },
    );
  } finally {
    state.inFlightByKey.delete(key);
  }
}
