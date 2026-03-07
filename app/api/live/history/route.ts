import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";
const HISTORY_TTL_MS = 3000;

type HistoryCache = {
  ts: number;
  payloadByMinutes: Map<string, unknown>;
  inFlightByMinutes: Map<string, Promise<unknown>>;
};

const globalCache = globalThis as typeof globalThis & {
  __fp_history_cache?: HistoryCache;
};

function cacheState(): HistoryCache {
  if (!globalCache.__fp_history_cache) {
    globalCache.__fp_history_cache = {
      ts: 0,
      payloadByMinutes: new Map(),
      inFlightByMinutes: new Map(),
    };
  }
  return globalCache.__fp_history_cache;
}

export async function GET(request: NextRequest) {
  const state = cacheState();
  const minutes = request.nextUrl.searchParams.get("minutes") || "30";
  const now = Date.now();
  const cached = state.payloadByMinutes.get(minutes);

  if (cached && now - state.ts < HISTORY_TTL_MS) {
    return NextResponse.json(cached);
  }

  const inFlight = state.inFlightByMinutes.get(minutes);
  if (inFlight) {
    const payload = await inFlight;
    return NextResponse.json(payload);
  }

  try {
    const req = (async () => {
      const response = await fetch(
        `${PY_API_BASE}/api/history?minutes=${encodeURIComponent(minutes)}`,
        {
          cache: "no-store",
        },
      );
      const payload = await response.json();
      state.payloadByMinutes.set(minutes, payload);
      state.ts = Date.now();
      return payload;
    })();
    state.inFlightByMinutes.set(minutes, req);
    const payload = await req;

    return NextResponse.json(payload, {
      status: 200,
    });
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
    state.inFlightByMinutes.delete(minutes);
  }
}
