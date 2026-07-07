// App shell: sidebar navigation + the always-visible disclaimer (spec §1.5).

import { NavLink, Outlet } from "react-router-dom";
import { DISCLAIMER } from "../lib/api";

const NAV = [
  { to: "/", label: "Dashboard", icon: "◫" },
  { to: "/chat", label: "Chat", icon: "💬" },
  { to: "/signals", label: "Signals", icon: "📡" },
  { to: "/portfolio", label: "Portfolio", icon: "📊" },
  { to: "/journal", label: "Journal", icon: "📓" },
  { to: "/analytics", label: "Analytics", icon: "📈" },
  { to: "/settings", label: "Settings", icon: "⚙" },
  { to: "/system", label: "System", icon: "🖥" },
];

export default function Layout() {
  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-100">
      <aside className="flex w-52 shrink-0 flex-col border-r border-slate-800 bg-slate-900/40">
        <div className="px-4 py-5">
          <h1 className="text-xl font-bold tracking-tight">
            B<span className="text-sky-400">-</span>Quant
          </h1>
          <p className="text-[11px] text-slate-500">AI analysis · you trade</p>
        </div>
        <nav className="flex-1 space-y-1 px-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                  isActive ? "bg-sky-500/15 text-sky-300" : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                }`
              }
            >
              <span aria-hidden>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <p className="px-4 py-4 text-[10px] leading-snug text-slate-600">{DISCLAIMER}</p>
      </aside>
      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-6xl p-6">
          <Outlet />
        </div>
        <footer className="px-6 pb-4 text-center text-[10px] text-slate-600">{DISCLAIMER}</footer>
      </main>
    </div>
  );
}
