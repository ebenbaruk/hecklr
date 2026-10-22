import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, estimate, fakeApi, json, renderApp } from "../test/helpers";

const RUN = "01M5HR04J2AR9QWCX2NRVP1P1M";

function item(n: number, grader: string, judgePassed: boolean) {
  const trial = `01M5HR04J2AR9QWCX2NRVPT00${n}`;
  return {
    run_id: RUN,
    trial_id: trial,
    scenario_id: `scenario-${n}`,
    title: `Scenario ${n}`,
    grade: {
      id: `01M5HR04J2AR9QWCX2NRVPG00${n}`,
      trial_id: trial,
      grader,
      grader_version: "1",
      criterion_index: 0,
      status: "ok",
      value: judgePassed,
      passed: judgePassed,
      rationale: `Judge rationale ${n}`,
      evidence: [],
      judge_model: "fake/judge",
    },
    transcript: [
      {
        id: `01M5HR04J2AR9QWCX2NRVPE00${n}`,
        trial_id: trial,
        seq: 0,
        t_ms: 0,
        actor: "user",
        payload: { kind: "utterance", text: `Customer line ${n}`, turn: 0, end_ms: 10, latency_ms: null, interrupted: false },
      },
    ],
    label: null,
  };
}

const QUEUE = {
  runs: [RUN],
  graders: ["judge:resolved", "judge:tone"],
  labeled: 0,
  items: [item(1, "judge:resolved", true), item(2, "judge:tone", false)],
};

const AGREEMENT = {
  labels: 12,
  unmatched: 1,
  judges: [
    {
      grader: "judge:resolved",
      grader_version: "1",
      judge_model: "fake/judge",
      n: 12,
      kappa: { ...estimate(0.62, 0.21, 0.93, 12), method: "bootstrap" },
      precision: estimate(0.8, 0.49, 0.94, 5),
      recall: null,
      trusted: false,
    },
  ],
};

function api() {
  return fakeApi({
    "/api/meta": META,
    "/api/runs": [],
    "/api/labels/queue": QUEUE,
    "/api/labels/agreement": AGREEMENT,
    "/api/labels": ({ body }: { body: { grade_id: string; passed: boolean; note: string | null } }) =>
      json(
        {
          id: "01M5HR04J2AR9QWCX2NRVPL001",
          trial_id: "t",
          grade_id: body.grade_id,
          grader: "judge:resolved",
          grader_version: "1",
          passed: body.passed,
          labeler: "ada",
          note: body.note,
          created_at: "2026-10-22T12:00:00Z",
        },
        201,
      ),
  });
}

describe("LabelsPage", () => {
  it("sc7: shows each judge's kappa, precision and recall with intervals and trust", async () => {
    api();
    renderApp("/labels");

    const card = (await screen.findByRole("heading", { name: /Judge agreement/ })).closest("section") as HTMLElement;
    const row = (await within(card).findByText("judge:resolved v1")).closest("tr") as HTMLElement;
    expect(within(row).getByText("0.62", { exact: false })).toHaveTextContent("0.62 [0.21, 0.93]");
    expect(within(row).getByText("80.0%", { exact: false })).toHaveTextContent("80.0% [49.0%, 94.0%]");
    expect(within(row).getByText("n/a")).toBeInTheDocument();
    expect(within(row).getByText("untrusted")).toBeInTheDocument();
    expect(within(card).getByText(/1 label\(s\) match no readable judge grade: their run is still being written or was deleted/)).toBeInTheDocument();
  });

  it("us11: hides the judge's verdict until the human labels, then records the label", async () => {
    const fake = api();
    renderApp("/labels");

    expect(await screen.findByText("Customer line 1")).toBeInTheDocument();
    expect(screen.queryByText(/Judge rationale 1/)).not.toBeInTheDocument();
    expect(screen.getByText(/hidden until you label/)).toBeInTheDocument();
    await waitFor(() => {
      expect(fake.fetch).toHaveBeenCalledWith(
        "/api/labels/queue?labeler=ada",
        expect.anything(),
      );
    });

    await userEvent.type(screen.getByLabelText("Note (optional)"), "never confirmed");
    await userEvent.click(screen.getByRole("button", { name: /Fail/ }));

    expect(fake.posted["/api/labels"]).toEqual([
      { run_id: RUN, grade_id: QUEUE.items[0]?.grade.id, passed: false, labeler: "ada", note: "never confirmed" },
    ]);
    // Moves on to the next item, and the agreement table is refreshed.
    expect(await screen.findByText("Customer line 2")).toBeInTheDocument();
    expect(screen.getByText(/labeled 1/)).toBeInTheDocument();
    expect(fake.fetch.mock.calls.filter(([url]) => url === "/api/labels/agreement").length).toBeGreaterThan(1);

    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(screen.getByText(/Judge rationale 1/)).toBeInTheDocument();
    expect(screen.getByText("disagrees with you")).toBeInTheDocument();
  });

  it("labels with the keyboard", async () => {
    const fake = api();
    renderApp("/labels");
    await screen.findByText("Customer line 1");

    await userEvent.keyboard("p");

    await waitFor(() => {
      expect(fake.posted["/api/labels"]).toHaveLength(1);
    });
    expect((fake.posted["/api/labels"]?.[0] as { passed: boolean }).passed).toBe(true);
  });

  it("filters the queue by run and rubric through the URL", async () => {
    const fake = api();
    renderApp(`/labels?run=${RUN}&grader=judge:tone`);
    await screen.findByText("Customer line 1");

    expect(fake.fetch).toHaveBeenCalledWith(
      `/api/labels/queue?labeler=ada&run=${RUN}&grader=judge%3Atone`,
      expect.anything(),
    );
  });

  it("says when there is nothing to label", async () => {
    fakeApi({
      "/api/meta": META,
      "/api/runs": [],
      "/api/labels/queue": { ...QUEUE, items: [] },
      "/api/labels/agreement": { labels: 0, unmatched: 0, judges: [] },
    });
    renderApp("/labels");

    expect(await screen.findByText(/No judge grade to label/)).toBeInTheDocument();
    expect(screen.getByText(/No label yet/)).toBeInTheDocument();
  });
});
