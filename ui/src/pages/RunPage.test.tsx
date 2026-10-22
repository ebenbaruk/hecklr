import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, fakeApi, renderApp } from "../test/helpers";
import analysis from "../test/fixtures/compare.json";

// A real analysis of the acme-maintenance smoke suite against fault F1.
const RUN = analysis.candidate.id;

describe("RunPage", () => {
  it("us01: shows the run's metrics and every scenario's Wilson interval", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${RUN}`]: analysis });
    renderApp(`/runs/${RUN}`);

    const metrics = (await screen.findByRole("heading", { name: "Metrics" })).closest("section") as HTMLElement;
    const passRate = within(metrics).getByText("pass_rate").closest("tr") as HTMLElement;
    expect(passRate).toHaveTextContent("72.7% [55.8%, 84.9%]");
    expect(within(metrics).getByText(/33 of 33 trials analyzed/)).toBeInTheDocument();

    const scenarios = screen.getByRole("heading", { name: "Scenarios" }).closest("section") as HTMLElement;
    const booking = within(scenarios).getByText("scripted-booking-2").closest("tr") as HTMLElement;
    expect(booking).toHaveTextContent("0/1");
    expect(booking).toHaveTextContent("0.0% [0.0%, 79.3%]");
  });

  it("shows sliced metrics on demand and filters failed trials", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${RUN}`]: analysis });
    renderApp(`/runs/${RUN}`);
    const metrics = (await screen.findByRole("heading", { name: "Metrics" })).closest("section") as HTMLElement;
    const before = within(metrics).getAllByRole("row").length;

    await userEvent.click(within(metrics).getByLabelText(/sliced metrics/));
    expect(within(metrics).getAllByRole("row").length).toBeGreaterThan(before);

    const trials = screen.getByRole("heading", { name: /^Trials/ }).closest("section") as HTMLElement;
    expect(within(trials).getAllByRole("row")).toHaveLength(34);
    await userEvent.click(within(trials).getByLabelText(/Failed and excluded only/));
    expect(within(trials).getAllByRole("row")).toHaveLength(10);
    expect(within(trials).getByRole("link", { name: "scripted-booking-2 #0" })).toHaveAttribute(
      "href",
      expect.stringMatching(new RegExp(`^/runs/${RUN}/trials/`)),
    );
  });
});
