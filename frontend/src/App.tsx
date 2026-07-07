// App root: auth gate (prod only) → onboarding gate (first run) → main app.

import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import { api, ApiError, AppSettings } from "./lib/api";
import Analytics from "./views/Analytics";
import Chat from "./views/Chat";
import Dashboard from "./views/Dashboard";
import Journal from "./views/Journal";
import Login from "./views/Login";
import Onboarding from "./views/Onboarding";
import Portfolio from "./views/Portfolio";
import Settings from "./views/Settings";
import Signals from "./views/Signals";
import System from "./views/System";

type Gate = "loading" | "login" | "onboarding" | "ready";

export default function App() {
  const [gate, setGate] = useState<Gate>("loading");

  const bootstrap = useCallback(async () => {
    try {
      const me = await api.get<{ authenticated: boolean }>("/api/auth/me");
      if (!me.authenticated) {
        setGate("login");
        return;
      }
      const settings = await api.get<AppSettings>("/api/settings");
      setGate(settings.onboarding_complete ? "ready" : "onboarding");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setGate("login");
      else setGate("ready"); // API hiccup: let views surface their own errors
    }
  }, []);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  if (gate === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950">
        <Spinner label="Starting B-Quant…" />
      </div>
    );
  }
  if (gate === "login") return <Login onLogin={bootstrap} />;

  return (
    <BrowserRouter>
      {gate === "onboarding" ? (
        <Routes>
          <Route path="*" element={<Onboarding onDone={() => setGate("ready")} />} />
        </Routes>
      ) : (
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/journal" element={<Journal />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/system" element={<System />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      )}
    </BrowserRouter>
  );
}
