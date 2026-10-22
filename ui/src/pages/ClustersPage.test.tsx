import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, fakeApi, renderApp } from "../test/helpers";

const RUN_CLUSTER = "01M5HR04J2AR9QWCX2NRVPC001";
const PROD_CLUSTER = "01M5HR04J2AR9QWCX2NRVPC002";
const RUN = "01M5HR04J2AR9QWCX2NRVPR001";
const TRIAL = "01M5HR04J2AR9QWCX2NRVPT001";

function cluster(id: string, label: string, source: "run" | "production") {
  return {
    id,
    label,
    description: source === "production" ? "The agent books the office address." : null,
    source,
    trial_ids: source === "run" ? [TRIAL] : [],
    conversation_ids: source === "production" ? ["prod-1", "prod-2"] : [],
    created_at: "2026-10-22T12:00:00Z",
  };
}

const PRODUCTION = cluster(PROD_CLUSTER, "fails when the customer gives two addresses", "production");

function api() {
  return fakeApi({
    "/api/meta": META,
    "/api/clusters": [
      { cluster: PRODUCTION, proposals: [{ id: "p" }] },
      { cluster: cluster(RUN_CLUSTER, "fails on Saturday questions", "run"), proposals: [] },
    ],
    [`/api/clusters/${PROD_CLUSTER}`]: {
      cluster: PRODUCTION,
      members: [
        {
          id: "prod-1",
          exemplar: true,
          run_id: null,
          conversation: {
            id: "prod-1",
            source: "jsonl",
            turns: [
              { role: "user", text: "Book me at home." },
              { role: "agent", text: "Booked at the office." },
            ],
          },
        },
        { id: "prod-2", exemplar: false, run_id: null, conversation: null },
      ],
      proposals: [
        {
          id: "p",
          cluster_id: PROD_CLUSTER,
          scenario: { id: "s", title: "Two addresses", goal: "g", tags: [] },
          status: "proposed",
          reviewer: null,
          note: null,
          created_at: "2026-10-22T12:00:00Z",
          decided_at: null,
        },
      ],
    },
    [`/api/clusters/${RUN_CLUSTER}`]: {
      cluster: cluster(RUN_CLUSTER, "fails on Saturday questions", "run"),
      members: [{ id: TRIAL, exemplar: true, run_id: RUN, conversation: null }],
      proposals: [],
    },
  });
}

describe("ClustersPage", () => {
  it("us08: lists clusters and shows a production cluster's exemplar conversation", async () => {
    api();
    renderApp("/clusters");

    await userEvent.click(await screen.findByRole("link", { name: /fails when the customer gives two addresses/ }));

    expect(await screen.findByText("Booked at the office.")).toBeVisible();
    expect(screen.getByText("The agent books the office address.")).toBeInTheDocument();
    expect(screen.getByText("(not stored here)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "review" })).toHaveAttribute("href", "/proposals?status=proposed");
  });

  it("links run members to the trial viewer", async () => {
    api();
    renderApp(`/clusters/${RUN_CLUSTER}`);

    expect(await screen.findByRole("link", { name: `Trial ${TRIAL}` })).toHaveAttribute(
      "href",
      `/runs/${RUN}/trials/${TRIAL}`,
    );
    expect(screen.getByText("No scenario was synthesized from this cluster.")).toBeInTheDocument();
  });

  it("explains where clusters come from when there are none", async () => {
    fakeApi({ "/api/meta": META, "/api/clusters": [] });
    renderApp("/clusters");

    expect(await screen.findByText(/hecklr mine LOGS/)).toBeInTheDocument();
  });
});
