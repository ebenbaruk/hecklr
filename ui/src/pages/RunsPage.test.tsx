import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, estimate, fakeApi, json, renderApp } from "../test/helpers";

const OLD = "01M5HQZ5MYZFXPD9FT89P7A040";
const NEW = "01M5HR0TRG9B02SWE4NPJ63X9A";
const RUNNING = "01M5HR1111111111111111111A";

function run(id: string, suite: string, status = "completed") {
  return {
    info: {
      id,
      suite,
      agent: { name: "acme-maintenance", version: null },
      git_sha: "abcdef1234",
      created_at: "2026-10-22T10:00:00Z",
      status,
      seed: 1,
      adaptive: false,
      spent_usd: "0.0123",
      simulator_model: null,
      judge_model: null,
    },
    counts: { planned: 33, ok: 33, agent_error: 0, harness_error: 0, timeout: 0 },
    planned: 33,
    baseline_id: null,
  };
}

function summary(id: string, value: number) {
  return {
    run_id: id,
    pass_rate: estimate(value, value - 0.17, Math.min(1, value + 0.12), 33),
    cost_per_trial: estimate(0.0004, 0.0003, 0.0005, 33),
    counts: {},
    problems: [],
  };
}

function api() {
  return fakeApi({
    "/api/meta": META,
    "/api/runs": [run(RUNNING, "ci", "running"), run(NEW, "smoke"), run(OLD, "smoke")],
    [`/api/runs/${NEW}/summary`]: summary(NEW, 0.727),
    [`/api/runs/${OLD}/summary`]: summary(OLD, 1),
    [`/api/runs/${RUNNING}/summary`]: json({ detail: "still being written" }, 409),
  });
}


describe("RunsPage", () => {
  it("us01: shows every run's pass rate and cost with their intervals", async () => {
    api();
    renderApp("/");

    const row = (await screen.findByRole("link", { name: NEW.slice(-8) })).closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(await cells.findByText("72.7%", { exact: false })).toHaveTextContent(
      "72.7% [55.7%, 84.7%]",
    );
    expect(cells.getByText("$0.0004", { exact: false })).toHaveTextContent(
      "$0.0004 [$0.0003, $0.0005]",
    );
    expect(await screen.findByText("results open when the run ends")).toBeInTheDocument();
  });

  it("filters by suite and status", async () => {
    api();
    renderApp("/");
    await screen.findByRole("link", { name: NEW.slice(-8) });

    await userEvent.selectOptions(screen.getByLabelText("Suite"), "ci");
    expect(screen.queryByRole("link", { name: NEW.slice(-8) })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: RUNNING.slice(-8) })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Suite"), "");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "completed");
    expect(screen.queryByRole("link", { name: RUNNING.slice(-8) })).not.toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  it("compares two picked runs with the older one as the baseline", async () => {
    api();
    renderApp("/");
    await screen.findByRole("link", { name: NEW.slice(-8) });
    const compare = screen.getByRole("button", { name: /Compare/ });
    expect(compare).toBeDisabled();

    await userEvent.click(screen.getByLabelText(`Select run ${NEW} for comparison`));
    await userEvent.click(screen.getByLabelText(`Select run ${OLD} for comparison`));
    await userEvent.click(compare);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/compare?baseline=${OLD}&candidate=${NEW}`,
      );
    });
  });

  it("explains how to get a first run", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [] });
    renderApp("/");

    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument();
  });
});
