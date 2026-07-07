// Chat (spec §7.1): conversational assistant with read-only tools. It can
// run a single-ticker analysis through the real pipeline but can never bypass
// the risk engine or trigger alerts.

import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Button, Spinner, inputClass } from "../components/ui";

interface ChatMsg {
  id: number;
  ts: string;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name: string | null;
}

const SUGGESTIONS = [
  "Should I buy NVDA?",
  "What's the market regime right now?",
  "Summarize my portfolio",
  "Any signals today?",
];

export default function Chat() {
  const [messages, setMessages] = useState<ChatMsg[] | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .get<{ messages: ChatMsg[] }>("/api/chat/history")
      .then((b) => setMessages(b.messages))
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  const send = async (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError("");
    setInput("");
    const optimistic: ChatMsg = { id: -1, ts: new Date().toISOString(), role: "user", content: text, tool_name: null };
    setMessages((m) => [...(m ?? []), optimistic]);
    try {
      await api.post<{ reply: string }>("/api/chat", { message: text });
      const hist = await api.get<{ messages: ChatMsg[] }>("/api/chat/history");
      setMessages(hist.messages);
    } catch (e) {
      setError(e instanceof Error ? e.message : "send failed");
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(input);
  };

  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col">
      <h1 className="mb-3 text-2xl font-bold">Chat</h1>
      <div className="flex-1 space-y-3 overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        {!messages ? (
          <Spinner />
        ) : messages.length === 0 ? (
          <div className="space-y-3">
            <p className="text-sm text-slate-400">
              Ask about signals, your portfolio, the market — or "Should I buy NVDA?" to run a full pipeline
              analysis with the complete risk check.
            </p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:border-sky-500"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) =>
            m.role === "tool" ? (
              <p key={i} className="text-center text-[11px] text-slate-600">
                ⚙ consulted {m.tool_name}
              </p>
            ) : (
              <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-4 py-2 text-sm ${
                    m.role === "user" ? "bg-sky-600 text-white" : "bg-slate-800 text-slate-100"
                  }`}
                >
                  {m.content}
                </div>
              </div>
            ),
          )
        )}
        {busy && <Spinner label="Thinking…" />}
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <div ref={bottom} />
      </div>
      <form onSubmit={submit} className="mt-3 flex gap-2">
        <input
          className={inputClass}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask anything… (informational only — you place all trades)"
          disabled={busy}
        />
        <Button type="submit" disabled={busy || !input.trim()}>Send</Button>
      </form>
    </div>
  );
}
