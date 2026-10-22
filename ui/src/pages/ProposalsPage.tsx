import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { postJson, useApi, withQuery } from "../api";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  Loading,
  PageTitle,
  button,
  input,
} from "../components/common";
import { dateTime } from "../format";
import type { DecisionResult, Meta, ProposalItem, ProposalStatus } from "../types";
import { useUser } from "../user";

const TABS: { status: ProposalStatus; label: string }[] = [
  { status: "proposed", label: "To review" },
  { status: "approved", label: "Approved" },
  { status: "rejected", label: "Rejected" },
];

export function ProposalsPage() {
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") ?? "proposed") as ProposalStatus;
  const proposals = useApi<ProposalItem[]>(withQuery("/api/proposals", { status }));
  const meta = useApi<Meta>("/api/meta");
  const [decided, setDecided] = useState<DecisionResult | null>(null);

  return (
    <>
      <PageTitle
        sub={
          <>
            Scenarios the miner synthesized from failure clusters. Approved ones are written to{" "}
            <code className="font-mono">{meta.data?.scenarios_dir ?? "scenarios/mined"}</code> and
            join every suite whose glob matches.
          </>
        }
      >
        Scenario approvals
      </PageTitle>
      <div role="tablist" aria-label="Proposal status" className="mb-4 flex gap-2">
        {TABS.map((tab) => (
          <button
            key={tab.status}
            type="button"
            role="tab"
            aria-selected={status === tab.status}
            className={`${button} ${status === tab.status ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : ""}`}
            onClick={() => {
              setParams({ status: tab.status });
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {decided?.written && (
        <p role="status" className="mb-4 rounded-md bg-emerald-50 p-3 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200">
          Approved: wrote <code className="font-mono">{decided.written}</code>
        </p>
      )}
      {decided?.warnings.map((warning) => (
        <p key={warning} role="alert" className="mb-4 rounded-md bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {warning}
        </p>
      ))}
      {proposals.error && <ErrorBox error={proposals.error} />}
      {proposals.loading && <Loading what="proposals" />}
      {proposals.data?.length === 0 && (
        <Empty>
          {status === "proposed"
            ? "Nothing to review. `hecklr mine LOGS` writes proposals here."
            : `No ${status} proposal.`}
        </Empty>
      )}
      {proposals.data?.map((item) => (
        <ProposalCard
          key={`${item.proposal.id}-${item.proposal.status}`}
          item={item}
          onDecided={(result) => {
            setDecided(result);
            proposals.reload();
          }}
        />
      ))}
    </>
  );
}

function ProposalCard({
  item,
  onDecided,
}: {
  item: ProposalItem;
  onDecided: (result: DecisionResult) => void;
}) {
  const { user } = useUser();
  const { proposal } = item;
  const [yaml, setYaml] = useState(item.yaml);
  const [note, setNote] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);
  const open = proposal.status === "proposed";

  const decide = async (decision: "approve" | "reject") => {
    setSaving(true);
    setError(null);
    try {
      onDecided(
        await postJson<DecisionResult>(`/api/proposals/${proposal.id}/decision`, {
          decision,
          reviewer: user,
          note: note || null,
          yaml: decision === "approve" && yaml !== item.yaml ? yaml : null,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title={
        <>
          {proposal.scenario.title}{" "}
          <code className="ml-1 font-mono text-xs text-zinc-500">{proposal.scenario.id}</code>
        </>
      }
      actions={
        <span className="text-xs text-zinc-500">
          from cluster{" "}
          <Link to={`/clusters/${proposal.cluster_id}`}>{item.cluster_label ?? proposal.cluster_id}</Link>{" "}
          · {dateTime(proposal.created_at)} · {item.source}
        </span>
      }
    >
      <p className="mb-3 text-sm">
        <span className="font-medium">Goal:</span> {proposal.scenario.goal}
      </p>
      {!open && (
        <p className="mb-3 text-sm">
          <Badge tone={proposal.status === "approved" ? "good" : "bad"}>{proposal.status}</Badge> by{" "}
          {proposal.reviewer}
          {proposal.decided_at && ` on ${dateTime(proposal.decided_at)}`}
          {proposal.note && <span className="text-zinc-500"> · “{proposal.note}”</span>}
        </p>
      )}
      <label className="block text-sm">
        <span className="font-medium">Scenario YAML</span>
        {open && <span className="ml-2 text-xs text-zinc-500">edit before approving if needed</span>}
        <textarea
          className={`${input} mt-1 block w-full font-mono text-xs`}
          rows={Math.min(24, yaml.split("\n").length + 1)}
          value={yaml}
          readOnly={!open}
          spellCheck={false}
          onChange={(e) => {
            setYaml(e.target.value);
          }}
        />
      </label>
      {open && (
        <fieldset disabled={saving || !user} className="mt-3 flex flex-wrap items-end gap-2">
          <label className="flex flex-1 flex-col text-sm">
            Note (optional)
            <input
              className={input}
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
              }}
            />
          </label>
          <button type="button" className={button} onClick={() => void decide("approve")}>
            Approve
          </button>
          <button type="button" className={button} onClick={() => void decide("reject")}>
            Reject
          </button>
          {yaml !== item.yaml && (
            <button
              type="button"
              className={button}
              onClick={() => {
                setYaml(item.yaml);
              }}
            >
              Undo edits
            </button>
          )}
        </fieldset>
      )}
      {open && !user && <p className="mt-2 text-xs text-zinc-500">Enter your name at the top to review.</p>}
      {error && <ErrorBox error={error} />}
    </Card>
  );
}
