// Trade journal (spec §7.5): auto-created from signals + user decisions.

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Card, ErrorNote, Pill, Spinner, fmtWhen } from "../components/ui";

interface Entry {
  id: number;
  ts: string;
  signal_id: string;
  ticker: string;
  decision: string;
  note: string;
}

export default function Journal() {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<Entry[]>("/api/signals/journal/entries")
      .then(setEntries)
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  if (error) return <ErrorNote message={error} />;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Trade Journal</h1>
      <p className="text-sm text-slate-400">
        Entries appear automatically when you mark a signal taken, skipped, or modified in the Signals feed.
      </p>
      <Card>
        {!entries ? (
          <Spinner />
        ) : entries.length === 0 ? (
          <p className="text-sm text-slate-500">No journal entries yet — decide on a signal first.</p>
        ) : (
          <ul className="divide-y divide-slate-800">
            {entries.map((e) => (
              <li key={e.id} className="py-3">
                <div className="flex items-center gap-3 text-sm">
                  <span className="font-mono font-semibold">{e.ticker}</span>
                  <Pill ok={e.decision === "taken"}>{e.decision}</Pill>
                  <span className="ml-auto text-xs text-slate-500">{fmtWhen(e.ts)}</span>
                </div>
                {e.note && <p className="mt-1 text-sm text-slate-400">“{e.note}”</p>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
