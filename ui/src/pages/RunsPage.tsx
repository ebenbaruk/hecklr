import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, useApi, withQuery } from "../api";
import {
  Empty,
  ErrorBox,
  EstimateText,
  Loading,
  PageTitle,
  StatusBadge,
  TableWrap,
  button,
  input,
  td,
  th,
} from "../components/common";
import { dateTime, shortId } from "../format";
import type { RunItem, RunStatus, RunSummary } from "../types";

/** One run's pass rate and cost, fetched per row: each needs a bootstrap on the server. */
function SummaryCells({ runId }: { runId: string }) {
  const { data, error } = useApi<RunSummary>(`/api/runs/${runId}/summary`);
  if (error) {
    const busy = error instanceof ApiError && error.status === 409;
    return (
      <td colSpan={2} className={`${td} text-xs text-zinc-500`}>
        {busy ? "results open when the run ends" : error.message}
      </td>
    );
  }
  if (!data) {
    return (
      <td colSpan={2} className={`${td} text-xs text-zinc-400`}>
        computing…
      </td>
    );
  }
  return (
    <>
      <td className={td}>
        <EstimateText estimate={data.pass_rate} kind="rate" />
      </td>
      <td className={td}>
        <EstimateText estimate={data.cost_per_trial} kind="usd" />
      </td>
    </>
  );
}

export function RunsPage() {
  const runs = useApi<RunItem[]>("/api/runs");
  const navigate = useNavigate();
  const [suite, setSuite] = useState("");
  const [status, setStatus] = useState<RunStatus | "">("");
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState<string[]>([]);

  const suites = useMemo(
    () => [...new Set((runs.data ?? []).map((r) => r.info.suite ?? ""))].filter(Boolean).sort(),
    [runs.data],
  );
  const shown = (runs.data ?? []).filter((r) => {
    const text = `${r.info.id} ${r.info.agent.name} ${r.info.agent.version ?? ""} ${r.info.git_sha ?? ""}`;
    return (
      (!suite || r.info.suite === suite) &&
      (!status || r.info.status === status) &&
      text.toLowerCase().includes(search.toLowerCase())
    );
  });

  const toggle = (id: string) => {
    setPicked((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id].slice(-2),
    );
  };
  const compare = () => {
    // Runs list newest first, so the older pick is the baseline.
    const ordered = (runs.data ?? []).filter((r) => picked.includes(r.info.id)).reverse();
    const [baseline, candidate] = ordered.map((r) => r.info.id);
    if (baseline && candidate) {
      void navigate(withQuery("/compare", { baseline, candidate }));
    }
  };

  return (
    <>
      <PageTitle sub="Every stored run of this project. Pick two to compare them.">Runs</PageTitle>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col text-xs text-zinc-500">
          Suite
          <select
            className={input}
            value={suite}
            onChange={(e) => {
              setSuite(e.target.value);
            }}
          >
            <option value="">All suites</option>
            {suites.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs text-zinc-500">
          Status
          <select
            className={input}
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as RunStatus | "");
            }}
          >
            <option value="">Any status</option>
            {["completed", "running", "budget_exceeded", "cancelled", "failed", "pending"].map(
              (s) => (
                <option key={s} value={s}>
                  {s.replace("_", " ")}
                </option>
              ),
            )}
          </select>
        </label>
        <label className="flex flex-col text-xs text-zinc-500">
          Search
          <input
            className={input}
            type="search"
            placeholder="id, agent, git sha"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
            }}
          />
        </label>
        <button type="button" className={button} disabled={picked.length !== 2} onClick={compare}>
          Compare {picked.length}/2 selected
        </button>
      </div>
      {runs.error && <ErrorBox error={runs.error} />}
      {runs.loading && <Loading what="runs" />}
      {runs.data && shown.length === 0 && (
        <Empty>
          {runs.data.length === 0
            ? "No runs yet. Run `hecklr run` in this project, then reload."
            : "No run matches these filters."}
        </Empty>
      )}
      {shown.length > 0 && (
        <TableWrap>
          <table className="w-full text-sm">
            <caption className="sr-only">Stored runs, newest first</caption>
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className={th}>
                  <span className="sr-only">Select</span>
                </th>
                <th className={th}>Run</th>
                <th className={th}>Status</th>
                <th className={th}>Suite</th>
                <th className={th}>Agent</th>
                <th className={th}>Created</th>
                <th className={th}>Trials</th>
                <th className={th}>Pass rate [95% CI]</th>
                <th className={th}>Cost / trial [95% CI]</th>
                <th className={th}>Spent</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr
                  key={r.info.id}
                  className="border-b border-zinc-100 hover:bg-zinc-50 dark:border-zinc-800/60 dark:hover:bg-zinc-900"
                >
                  <td className={td}>
                    <input
                      type="checkbox"
                      checked={picked.includes(r.info.id)}
                      onChange={() => {
                        toggle(r.info.id);
                      }}
                      aria-label={`Select run ${r.info.id} for comparison`}
                    />
                  </td>
                  <td className={`${td} font-mono`}>
                    <Link to={`/runs/${r.info.id}`} title={r.info.id}>
                      {shortId(r.info.id)}
                    </Link>
                  </td>
                  <td className={td}>
                    <StatusBadge status={r.info.status} />
                  </td>
                  <td className={td}>{r.info.suite ?? "-"}</td>
                  <td className={td}>
                    {r.info.agent.name}
                    {r.info.git_sha && (
                      <span className="ml-1 font-mono text-xs text-zinc-500">
                        {r.info.git_sha.slice(0, 7)}
                      </span>
                    )}
                  </td>
                  <td className={`${td} whitespace-nowrap`}>{dateTime(r.info.created_at)}</td>
                  <td className={`${td} tabular-nums`}>
                    {r.counts.ok + r.counts.agent_error + r.counts.timeout + r.counts.harness_error}
                    /{r.planned}
                    {r.counts.harness_error > 0 && (
                      <span className="ml-1 text-xs text-amber-600">
                        ({r.counts.harness_error} harness errors)
                      </span>
                    )}
                  </td>
                  <SummaryCells runId={r.info.id} />
                  <td className={`${td} tabular-nums`}>${Number(r.info.spent_usd).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
    </>
  );
}
