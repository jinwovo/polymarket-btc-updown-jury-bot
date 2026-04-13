import { NextResponse } from "next/server";
const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";
export async function GET() {
  try {
    const res = await fetch(`${PY_API_BASE}/api/eth5/live-pnl`, { cache: "no-store" });
    return NextResponse.json(await res.json(), { status: 200 });
  } catch (error) {
    return NextResponse.json({ ok: false, error: String(error) }, { status: 502 });
  }
}
