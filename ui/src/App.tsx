import { useState } from "react";
import { NavLink, Route, Routes } from "react-router";

import { useApi } from "./api";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserContext, useStoredUser } from "./user";
import { ClustersPage } from "./pages/ClustersPage";
import { ComparePage } from "./pages/ComparePage";
import { LabelsPage } from "./pages/LabelsPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ProposalsPage } from "./pages/ProposalsPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { TrialPage } from "./pages/TrialPage";
import type { Meta } from "./types";

const NAV = [
  { to: "/", label: "Runs", end: true },
  { to: "/compare", label: "Compare", end: false },
  { to: "/clusters", label: "Clusters", end: false },
  { to: "/proposals", label: "Approvals", end: false },
  { to: "/labels", label: "Labeling", end: false },
];

function navClass({ isActive }: { isActive: boolean }): string {
  const base = "rounded-md px-3 py-1.5 text-sm font-medium no-underline hover:no-underline";
  return isActive
    ? `${base} bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900`
    : `${base} text-zinc-700 hover:bg-zinc-200 dark:text-zinc-300 dark:hover:bg-zinc-800`;
}

export function App() {
  const meta = useApi<Meta>("/api/meta");
  const [user, setUser] = useStoredUser(meta.data?.user);
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <UserContext value={{ user, setUser }}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:p-2"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-40 border-b border-zinc-200 bg-white/90 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/90">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-2">
          <NavLink to="/" className="flex items-center gap-2 text-zinc-900 no-underline hover:no-underline dark:text-zinc-100">
            <img src="/favicon.svg" alt="" className="size-6" />
            <span className="font-semibold">Hecklr</span>
            {meta.data && (
              <span className="hidden text-sm text-zinc-600 dark:text-zinc-400 sm:inline">{meta.data.project}</span>
            )}
          </NavLink>
          <button
            type="button"
            className="ml-auto rounded-md border border-zinc-300 px-2 py-1 text-sm md:hidden dark:border-zinc-700"
            aria-expanded={menuOpen}
            aria-controls="main-nav"
            onClick={() => {
              setMenuOpen((open) => !open);
            }}
          >
            Menu
          </button>
          <nav
            id="main-nav"
            aria-label="Main"
            className={`${menuOpen ? "flex" : "hidden"} w-full flex-col gap-1 md:ml-4 md:flex md:w-auto md:flex-row`}
          >
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={navClass}
                onClick={() => {
                  setMenuOpen(false);
                }}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-2 md:ml-auto">
            <label className="flex items-center gap-1 text-sm text-zinc-600 dark:text-zinc-400">
              <span>You:</span>
              <input
                value={user}
                onChange={(e) => {
                  setUser(e.target.value);
                }}
                className="w-28 rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-sm text-zinc-900 dark:border-zinc-700 dark:text-zinc-100"
                aria-label="Your name for labels and reviews"
              />
            </label>
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-6">
        <Routes>
          <Route path="/" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/runs/:runId/trials/:trialId" element={<TrialPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/clusters" element={<ClustersPage />} />
          <Route path="/clusters/:clusterId" element={<ClustersPage />} />
          <Route path="/proposals" element={<ProposalsPage />} />
          <Route path="/labels" element={<LabelsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
      {meta.data && (
        <footer className="mx-auto max-w-7xl px-4 pb-8 text-xs text-zinc-600 dark:text-zinc-400">
          Hecklr {meta.data.version} · {meta.data.root} · values show [95% confidence interval]
        </footer>
      )}
    </UserContext>
  );
}
