import { Link, useSearchParams } from "react-router";

import { useApi, withQuery } from "../api";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  EstimateText,
  Loading,
  PageTitle,
  TableWrap,
  VerdictBadge,
  input,
  td,
  th,
} from "../components/common";
import { dateTime, metricKind, pValue, shortId, sliceLabel } from "../format";
import type { Analysis, Comparison, RunItem } from "../types";

function RunPicker({
  label,
  value,
  runs,
  onChange,
}: {
  label: string;
  value: string;
  runs: RunItem[];
  onChange: (id: string) => void;
}) {
  return (
    <label className="flex flex-col text-xs text-zinc-500">
      {label}
      <select
        className={`${input} max-w-xs`}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      >
        <option value="">Pick a run</option>
        {runs.map((r) => (
          <option key={r.info.id} value={r.info.id}>
            {shortId(r.info.id)} · {r.info.suite ?? "-"} · {dateTime(r.info.created_at)} ·{" "}
            {r.info.status}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ComparePage() {
  const [params, setParams] = useSearchParams();
  const baseline = params.get("baseline") ?? "";
  const candidate = params.get("candidate") ?? "";
  const runs = useApi<RunItem[]>("/api/runs");
  const ready = baseline && candidate && baseline !== candidate;
  const analysis = useApi<Analysis>(ready ? withQuery("/api/compare", { baseline, candidate }) : null);

  const pick = (key: "baseline" | "candidate") => (id: string) => {
    const next = new URLSearchParams(params);
    next.set(key, id);
    setParams(next);
  };

  return (
    <>
      <PageTitle sub="Paired by scenario: Δ is the mean of per-scenario differences, with a paired cluster-bootstrap interval.">
        Compare runs
      </PageTitle>
      <div className="mb-6 flex flex-wrap items-end gap-3">
        <RunPicker label="Baseline" value={baseline} runs={runs.data ?? []} onChange={pick("baseline")} />
        <span aria-hidden className="pb-2 text-zinc-400">
          →
        </span>
        <RunPicker
          label="Candidate"
          value={candidate}
          runs={runs.data ?? []}
          onChange={pick("candidate")}
        />
      </div>
      {!ready && <Empty>Pick two different runs.</Empty>}
      {analysis.error && <ErrorBox error={analysis.error} />}
      {analysis.loading && <Loading what="the comparison (bootstrapping intervals)" />}
      {analysis.data && <ComparisonView analysis={analysis.data} />}
    </>
  );
}

function ComparisonView({ analysis }: { analysis: Analysis }) {
  const suite = analysis.comparisons.find(
    (c) => c.metric === "pass_rate" && Object.keys(c.slice).length === 0,
  );
  const candidateId = analysis.candidate.id;
  const baselineId = analysis.baseline?.id ?? "";
  return (
    <>
      {analysis.problems.length > 0 && (
        <div role="alert" className="mb-4 rounded-md bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {analysis.problems.join("; ")}
        </div>
      )}
      {suite && <VerdictCard comparison={suite} minEffect={analysis.stats.min_effect} />}
      <Card title={`Flipped scenarios, grouped by cause (${analysis.flips.length} groups)`}>
        {analysis.flips.length === 0 ? (
          <Empty>No scenario flipped between the runs.</Empty>
        ) : (
          <ul className="space-y-3">
            {analysis.flips.map((flip) => (
              <li key={`${flip.direction}-${flip.cause}`} className="text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  {flip.direction === "pass_to_fail" ? (
                    <Badge tone="bad">pass → fail</Badge>
                  ) : (
                    <Badge tone="good">fail → pass</Badge>
                  )}
                  <code className="font-mono">{flip.cause}</code>
                  <span className="text-zinc-500">{flip.scenario_ids.length} scenario(s)</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {flip.scenario_ids.map((id) => {
                    const trial = analysis.trials.find(
                      (t) => t.scenario_id === id && t.passed === (flip.direction === "fail_to_pass"),
                    );
                    return trial ? (
                      <Link key={id} to={`/runs/${candidateId}/trials/${trial.trial_id}`}>
                        {id}
                      </Link>
                    ) : (
                      <span key={id}>{id}</span>
                    );
                  })}
                </div>
                {flip.rationale && (
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">“{flip.rationale}”</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Every comparison (Benjamini–Hochberg q-values across slices)">
        <TableWrap>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className={th}>Metric</th>
                <th className={th}>Slice</th>
                <th className={th}>Baseline</th>
                <th className={th}>Candidate</th>
                <th className={th}>Δ [95% CI]</th>
                <th className={th}>p</th>
                <th className={th}>q</th>
                <th className={th}>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {analysis.comparisons.map((c) => {
                const kind = metricKind(c.metric);
                return (
                  <tr key={c.id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                    <td className={`${td} font-mono text-xs`}>{c.metric}</td>
                    <td className={td}>{sliceLabel(c.slice)}</td>
                    <td className={td}>
                      <EstimateText estimate={c.baseline_estimate} kind={kind} />
                    </td>
                    <td className={td}>
                      <EstimateText estimate={c.candidate_estimate} kind={kind} />
                    </td>
                    <td className={td}>
                      <EstimateText estimate={c.delta} kind={kind === "rate" ? "delta" : kind} />
                    </td>
                    <td className={`${td} tabular-nums`}>{pValue(c.p_value)}</td>
                    <td className={`${td} tabular-nums`}>{pValue(c.q_value)}</td>
                    <td className={td}>
                      <VerdictBadge verdict={c.verdict} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      </Card>
      <Card title="Scenarios">
        <TableWrap>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className={th}>Scenario</th>
                <th className={th}>Baseline pass rate</th>
                <th className={th}>Candidate pass rate</th>
                <th className={th}>Flip</th>
                <th className={th}>Cause</th>
              </tr>
            </thead>
            <tbody>
              {analysis.scenarios.map((s) => (
                <tr key={s.scenario_id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                  <td className={td}>{s.title}</td>
                  <td className={td}>
                    <EstimateText estimate={s.baseline_pass_rate} kind="rate" />
                  </td>
                  <td className={td}>
                    <EstimateText estimate={s.pass_rate} kind="rate" />
                  </td>
                  <td className={td}>
                    {s.flip === "pass_to_fail" && <Badge tone="bad">pass → fail</Badge>}
                    {s.flip === "fail_to_pass" && <Badge tone="good">fail → pass</Badge>}
                  </td>
                  <td className={`${td} font-mono text-xs`}>{s.cause?.cause ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Card>
      <p className="text-sm">
        <Link to={`/runs/${baselineId}`}>Baseline run</Link> ·{" "}
        <Link to={`/runs/${candidateId}`}>Candidate run</Link>
      </p>
    </>
  );
}

function VerdictCard({ comparison, minEffect }: { comparison: Comparison; minEffect: number }) {
  return (
    <Card title="Suite verdict">
      <div className="flex flex-wrap items-center gap-4">
        <span className="text-lg">
          <VerdictBadge verdict={comparison.verdict} />
        </span>
        <span>
          Pass rate{" "}
          <EstimateText estimate={comparison.baseline_estimate} kind="rate" /> →{" "}
          <EstimateText estimate={comparison.candidate_estimate} kind="rate" />
        </span>
        <span>
          Δ <strong><EstimateText estimate={comparison.delta} kind="delta" /></strong>
        </span>
        <span className="text-sm text-zinc-500">
          p = {pValue(comparison.p_value)} · minimum effect ±{(minEffect * 100).toFixed(1)} pts
        </span>
      </div>
      {comparison.trials_needed !== null && (
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          About {comparison.trials_needed} more trials per scenario would likely decide it.
        </p>
      )}
    </Card>
  );
}
