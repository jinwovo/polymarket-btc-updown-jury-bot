import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";

const ROUTE_MAP: Record<string, string> = {
  signal_start: "/api/control/btc15/signal/start",
  signal_stop: "/api/control/btc15/signal/stop",
  paper_start: "/api/control/btc15/paper/start",
  paper_stop: "/api/control/btc15/paper/stop",
  live_start: "/api/control/btc15/live/start",
  live_stop: "/api/control/btc15/live/stop",
};

export async function GET() {
  try {
    const [signal, paper, live] = await Promise.all([
      fetch(`${PY_API_BASE}/api/control/btc15/signal`, { cache: "no-store" }).then((r) => r.json()).catch(() => ({ ok: false, running: false })),
      fetch(`${PY_API_BASE}/api/control/btc15/paper`, { cache: "no-store" }).then((r) => r.json()).catch(() => ({ ok: false, running: false })),
      fetch(`${PY_API_BASE}/api/control/btc15/live`, { cache: "no-store" }).then((r) => r.json()).catch(() => ({ ok: false, running: false })),
    ]);
    return NextResponse.json({ ok: true, signal, paper, live });
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
  }
}

export async function POST(request: NextRequest) {
  let body: Record<string, unknown> = {};
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch (_) {
    body = {};
  }

  const action = String(body.action ?? "").toLowerCase();
  const endpoint = ROUTE_MAP[action];

  if (!endpoint) {
    return NextResponse.json(
      { ok: false, error: `Unknown action: ${action}. Valid: ${Object.keys(ROUTE_MAP).join(", ")}` },
      { status: 400 },
    );
  }

  try {
    const response = await fetch(`${PY_API_BASE}${endpoint}`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    return NextResponse.json(payload, { status: response.status });
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
  }
}
