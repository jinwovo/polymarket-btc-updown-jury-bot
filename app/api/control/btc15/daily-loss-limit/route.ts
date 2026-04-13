import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const response = await fetch(`${PY_API_BASE}/api/control/btc15/daily-loss-limit`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    return NextResponse.json(payload, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { ok: false, error: `Failed to reach Python API: ${error instanceof Error ? error.message : String(error)}` },
      { status: 502 },
    );
  }
}
