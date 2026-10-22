import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, fakeApi, json, renderApp } from "../test/helpers";
import compare from "../test/fixtures/compare.json";

// A real comparison from the acme-maintenance smoke suite: clean agent vs fault F1.
const BASE = "01M5HQZ5MYZFXPD9FT89P7A040";
const CAND = "01M5HR0TRG9B02SWE4NPJ63X9A";

function runItem(id: string) {
  return {
    info: { ...compare.candidate, id },
    counts: { planned: 33, ok: 33, agent_error: 0, harness_error: 0, timeout: 0 },
    planned: 33,
    baseline_id: null,
  };
}

describe("ComparePage", () => {
  it("us04: shows the suite verdict and its delta with the interval", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [runItem(CAND), runItem(BASE)], "/api/compare": compare });
    renderApp(`/compare?baseline=${BASE}&candidate=${CAND}`);

    const verdict = (await screen.findByRole("heading", { name: "Suite verdict" })).closest("section");
    const card = within(verdict as HTMLElement);
    expect(card.getByText("regression")).toBeInTheDocument();
    expect(card.getByText("−27.3 pts", { exact: false })).toHaveTextContent(
      "−27.3 pts [−42.4 pts, −12.1 pts]",
    );
    expect(card.getByText(/minimum effect ±3.0 pts/)).toBeInTheDocument();
  });

  it("us05: groups flipped scenarios by cause and links to a failing trial", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [], "/api/compare": compare });
    renderApp(`/compare?baseline=${BASE}&candidate=${CAND}`);

    const flips = (await screen.findByRole("heading", { name: /Flipped scenarios/ })).closest(
      "section",
    ) as HTMLElement;
    expect(within(flips).getByText("state: the visit moved to the slot asked for")).toBeInTheDocument();
    expect(within(flips).getByText("state: the visit is booked at the slot asked for")).toBeInTheDocument();
    const link = within(flips).getByRole("link", { name: "scripted-booking-2" });
    expect(link.getAttribute("href")).toMatch(new RegExp(`^/runs/${CAND}/trials/[0-9A-Z]{26}$`));
  });

  it("lists every slice comparison with p, q and a verdict", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [], "/api/compare": compare });
    renderApp(`/compare?baseline=${BASE}&candidate=${CAND}`);

    const table = (await screen.findByRole("heading", { name: /Every comparison/ })).closest(
      "section",
    ) as HTMLElement;
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(compare.comparisons.length + 1);
    expect(within(rows[1] as HTMLElement).getByText("whole suite")).toBeInTheDocument();
  });

  it("asks for two runs and changes the URL when one is picked", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [runItem(CAND), runItem(BASE)] });
    renderApp("/compare");

    expect(await screen.findByText("Pick two different runs.")).toBeInTheDocument();
    await screen.findAllByRole("option", { name: new RegExp(BASE.slice(-8)) });
    await userEvent.selectOptions(screen.getByLabelText("Baseline"), BASE);
    expect(screen.getByTestId("location")).toHaveTextContent(`/compare?baseline=${BASE}`);
  });

  it("shows the server's error", async () => {
    fakeApi({
      "/api/meta": META,
      "/api/runs": [],
      "/api/compare": json({ detail: "no stored run 'X'" }, 404),
    });
    renderApp(`/compare?baseline=${BASE}&candidate=${CAND}`);

    expect(await screen.findByRole("alert")).toHaveTextContent("no stored run 'X'");
  });
});
