import type { ReactNode } from "react";

import { ApiError } from "../api";
import { type EstimateKind, formatEstimate, formatValue } from "../format";
import type { Estimate, RunStatus, Verdict } from "../types";

/** A value with its confidence interval, the interval drawn smaller and muted. */
export function EstimateText({
  estimate,
  kind,
}: {
  estimate: Estimate | null | undefined;
  kind: EstimateKind;
}) {
  if (!estimate) {
    return <span className="text-zinc-600 dark:text-zinc-400">n/a</span>;
  }
  const level = Math.round(estimate.confidence * 100);
  return (
    <span
      className="whitespace-nowrap tabular-nums"
      title={`${formatEstimate(estimate, kind)}, ${level}% CI (${estimate.method}, n=${estimate.n})`}
    >
      {formatValue(estimate.value, kind)}{" "}
      <span className="text-xs text-zinc-600 dark:text-zinc-400">
        [{formatValue(estimate.ci_low, kind)}, {formatValue(estimate.ci_high, kind)}]
      </span>
    </span>
  );
}

const TONES = {
  good: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200",
  bad: "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-200",
  warn: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
  neutral: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  info: "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

const STATUS_TONES: Record<RunStatus, Tone> = {
  completed: "good",
  running: "info",
  pending: "neutral",
  budget_exceeded: "warn",
  cancelled: "warn",
  failed: "bad",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return <Badge tone={STATUS_TONES[status]}>{status.replace("_", " ")}</Badge>;
}

const VERDICT_TONES: Record<Verdict, Tone> = {
  improvement: "good",
  regression: "bad",
  no_change: "neutral",
  inconclusive: "warn",
};

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return <Badge tone={VERDICT_TONES[verdict]}>{verdict.replace("_", " ")}</Badge>;
}

export function PassBadge({ passed }: { passed: boolean | null }) {
  if (passed === null) return <Badge tone="neutral">excluded</Badge>;
  return passed ? <Badge tone="good">pass</Badge> : <Badge tone="bad">fail</Badge>;
}

export function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="py-6 text-sm text-zinc-600 dark:text-zinc-400">
      Loading {what}…
    </p>
  );
}

export function ErrorBox({ error }: { error: Error }) {
  const problems = error instanceof ApiError ? error.problems : [];
  return (
    <div
      role="alert"
      className="my-4 rounded-md border border-rose-300 bg-rose-50 p-3 text-sm text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200"
    >
      <p>{error.message}</p>
      {problems.length > 0 && (
        <ul className="mt-2 list-disc pl-5">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Card({
  title,
  children,
  actions,
}: {
  title?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="mb-6 rounded-lg border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      {(title ?? actions) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-base font-semibold">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function PageTitle({ children, sub }: { children: ReactNode; sub?: ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight">{children}</h1>
      {sub && <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{sub}</p>}
    </header>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-4 text-sm text-zinc-600 dark:text-zinc-400">{children}</p>;
}

/** Wraps a table so it scrolls sideways on narrow screens instead of overflowing the page. */
export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="-mx-4 overflow-x-auto px-4">{children}</div>;
}

export const th = "px-2 py-2 text-left text-xs font-medium uppercase tracking-wide text-zinc-600 dark:text-zinc-400";
export const td = "px-2 py-2 align-top";
export const button =
  "inline-flex items-center gap-1 rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-800 dark:hover:bg-zinc-700";
export const input =
  "rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900";
