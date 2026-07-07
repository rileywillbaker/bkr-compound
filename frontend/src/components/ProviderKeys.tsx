// Provider key cards: step-by-step signup instructions, paste fields, Save,
// and Test connection. Used by both the onboarding wizard and Settings.
// Saved values never come back from the server — only masked previews.

import { useEffect, useState } from "react";
import { api, ProviderCheck, ProviderOverview } from "../lib/api";
import { PROVIDERS, ProviderInfo } from "../lib/providers";
import { Button, Field, Pill, inputClass } from "./ui";

function ProviderCard({
  info,
  configured,
  onSaved,
}: {
  info: ProviderInfo;
  configured: Record<string, string | null>;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<ProviderCheck | null>(null);
  const [error, setError] = useState("");

  const anyConfigured = Object.values(configured ?? {}).some((v) => v);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      for (const f of info.fields) {
        const value = values[f.field]?.trim();
        if (value) await api.put("/api/providers/credentials", { provider: info.id, field: f.field, value });
      }
      setValues({});
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.post<ProviderCheck>(`/api/providers/${info.id}/test`));
    } catch (e) {
      setTest({ provider: info.id, ok: false, detail: e instanceof Error ? e.message : "test failed" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="font-semibold">
            {info.name}{" "}
            {info.required ? (
              <span className="text-xs text-amber-400">required</span>
            ) : (
              <span className="text-xs text-slate-500">optional</span>
            )}
          </h3>
          <p className="mt-0.5 text-xs text-slate-400">{info.purpose}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Pill ok={anyConfigured}>{anyConfigured ? "configured" : "not set"}</Pill>
          <Button variant="ghost" onClick={() => setOpen(!open)}>
            {open ? "Close" : anyConfigured ? "Edit" : "Set up"}
          </Button>
        </div>
      </div>

      {open && (
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <div>
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
              How to get this key
            </h4>
            <ol className="list-decimal space-y-1.5 pl-4 text-sm text-slate-300">
              {info.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
            <a
              href={info.url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-xs text-sky-400 hover:underline"
            >
              Open {info.url.replace("https://", "")} ↗
            </a>
          </div>
          <div className="space-y-3">
            {info.fields.map((f) => (
              <Field key={f.field} label={`${f.label}${configured?.[f.field] ? ` (saved: ${configured[f.field]})` : ""}`}>
                <input
                  className={inputClass}
                  type="password"
                  placeholder={f.placeholder}
                  value={values[f.field] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
                  autoComplete="off"
                />
              </Field>
            ))}
            <div className="flex items-center gap-2">
              <Button onClick={save} disabled={saving || info.fields.every((f) => !values[f.field]?.trim())}>
                {saving ? "Saving…" : "Save"}
              </Button>
              <Button variant="ghost" onClick={runTest} disabled={testing || !anyConfigured}>
                {testing ? "Testing…" : "Test connection"}
              </Button>
            </div>
            {test && <Pill ok={test.ok}>{test.ok ? "✓ working" : `✗ ${test.detail}`}</Pill>}
            {error && <p className="text-xs text-rose-400">{error}</p>}
            <p className="text-[11px] text-slate-500">
              Stored encrypted on the server; never shown again after saving.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ProviderKeys() {
  const [overview, setOverview] = useState<ProviderOverview | null>(null);
  const [err, setErr] = useState("");

  const refresh = () =>
    api
      .get<ProviderOverview>("/api/providers")
      .then(setOverview)
      .catch((e) => setErr(e instanceof Error ? e.message : "failed to load providers"));

  useEffect(() => {
    refresh();
  }, []);

  if (err) return <p className="text-sm text-rose-400">{err}</p>;
  return (
    <div className="space-y-3">
      {PROVIDERS.map((p) => (
        <ProviderCard key={p.id} info={p} configured={overview?.configured?.[p.id] ?? {}} onSaved={refresh} />
      ))}
    </div>
  );
}
