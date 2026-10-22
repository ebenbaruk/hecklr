import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router";

import { useApi } from "../api";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  Loading,
  PageTitle,
  PassBadge,
  TableWrap,
  td,
  th,
} from "../components/common";
import { Waveform } from "../components/Waveform";
import { seconds } from "../format";
import type { Event, Grade, TrialDetail } from "../types";

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** Scrolls to and highlights the event named in the URL hash (`#ev-<id>`, a grade's evidence). */
function useHighlightedEvent(): string | null {
  const { hash } = useLocation();
  const id = hash.startsWith("#ev-") ? hash.slice(4) : null;
  useEffect(() => {
    if (id) {
      document.getElementById(`ev-${id}`)?.scrollIntoView({ block: "center" });
    }
  }, [id]);
  return id;
}

export function TrialPage() {
  const { runId = "", trialId = "" } = useParams();
  const { data, error, loading } = useApi<TrialDetail>(`/api/runs/${runId}/trials/${trialId}`);
  const highlighted = useHighlightedEvent();
  if (error) return <ErrorBox error={error} />;
  if (loading || !data) return <Loading what="the trial" />;
  const { trial, row } = data;
  return (
    <>
      <p className="mb-2 text-sm">
        <Link to={`/runs/${runId}`}>← Run {runId}</Link>
      </p>
      <PageTitle
        sub={
          <>
            {trial.scenario_id} · persona {trial.persona_id} · {trial.channel} · trial #{trial.index}{" "}
            · seed {trial.seed} · {seconds(trial.duration_ms)} · {trial.turns} turns · ended:{" "}
            {trial.end_reason ?? "-"}
          </>
        }
      >
        {data.title} <PassBadge passed={row ? row.passed : null} />
        {trial.status !== "ok" && (
          <span className="ml-2">
            <Badge tone="bad">{trial.status}</Badge>
          </span>
        )}
      </PageTitle>
      {trial.error && <ErrorBox error={new Error(trial.error)} />}
      <div className="grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-3">
          {data.recordings.length > 0 && (
            <Card title="Audio">
              {data.recordings.map((r) => (
                <Waveform key={r.path} recording={r} />
              ))}
            </Card>
          )}
          <Card title="Transcript">
            <Transcript events={data.events} highlighted={highlighted} />
          </Card>
          <Card title="Timeline (tool calls, interruptions, silences)">
            <Timeline events={data.events} highlighted={highlighted} />
          </Card>
        </div>
        <div className="lg:col-span-2">
          <Card title="Grades">
            <Grades grades={data.grades} runId={runId} trialId={trial.id} />
          </Card>
          <Card title="World state: before → after">
            <WorldDiff detail={data} />
          </Card>
          {data.labels.length > 0 && (
            <Card title="Human labels">
              <ul className="space-y-1 text-sm">
                {data.labels.map((l) => (
                  <li key={l.id}>
                    {l.labeler}: {l.grader} <PassBadge passed={l.passed} />
                    {l.note && <span className="text-zinc-500"> “{l.note}”</span>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

const ROW_HIGHLIGHT = "ring-2 ring-amber-400";

function Transcript({ events, highlighted }: { events: Event[]; highlighted: string | null }) {
  const turns = events.filter((e) => e.payload.kind === "utterance" || e.payload.kind === "email");
  if (turns.length === 0) return <Empty>No utterance was recorded.</Empty>;
  return (
    <ol className="space-y-3">
      {turns.map((e) => {
        const agent = e.actor === "agent";
        const p = e.payload;
        return (
          <li
            key={e.id}
            id={`ev-${e.id}`}
            className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${agent ? "bg-zinc-100 dark:bg-zinc-800" : "ml-auto bg-sky-50 dark:bg-sky-950"} ${highlighted === e.id ? ROW_HIGHLIGHT : ""}`}
          >
            <div className="mb-1 flex gap-2 text-xs text-zinc-500">
              <span className="font-medium">{agent ? "Agent" : "Customer"}</span>
              <span>{e.t_ms} ms</span>
              {p.kind === "utterance" && p.latency_ms !== null && (
                <span>latency {p.latency_ms} ms</span>
              )}
              {p.kind === "utterance" && p.interrupted && <Badge tone="warn">interrupted</Badge>}
            </div>
            <p className="whitespace-pre-wrap">
              {p.kind === "utterance" && p.text}
              {p.kind === "email" && `Subject: ${p.message.subject}\n\n${p.message.body}`}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

function describe(e: Event): { title: string; detail: string | null; tone: "bad" | "neutral" } {
  const p = e.payload;
  switch (p.kind) {
    case "tool_call":
      return { title: `call ${p.tool} (${p.source})`, detail: json(p.args), tone: "neutral" };
    case "tool_result":
      return {
        title: `${p.tool} ${p.error ? `error: ${p.error}` : "ok"} · ${p.latency_ms} ms${p.fault ? ` · fault ${p.fault}` : ""}`,
        detail: p.error ? null : json(p.result),
        tone: p.error ? "bad" : "neutral",
      };
    case "interrupt":
      return {
        title: `${e.actor} ${p.style.replace("_", "-")} over ${p.target}: ${p.reaction_ms === null ? "never stopped" : `stopped in ${p.reaction_ms} ms`}`,
        detail: null,
        tone: "neutral",
      };
    case "silence":
      return {
        title: `${p.reason.replace("_", " ")} silence (${p.attributed_to ?? "nobody"}) ${p.end_ms - p.start_ms} ms`,
        detail: null,
        tone: "neutral",
      };
    case "handoff":
      return { title: `handoff to ${p.target ?? "a human"}`, detail: p.reason, tone: "neutral" };
    case "error":
      return { title: `${p.source} error: ${p.message}`, detail: null, tone: "bad" };
    case "audio_span":
      return {
        title: `${e.actor} ${p.content} ${p.start_ms}–${p.end_ms} ms`,
        detail: null,
        tone: "neutral",
      };
    case "utterance":
    case "email":
      return { title: e.payload.kind, detail: null, tone: "neutral" };
  }
}

function Timeline({ events, highlighted }: { events: Event[]; highlighted: string | null }) {
  const items = events.filter((e) => e.payload.kind !== "utterance" && e.payload.kind !== "email");
  if (items.length === 0) return <Empty>No tool call or turn-taking event.</Empty>;
  return (
    <ol className="space-y-1 text-sm">
      {items.map((e) => {
        const { title, detail, tone } = describe(e);
        return (
          <li
            key={e.id}
            id={`ev-${e.id}`}
            className={`rounded px-2 py-1 ${highlighted === e.id ? ROW_HIGHLIGHT : ""}`}
          >
            <span className="mr-2 inline-block w-16 text-right font-mono text-xs text-zinc-500">
              {e.t_ms} ms
            </span>
            <span className={tone === "bad" ? "text-rose-600 dark:text-rose-400" : ""}>{title}</span>
            {detail && (
              <details className="ml-18">
                <summary className="cursor-pointer text-xs text-zinc-500">details</summary>
                <pre className="mt-1 overflow-x-auto rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-950">
                  {detail}
                </pre>
              </details>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Grades({ grades, runId, trialId }: { grades: Grade[]; runId: string; trialId: string }) {
  const [diagnostic, setDiagnostic] = useState(false);
  const shown = diagnostic ? grades : grades.filter((g) => g.criterion_index !== null);
  return (
    <>
      <ul className="space-y-3 text-sm">
        {shown.map((g) => (
          <li key={g.id}>
            <div className="flex flex-wrap items-center gap-2">
              {g.status === "ok" ? <PassBadge passed={g.passed} /> : <Badge tone="warn">{g.status}</Badge>}
              <code className="font-mono text-xs">{g.grader}</code>
              <span className="text-xs text-zinc-500">
                v{g.grader_version}
                {g.judge_model && ` · ${g.judge_model}`}
              </span>
            </div>
            {g.rationale && <p className="mt-1 text-zinc-700 dark:text-zinc-300">{g.rationale}</p>}
            {g.evidence.length > 0 && (
              <p className="mt-1 text-xs">
                Evidence:{" "}
                {g.evidence.map((ref, i) => (
                  <span key={ref.event_id}>
                    {i > 0 && ", "}
                    {ref.trial_id === trialId ? (
                      <a href={`#ev-${ref.event_id}`}>event {i + 1}</a>
                    ) : (
                      <Link to={`/runs/${runId}/trials/${ref.trial_id}#ev-${ref.event_id}`}>
                        event {i + 1} (other trial)
                      </Link>
                    )}
                  </span>
                ))}
              </p>
            )}
          </li>
        ))}
      </ul>
      {grades.length !== shown.length || diagnostic ? (
        <label className="mt-3 flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={diagnostic}
            onChange={(e) => {
              setDiagnostic(e.target.checked);
            }}
          />
          Show diagnostic grades (not success criteria)
        </label>
      ) : null}
    </>
  );
}

function WorldDiff({ detail }: { detail: TrialDetail }) {
  if (detail.world_changes.length === 0) return <Empty>The world did not change.</Empty>;
  return (
    <TableWrap>
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-zinc-200 dark:border-zinc-800">
            <th className={th}>Record</th>
            <th className={th}>Before</th>
            <th className={th}>After</th>
          </tr>
        </thead>
        <tbody>
          {detail.world_changes.map((c) => (
            <tr key={`${c.collection}-${c.key}`} className="border-b border-zinc-100 dark:border-zinc-800/60">
              <td className={td}>
                <div className="font-mono">
                  {c.collection}[{c.key}]
                </div>
                <div className="text-zinc-500">{c.change}</div>
              </td>
              <td className={td}>
                <pre className="whitespace-pre-wrap text-rose-700 dark:text-rose-300">{c.before ?? "-"}</pre>
              </td>
              <td className={td}>
                <pre className="whitespace-pre-wrap text-emerald-700 dark:text-emerald-300">{c.after ?? "-"}</pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}
