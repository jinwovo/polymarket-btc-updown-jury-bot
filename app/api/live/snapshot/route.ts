import { NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";
const SNAPSHOT_TTL_MS = 250;

type SnapshotCache = {
  ts: number;
  payload: unknown | null;
  inFlight: Promise<unknown> | null;
};

const globalCache = globalThis as typeof globalThis & {
  __fp_snapshot_cache?: SnapshotCache;
};

function cacheState(): SnapshotCache {
  if (!globalCache.__fp_snapshot_cache) {
    globalCache.__fp_snapshot_cache = { ts: 0, payload: null, inFlight: null };
  }
  return globalCache.__fp_snapshot_cache;
}

export async function GET() {
  const state = cacheState();
  const now = Date.now();
  if (state.payload && now - state.ts < SNAPSHOT_TTL_MS) {
    return NextResponse.json(state.payload);
  }

  if (state.inFlight) {
    const payload = await state.inFlight;
    return NextResponse.json(payload);
  }

  try {
    state.inFlight = (async () => {
      const response = await fetch(`${PY_API_BASE}/api/snapshot`, {
        cache: "no-store",
      });
      const payload = await response.json();
      state.payload = payload;
      state.ts = Date.now();
      return payload;
    })();
    const payload = await state.inFlight;

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
    state.inFlight = null;
  }
}
