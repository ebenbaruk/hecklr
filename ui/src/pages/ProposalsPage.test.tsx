import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, fakeApi, json, renderApp } from "../test/helpers";

const PROPOSAL = "01M5HR04J2AR9QWCX2NRVPP001";
const CLUSTER = "01M5HR04J2AR9QWCX2NRVPC001";
const YAML = "id: mined-double-address\ntitle: Customer gives two addresses\n";

const ITEM = {
  proposal: {
    id: PROPOSAL,
    cluster_id: CLUSTER,
    scenario: { id: "mined-double-address", title: "Customer gives two addresses", goal: "Book at home", tags: [] },
    status: "proposed",
    reviewer: null,
    note: null,
    created_at: "2026-10-22T12:00:00Z",
    decided_at: null,
  },
  yaml: YAML,
  cluster_label: "fails when the customer gives two addresses",
  source: "2026-10-22/proposals.jsonl",
};

describe("ProposalsPage", () => {
  it("us08: approves an edited scenario and says where it was written", async () => {
    const fake = fakeApi({
      "/api/meta": META,
      "/api/proposals?status=proposed": [ITEM],
      [`/api/proposals/${PROPOSAL}/decision`]: {
        proposal: { ...ITEM.proposal, status: "approved", reviewer: "ada" },
        written: "/work/acme/scenarios/mined/mined-double-address.yaml",
      },
    });
    renderApp("/proposals");

    const editor = await screen.findByLabelText(/Scenario YAML/);
    expect(screen.getByRole("link", { name: ITEM.cluster_label })).toHaveAttribute("href", `/clusters/${CLUSTER}`);
    await userEvent.clear(editor);
    await userEvent.type(editor, "id: mined-double-address{enter}title: Reviewed{enter}");
    await userEvent.type(screen.getByLabelText("Note (optional)"), "real bug");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(fake.posted[`/api/proposals/${PROPOSAL}/decision`]).toEqual([
      {
        decision: "approve",
        reviewer: "ada",
        note: "real bug",
        yaml: "id: mined-double-address\ntitle: Reviewed\n",
      },
    ]);
    expect(
      await screen.findByText("/work/acme/scenarios/mined/mined-double-address.yaml"),
    ).toBeInTheDocument();
  });

  it("sends no YAML when the scenario is approved unchanged, and rejects", async () => {
    const fake = fakeApi({
      "/api/meta": META,
      "/api/proposals?status=proposed": [ITEM],
      [`/api/proposals/${PROPOSAL}/decision`]: { proposal: ITEM.proposal, written: null },
    });
    renderApp("/proposals");

    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    expect(fake.posted[`/api/proposals/${PROPOSAL}/decision`]).toEqual([
      { decision: "reject", reviewer: "ada", note: null, yaml: null },
    ]);
  });

  it("lists the loader's problems when the scenario is invalid", async () => {
    fakeApi({
      "/api/meta": META,
      "/api/proposals?status=proposed": [ITEM],
      [`/api/proposals/${PROPOSAL}/decision`]: json(
        { detail: "the scenario is invalid", problems: ["line 2: channels: Field required"] },
        422,
      ),
    });
    renderApp("/proposals");

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("the scenario is invalid");
    expect(alert).toHaveTextContent("line 2: channels: Field required");
  });

  it("shows decided proposals read-only in their tab", async () => {
    fakeApi({
      "/api/meta": META,
      "/api/proposals?status=proposed": [],
      "/api/proposals?status=approved": [
        { ...ITEM, proposal: { ...ITEM.proposal, status: "approved", reviewer: "bob", note: "ok", decided_at: "2026-10-22T13:00:00Z" } },
      ],
    });
    renderApp("/proposals");
    expect(await screen.findByText(/Nothing to review/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Approved" }));

    await waitFor(() => {
      expect(screen.getByLabelText(/Scenario YAML/)).toHaveAttribute("readonly");
    });
    expect(screen.getByText(/by bob/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
