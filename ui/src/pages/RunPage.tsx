import { useState } from "react";
import { Link, useParams } from "react-router";

import { useApi, withQuery } from "../api";
import {
  Badge,
  Card,
  ErrorBox,
  EstimateText,
  Loading,
  PageTitle,
  PassBadge,
  StatusBadge,
  TableWrap,
  td,
  th,
} from "../components/common";
import { MetricTable } from "../components/MetricTable";
import { dateTime, seconds } from "../format";
import type { Analysis, TrialRow } from "../types";

export function RunPage() {
  const { runId = "" } = useParams();
  const { data, error, loading } = useApi<Analysis>(`/api/runs/${runId}`);
  if (error) return <ErrorBox error={error} />;
  if (loading || !data) return <Loading what="the run" />;
  const run = data.candidate;
  return (
    <>
      <PageTitle
        sub={
          <>
            {run.agent.name} · suite {run.suite ?? "-"} · {dateTime(run.created_at)} · seed{" "}
            {run.seed}
            {run.git_sha && <> · git {run.git_sha.slice(0, 7)}</>}
          </>
        }
      >
        Run <span className="font-mono text-xl">{run.id}</span>{" "}
        <StatusBadge status={run.status} />
      </PageTitle>
      {data.problems.length > 0 && (
        <div role="alert" className="mb-4 rounded-md bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {data.problems.join("; ")}
        </div>
      )}
      <Card title="Metrics">
        <p className="mb-2 text-sm text-zinc-500">
          {data.counts.analyzed} of {data.counts.total} trials analyzed · {data.counts.harness_error}{" "}
          harness errors · {data.counts.undecided} undecided · spent ${Number(run.spent_usd).toFixed(4)}
          {data.counts.contradiction_rate && (
            <>
              {" "}· simulator contradiction rate{" "}
              <EstimateText estimate={data.counts.contradiction_rate} kind="rate" />
            </>
          )}
        </p>
        <MetricTable summaries={data.summaries} />
      </Card>
      {data.judges.length > 0 && (
        <Card title="Judge trust (human agreement)">
          <ul className="space-y-1 text-sm">
            {data.judges.map((j) => (
              <li key={`${j.grader}-${j.grader_version}`}>
                {j.grader} v{j.grader_version}: κ <EstimateText estimate={j.kappa} kind="number" />{" "}
                {j.trusted ? <Badge tone="good">trusted</Badge> : <Badge tone="bad">untrusted</Badge>}
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card title="Scenarios">
        <TableWrap>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className={th}>Scenario</th>
                <th className={th}>Tags</th>
                <th className={th}>Passed</th>
                <th className={th}>Pass rate [Wilson 95% CI]</th>
                <th className={th}>Main failure cause</th>
              </tr>
            </thead>
            <tbody>
              {data.scenarios.map((s) => (
                <tr key={s.scenario_id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                  <td className={td}>
                    <div className="font-medium">{s.title}</div>
                    <div className="font-mono text-xs text-zinc-500">{s.scenario_id}</div>
                  </td>
                  <td className={td}>{s.tags.join(", ")}</td>
                  <td className={`${td} tabular-nums`}>
                    {s.passes}/{s.trials}
                  </td>
                  <td className={td}>
                    <EstimateText estimate={s.pass_rate} kind="rate" />
                  </td>
                  <td className={`${td} font-mono text-xs`}>{s.cause?.cause ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Card>
      <TrialsCard runId={run.id} trials={data.trials} />
      <p className="text-sm">
        <Link to={withQuery("/labels", { run: run.id })}>Label this run's judge grades</Link>
      </p>
    </>
  );
}

function TrialsCard({ runId, trials }: { runId: string; trials: TrialRow[] }) {
  const [failedOnly, setFailedOnly] = useState(false);
  const shown = failedOnly ? trials.filter((t) => t.passed !== true) : trials;
  return (
    <Card
      title={`Trials (${shown.length})`}
      actions={
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={failedOnly}
            onChange={(e) => {
              setFailedOnly(e.target.checked);
            }}
          />
          Failed and excluded only
        </label>
      }
    >
      <TableWrap>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th className={th}>Trial</th>
              <th className={th}>Scenario</th>
              <th className={th}>Persona</th>
              <th className={th}>Channel</th>
              <th className={th}>Verdict</th>
              <th className={th}>Why</th>
              <th className={th}>Duration</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={t.trial_id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                <td className={td}>
                  <Link to={`/runs/${runId}/trials/${t.trial_id}`}>
                    {t.scenario_id} #{t.index}
                  </Link>
                </td>
                <td className={`${td} font-mono text-xs`}>{t.scenario_id}</td>
                <td className={td}>{t.persona_id}</td>
                <td className={td}>{t.channel}</td>
                <td className={td}>
                  <PassBadge passed={t.passed} />
                  {t.status !== "ok" && <span className="ml-1 text-xs">{t.status}</span>}
                </td>
                <td className={`${td} text-xs`}>{t.causes.map((c) => c.cause).join(", ")}</td>
                <td className={`${td} tabular-nums`}>{seconds(t.duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>
    </Card>
  );
}
