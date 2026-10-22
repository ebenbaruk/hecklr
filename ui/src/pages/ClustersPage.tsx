import { Link, useParams } from "react-router";

import { useApi } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageTitle } from "../components/common";
import { dateTime } from "../format";
import type { ClusterDetail, ClusterItem, ClusterMember } from "../types";

export function ClustersPage() {
  const { clusterId } = useParams();
  const clusters = useApi<ClusterItem[]>("/api/clusters");
  return (
    <>
      <PageTitle sub="Failed trials and production conversations the miner grouped by root cause.">
        Failure clusters
      </PageTitle>
      {clusters.error && <ErrorBox error={clusters.error} />}
      {clusters.loading && <Loading what="clusters" />}
      {clusters.data?.length === 0 && (
        <Empty>No cluster yet. `hecklr mine LOGS` writes them to .hecklr/mine/.</Empty>
      )}
      {clusters.data && clusters.data.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-5">
          <ul className="space-y-2 lg:col-span-2" aria-label="Clusters">
            {clusters.data.map(({ cluster, proposals }) => {
              const size = cluster.trial_ids.length + cluster.conversation_ids.length;
              const selected = cluster.id === clusterId;
              return (
                <li key={cluster.id}>
                  <Link
                    to={`/clusters/${cluster.id}`}
                    aria-current={selected ? "page" : undefined}
                    className={`block rounded-lg border p-3 text-zinc-900 no-underline hover:no-underline dark:text-zinc-100 ${selected ? "border-sky-500 bg-sky-50 dark:bg-sky-950" : "border-zinc-200 bg-white hover:border-zinc-400 dark:border-zinc-800 dark:bg-zinc-900"}`}
                  >
                    <span className="block font-medium">{cluster.label}</span>
                    <span className="mt-1 flex flex-wrap gap-2 text-xs text-zinc-500">
                      <Badge tone={cluster.source === "run" ? "info" : "neutral"}>{cluster.source}</Badge>
                      {size} member(s) · {proposals.length} proposal(s) · {dateTime(cluster.created_at)}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          <div className="lg:col-span-3">
            {clusterId ? <Detail clusterId={clusterId} /> : <Empty>Pick a cluster.</Empty>}
          </div>
        </div>
      )}
    </>
  );
}

function Detail({ clusterId }: { clusterId: string }) {
  const { data, error, loading } = useApi<ClusterDetail>(`/api/clusters/${clusterId}`);
  if (error) return <ErrorBox error={error} />;
  if (loading || !data) return <Loading what="the cluster" />;
  return (
    <Card title={data.cluster.label}>
      {data.cluster.description && <p className="mb-3 text-sm">{data.cluster.description}</p>}
      <h3 className="mb-2 text-sm font-semibold">Members ({data.members.length}), exemplars first</h3>
      <ul className="mb-4 space-y-2">
        {data.members.map((m) => (
          <Member key={m.id} member={m} />
        ))}
      </ul>
      <h3 className="mb-2 text-sm font-semibold">Proposed scenarios</h3>
      {data.proposals.length === 0 ? (
        <Empty>No scenario was synthesized from this cluster.</Empty>
      ) : (
        <ul className="space-y-1 text-sm">
          {data.proposals.map((p) => (
            <li key={p.id}>
              {p.scenario.title} <Badge tone={p.status === "approved" ? "good" : p.status === "rejected" ? "bad" : "warn"}>{p.status}</Badge>{" "}
              <Link to={`/proposals?status=${p.status}`}>review</Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Member({ member }: { member: ClusterMember }) {
  const tag = member.exemplar ? <Badge tone="info">exemplar</Badge> : null;
  if (member.conversation) {
    return (
      <li className="text-sm">
        <details open={member.exemplar}>
          <summary className="cursor-pointer">
            Conversation <code className="font-mono">{member.id}</code> {tag}
          </summary>
          <ol className="mt-2 space-y-1 border-l-2 border-zinc-200 pl-3 dark:border-zinc-700">
            {member.conversation.turns.map((turn, i) => (
              <li key={i}>
                <span className="font-medium">{turn.role === "agent" ? "Agent" : "Customer"}:</span>{" "}
                {turn.text}
              </li>
            ))}
          </ol>
        </details>
      </li>
    );
  }
  return (
    <li className="text-sm">
      {member.run_id ? (
        <Link to={`/runs/${member.run_id}/trials/${member.id}`}>Trial {member.id}</Link>
      ) : (
        <span>
          <code className="font-mono">{member.id}</code>{" "}
          <span className="text-xs text-zinc-500">(not stored here)</span>
        </span>
      )}{" "}
      {tag}
    </li>
  );
}
