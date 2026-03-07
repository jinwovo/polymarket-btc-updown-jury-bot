import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";
const HISTORY_TTL_MS = 3000;

type SignalHistoryCache = {
  ts: number;
  payloadByLimit: Map<string, unknown>;
  inFlightByLimit: Map<string, Promise<unknown>>;
};

const globalCache = globalThis as typeof globalThis & {
  __fp_signal_history_cache?: SignalHistoryCache;
};

function cacheState(): SignalHistoryCache {
  if (!globalCache.__fp_signal_history_cache) {
    globalCache.__fp_signal_history_cache = {
      ts: 0,
      payloadByLimit: new Map(),
      inFlightByLimit: new Map(),
    };
  }
  return globalCache.__fp_signal_history_cache;
}

export async function GET(request: NextRequest) {
  const state = cacheState();
  const limit = request.nextUrl.searchParams.get("limit") || "40";
  const now = Date.now();
  const cached = state.payloadByLimit.get(limit);

  if (cached && now - state.ts < HISTORY_TTL_MS) {
    return NextResponse.json(cached);
  }

  const inFlight = state.inFlightByLimit.get(limit);
  if (inFlight) {
    const payload = await inFlight;
    return NextResponse.json(payload);
  }

  try {
    const req = (async () => {
      const response = await fetch(
        `${PY_API_BASE}/api/signal-history?limit=${encodeURIComponent(limit)}`,
        { cache: "no-store" },
      );
      const payload = await response.json();
      state.payloadByLimit.set(limit, payload);
      state.ts = Date.now();
      return payload;
    })();

    state.inFlightByLimit.set(limit, req);
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
    state.inFlightByLimit.delete(limit);
  }
}
