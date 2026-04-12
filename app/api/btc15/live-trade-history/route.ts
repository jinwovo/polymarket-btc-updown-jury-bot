import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";

export async function GET(request: NextRequest) {
  const limit = request.nextUrl.searchParams.get("limit") || "20";
  const offset = request.nextUrl.searchParams.get("offset") || "0";
  try {
    const response = await fetch(
      `${PY_API_BASE}/api/btc15/live-trade-history?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
      { cache: "no-store" },
    );
    const payload = await response.json();
    return NextResponse.json(payload, { status: 200 });
  } catch (error) {
    return NextResponse.json(
      { ok: false, error: `Failed to reach Python API: ${error instanceof Error ? error.message : String(error)}` },
      { status: 502 },
    );
  }
}
