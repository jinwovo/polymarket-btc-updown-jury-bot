import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";

export async function GET() {
  try {
    const response = await fetch(`${PY_API_BASE}/api/control/live`, {
      cache: "no-store",
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

export async function POST(request: NextRequest) {
  let body: Record<string, unknown> = {};
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch (_) {
    body = {};
  }

  const action = String(body.action ?? "start").toLowerCase();
  const endpoint =
    action === "stop"
      ? "/api/control/live/stop"
      : action === "claim_now"
        ? "/api/control/live/claim"
      : action === "auth_config"
        ? "/api/control/live/auth-config"
        : "/api/control/live/start";

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
