import { NextRequest, NextResponse } from "next/server";

const PY_API_BASE = process.env.PY_DASHBOARD_URL || "http://127.0.0.1:8790";

// GET: list all accounts
export async function GET() {
  try {
    const res = await fetch(`${PY_API_BASE}/api/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
      cache: "no-store",
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (error) {
    return NextResponse.json({ ok: false, error: String(error) }, { status: 502 });
  }
}

// POST: save/start/stop/delete/status
export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const action = String(body.action || "save");

    const endpoints: Record<string, string> = {
      save: "/api/accounts/save",
      start: "/api/accounts/start",
      stop: "/api/accounts/stop",
      status: "/api/accounts/status",
      delete: "/api/accounts/delete",
    };
    const endpoint = endpoints[action] || "/api/accounts/save";

    const res = await fetch(`${PY_API_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (error) {
    return NextResponse.json({ ok: false, error: String(error) }, { status: 502 });
  }
}
