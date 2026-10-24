import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { type Loaded, postJson, useApi, withQuery } from "../api";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  EstimateText,
  Loading,
  PageTitle,
  PassBadge,
  TableWrap,
  button,
  input,
  td,
  th,
} from "../components/common";
import { shortId } from "../format";
import type { AgreementReport, HumanLabel, LabelQueue, QueueItem, RunItem } from "../types";
import { useUser } from "../user";

export function LabelsPage() {
  const { user } = useUser();
  const [params, setParams] = useSearchParams();
  const run = params.get("run") ?? "";
  const grader = params.get("grader") ?? "";
  const runs = useApi<RunItem[]>("/api/runs");
  const queue = useApi<LabelQueue>(
    user ? withQuery("/api/labels/queue", { labeler: user, run, grader }) : null,
  );
  const agreement = useApi<AgreementReport>("/api/labels/agreement");

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };

  return (
    <>
      <PageTitle sub="Judge a stratified sample of judged trials yourself; Hecklr measures how often each LLM judge agrees with you (Cohen's κ ≥ 0.70 to be trusted).">
        Judge labeling
      </PageTitle>
      <AgreementCard report={agreement} />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col text-xs text-zinc-500">
          Run
          <select
            className={input}
            value={run}
            onChange={(e) => {
              setFilter("run", e.target.value);
            }}
          >
            <option value="">Every run with judge grades</option>
            {(runs.data ?? []).map((r) => (
              <option key={r.info.id} value={r.info.id}>
                {shortId(r.info.id)} · {r.info.suite ?? "-"}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs text-zinc-500">
          Rubric
          <select
            className={input}
            value={grader}
            onChange={(e) => {
              setFilter("grader", e.target.value);
            }}
          >
            <option value="">All rubrics</option>
            {(queue.data?.graders ?? []).map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
      </div>
      {!user && <Empty>Enter your name at the top of the page to start labeling.</Empty>}
      {queue.error && <ErrorBox error={queue.error} />}
      {queue.loading && <Loading what="the labeling queue" />}
      {queue.data && (
        <Queue
          key={`${run}-${grader}-${user}`}
          queue={queue.data}
          labeler={user}
          onLabeled={() => {
            agreement.reload();
          }}
        />
      )}
    </>
  );
}

function AgreementCard({ report }: { report: Loaded<AgreementReport> }) {
  return (
    <Card title="Judge agreement with human labels">
      {report.error && <ErrorBox error={report.error} />}
      {report.data && report.data.judges.length === 0 && (
        <Empty>No label yet. Each label you give below updates this table.</Empty>
      )}
      {report.data && report.data.judges.length > 0 && (
        <TableWrap>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className={th}>Rubric</th>
                <th className={th}>Judge model</th>
                <th className={th}>Labels</th>
                <th className={th}>Cohen's κ [95% CI]</th>
                <th className={th}>Precision (fail) [95% CI]</th>
                <th className={th}>Recall (fail) [95% CI]</th>
                <th className={th}>Trust</th>
              </tr>
            </thead>
            <tbody>
              {report.data.judges.map((j) => (
                <tr
                  key={`${j.grader}-${j.grader_version}-${j.judge_model ?? ""}`}
                  className="border-b border-zinc-100 dark:border-zinc-800/60"
                >
                  <td className={`${td} font-mono text-xs`}>
                    {j.grader} v{j.grader_version}
                  </td>
                  <td className={td}>{j.judge_model ?? "-"}</td>
                  <td className={`${td} tabular-nums`}>{j.n}</td>
                  <td className={td}>
                    <EstimateText estimate={j.kappa} kind="number" />
                  </td>
                  <td className={td}>
                    <EstimateText estimate={j.precision} kind="rate" />
                  </td>
                  <td className={td}>
                    <EstimateText estimate={j.recall} kind="rate" />
                  </td>
                  <td className={td}>
                    {j.trusted ? <Badge tone="good">trusted</Badge> : <Badge tone="bad">untrusted</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {report.data && report.data.unmatched > 0 && (
        <p className="mt-2 text-xs text-zinc-500">
          {report.data.unmatched} label(s) match no readable judge grade: their run is still being written or was deleted.
        </p>
      )}
    </Card>
  );
}

function Queue({
  queue,
  labeler,
  onLabeled,
}: {
  queue: LabelQueue;
  labeler: string;
  onLabeled: () => void;
}) {
  const [labels, setLabels] = useState<Record<string, HumanLabel>>(() =>
    Object.fromEntries(queue.items.flatMap((i) => (i.label ? [[i.grade.id, i.label]] : []))),
  );
  const firstOpen = queue.items.findIndex((i) => !i.label);
  const [index, setIndex] = useState(firstOpen === -1 ? 0 : firstOpen);
  const item = queue.items[index];
  if (!item) {
    return (
      <Empty>
        No judge grade to label in these runs. Run a suite with judge criteria or the safety checks that ask a judge
        (injection_resisted, no_policy_override, no_pii_leak) first.
      </Empty>
    );
  }
  const done = Object.keys(labels).length;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
        <span>
          Item {index + 1} of {queue.items.length} · labeled {done}
        </span>
        <progress max={queue.items.length} value={done} className="w-40" aria-label="Labeling progress" />
        <button
          type="button"
          className={button}
          disabled={index === 0}
          onClick={() => {
            setIndex(index - 1);
          }}
        >
          Previous
        </button>
        <button
          type="button"
          className={button}
          disabled={index >= queue.items.length - 1}
          onClick={() => {
            setIndex(index + 1);
          }}
        >
          Next
        </button>
      </div>
      <LabelCard
        key={item.grade.id}
        item={item}
        labeler={labeler}
        label={labels[item.grade.id]}
        onLabel={(label) => {
          setLabels((current) => ({ ...current, [label.grade_id]: label }));
          onLabeled();
          if (index < queue.items.length - 1) setIndex(index + 1);
        }}
      />
    </>
  );
}

function LabelCard({
  item,
  labeler,
  label,
  onLabel,
}: {
  item: QueueItem;
  labeler: string;
  label: HumanLabel | undefined;
  onLabel: (label: HumanLabel) => void;
}) {
  const [note, setNote] = useState(label?.note ?? "");
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async (passed: boolean) => {
    setSaving(true);
    setError(null);
    try {
      onLabel(
        await postJson<HumanLabel>("/api/labels", {
          run_id: item.run_id,
          grade_id: item.grade.id,
          passed,
          labeler,
          note: note || null,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    // Keyboard labeling: p = pass, f = fail, unless typing in the note.
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) return;
      if (e.key === "p") void save(true);
      if (e.key === "f") void save(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  });

  return (
    <Card
      title={
        <>
          {item.title} <code className="ml-2 font-mono text-xs text-zinc-500">{item.grade.grader}</code>
        </>
      }
      actions={
        <Link to={`/runs/${item.run_id}/trials/${item.trial_id}`} className="text-sm">
          Open the full trial
        </Link>
      }
    >
      <ol className="mb-4 max-h-[50vh] space-y-2 overflow-y-auto">
        {item.transcript.map((e) => (
          <li
            key={e.id}
            className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${e.actor === "agent" ? "bg-zinc-100 dark:bg-zinc-800" : "ml-auto bg-sky-50 dark:bg-sky-950"}`}
          >
            <span className="mb-1 block text-xs text-zinc-500">{e.actor === "agent" ? "Agent" : "Customer"}</span>
            <span className="whitespace-pre-wrap">
              {e.payload.kind === "utterance" && e.payload.text}
              {e.payload.kind === "email" && `Subject: ${e.payload.message.subject}\n\n${e.payload.message.body}`}
            </span>
          </li>
        ))}
      </ol>
      <fieldset className="space-y-3" disabled={saving}>
        <legend className="mb-2 text-sm font-medium">
          Does this conversation pass <code className="font-mono">{item.grade.grader}</code>?
        </legend>
        <label className="block text-sm">
          Note (optional)
          <textarea
            className={`${input} mt-1 block w-full`}
            rows={2}
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
            }}
          />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={button} onClick={() => void save(true)}>
            Pass <kbd className="text-xs text-zinc-500">p</kbd>
          </button>
          <button type="button" className={button} onClick={() => void save(false)}>
            Fail <kbd className="text-xs text-zinc-500">f</kbd>
          </button>
          {label && (
            <span className="text-sm">
              You said <PassBadge passed={label.passed} />
            </span>
          )}
        </div>
      </fieldset>
      {error && <ErrorBox error={error} />}
      <div className="mt-4 border-t border-zinc-200 pt-3 text-sm dark:border-zinc-800">
        {label ? (
          <>
            The judge said <PassBadge passed={item.grade.passed} />{" "}
            {label.passed === item.grade.passed ? (
              <Badge tone="good">agrees with you</Badge>
            ) : (
              <Badge tone="bad">disagrees with you</Badge>
            )}
            {item.grade.rationale && (
              <p className="mt-1 text-zinc-600 dark:text-zinc-400">“{item.grade.rationale}”</p>
            )}
          </>
        ) : (
          <span className="text-zinc-500">
            The judge's verdict is hidden until you label, so it cannot sway you.
          </span>
        )}
      </div>
    </Card>
  );
}
