"use client";
import { useState, useEffect, useCallback } from "react";

interface Account {
  id: number;
  name: string;
  enabled: number;
  private_key?: string;
  api_key?: string;
  api_secret?: string;
  api_passphrase?: string;
  funder?: string;
  telegram_token?: string;
  telegram_chat_id?: string;
  seed_capital: number;
  fixed_stake: number;
  sizing_mode: string;
  position_mode: string;
  daily_loss_limit: number;
  mega_multiplier: number;
  mega_min_score: number;
  min_entry_score: number;
  status: string;
  pid?: number;
}

interface AccountStatus {
  account: Account;
  running: boolean;
  pid: number | null;
  today_pnl: number;
  today_trades: number;
  log_lines: string[];
}

export default function AccountManager() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [statuses, setStatuses] = useState<Record<number, AccountStatus>>({});
  const [editing, setEditing] = useState<Partial<Account> | null>(null);
  const [showKeys, setShowKeys] = useState(false);

  const fetchAccounts = useCallback(async () => {
    try {
      const res = await fetch("/api/accounts");
      const data = await res.json();
      if (data.accounts) setAccounts(data.accounts);
    } catch {}
  }, []);

  const fetchStatus = useCallback(async (id: number) => {
    try {
      const res = await fetch("/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "status", account_id: id }),
      });
      const data = await res.json();
      setStatuses((prev) => ({ ...prev, [id]: data }));
    } catch {}
  }, []);

  useEffect(() => {
    fetchAccounts();
    const interval = setInterval(fetchAccounts, 10000);
    return () => clearInterval(interval);
  }, [fetchAccounts]);

  useEffect(() => {
    accounts.forEach((a) => fetchStatus(a.id));
    const interval = setInterval(() => {
      accounts.forEach((a) => fetchStatus(a.id));
    }, 5000);
    return () => clearInterval(interval);
  }, [accounts, fetchStatus]);

  const saveAccount = async () => {
    if (!editing) return;
    await fetch("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "save", ...editing }),
    });
    setEditing(null);
    fetchAccounts();
  };

  const startAccount = async (id: number) => {
    await fetch("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "start", account_id: id }),
    });
    setTimeout(() => { fetchAccounts(); fetchStatus(id); }, 1000);
  };

  const stopAccount = async (id: number) => {
    await fetch("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "stop", account_id: id }),
    });
    setTimeout(() => { fetchAccounts(); fetchStatus(id); }, 1000);
  };

  const deleteAccount = async (id: number) => {
    if (!confirm("Delete this account?")) return;
    await fetch("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "delete", account_id: id }),
    });
    setSelectedId(null);
    fetchAccounts();
  };

  const selected = accounts.find((a) => a.id === selectedId);
  const status = selectedId ? statuses[selectedId] : null;

  return (
    <div className="rounded-xl border border-border/50 bg-card p-4">
      <h3 className="text-lg font-semibold mb-3">Multi-Account Trading</h3>

      {/* Account tabs */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {accounts.map((a) => {
          const s = statuses[a.id];
          const running = s?.running;
          const pnl = s?.today_pnl ?? 0;
          return (
            <button
              key={a.id}
              onClick={() => setSelectedId(a.id)}
              className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                selectedId === a.id
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted hover:bg-muted/80"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${running ? "bg-green-500" : "bg-gray-400"}`} />
                <span>{a.name || `Account ${a.id}`}</span>
              </div>
              <div className={`text-xs ${pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                ${pnl.toFixed(2)} ({s?.today_trades ?? 0}t)
              </div>
            </button>
          );
        })}
        <button
          onClick={() => setEditing({ name: "", seed_capital: 100, fixed_stake: 15, sizing_mode: "FIXED", position_mode: "BOTH", daily_loss_limit: 100, mega_multiplier: 3, mega_min_score: 6, min_entry_score: 3 })}
          className="px-3 py-2 rounded-lg text-sm bg-green-700 hover:bg-green-600 text-white"
        >
          + Add
        </button>
      </div>

      {/* Edit modal */}
      {editing && (
        <div className="border border-border rounded-lg p-4 mb-4 bg-background/50">
          <h4 className="font-medium mb-2">{editing.id ? "Edit Account" : "New Account"}</h4>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <label>
              Name
              <input value={editing.name || ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Seed Capital
              <input type="number" value={editing.seed_capital || 100} onChange={(e) => setEditing({ ...editing, seed_capital: Number(e.target.value) })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Fixed Stake
              <input type="number" value={editing.fixed_stake || 15} onChange={(e) => setEditing({ ...editing, fixed_stake: Number(e.target.value) })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Daily Loss Limit
              <input type="number" value={editing.daily_loss_limit || 100} onChange={(e) => setEditing({ ...editing, daily_loss_limit: Number(e.target.value) })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Private Key
              <input type={showKeys ? "text" : "password"} value={editing.private_key || ""} onChange={(e) => setEditing({ ...editing, private_key: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              API Key
              <input type={showKeys ? "text" : "password"} value={editing.api_key || ""} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              API Secret
              <input type={showKeys ? "text" : "password"} value={editing.api_secret || ""} onChange={(e) => setEditing({ ...editing, api_secret: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              API Passphrase
              <input type={showKeys ? "text" : "password"} value={editing.api_passphrase || ""} onChange={(e) => setEditing({ ...editing, api_passphrase: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Telegram Token
              <input type={showKeys ? "text" : "password"} value={editing.telegram_token || ""} onChange={(e) => setEditing({ ...editing, telegram_token: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
            <label>
              Telegram Chat ID
              <input value={editing.telegram_chat_id || ""} onChange={(e) => setEditing({ ...editing, telegram_chat_id: e.target.value })}
                className="mt-1 w-full rounded border border-border bg-background px-2 py-1" />
            </label>
          </div>
          <div className="flex gap-2 mt-3">
            <button onClick={() => setShowKeys(!showKeys)} className="px-3 py-1 text-xs bg-muted rounded">
              {showKeys ? "Hide Keys" : "Show Keys"}
            </button>
            <button onClick={saveAccount} className="px-4 py-1 bg-primary text-primary-foreground rounded text-sm">Save</button>
            <button onClick={() => setEditing(null)} className="px-4 py-1 bg-muted rounded text-sm">Cancel</button>
          </div>
        </div>
      )}

      {/* Selected account detail */}
      {selected && (
        <div className="border border-border rounded-lg p-4 bg-background/30">
          <div className="flex items-center justify-between mb-3">
            <h4 className="font-medium">{selected.name || `Account ${selected.id}`}</h4>
            <div className="flex gap-2">
              <button onClick={() => setEditing(selected)} className="px-3 py-1 text-xs bg-muted rounded">Edit</button>
              <button onClick={() => deleteAccount(selected.id)} className="px-3 py-1 text-xs bg-red-700 text-white rounded">Delete</button>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm mb-3">
            <div className="rounded bg-muted/30 p-2">
              <div className="text-muted-foreground text-xs">Status</div>
              <div className={status?.running ? "text-green-400 font-bold" : "text-gray-400"}>
                {status?.running ? "RUNNING" : "STOPPED"}
                {status?.pid ? ` (pid=${status.pid})` : ""}
              </div>
            </div>
            <div className="rounded bg-muted/30 p-2">
              <div className="text-muted-foreground text-xs">Today PnL</div>
              <div className={`font-bold ${(status?.today_pnl ?? 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                ${(status?.today_pnl ?? 0).toFixed(2)} ({status?.today_trades ?? 0} trades)
              </div>
            </div>
            <div className="rounded bg-muted/30 p-2">
              <div className="text-muted-foreground text-xs">Seed / Stake</div>
              <div>${selected.seed_capital} / ${selected.fixed_stake}</div>
            </div>
            <div className="rounded bg-muted/30 p-2">
              <div className="text-muted-foreground text-xs">Loss Limit</div>
              <div>${selected.daily_loss_limit}</div>
            </div>
          </div>

          <div className="flex gap-2 mb-3">
            {status?.running ? (
              <button onClick={() => stopAccount(selected.id)} className="px-4 py-2 bg-red-600 text-white rounded font-medium">Stop</button>
            ) : (
              <button onClick={() => startAccount(selected.id)} className="px-4 py-2 bg-green-600 text-white rounded font-medium">Start Live</button>
            )}
          </div>

          {/* Log output */}
          {status?.log_lines && status.log_lines.length > 0 && (
            <div className="bg-black/50 rounded p-2 max-h-40 overflow-y-auto text-xs font-mono text-gray-300">
              {status.log_lines.slice(-20).map((line, i) => (
                <div key={i}>{line}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
