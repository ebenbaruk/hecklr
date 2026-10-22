import { describe, expect, it } from "vitest";

import { formatEstimate, formatValue, metricKind, pValue, sliceLabel } from "./format";
import { estimate } from "./test/helpers";

describe("formatEstimate", () => {
  it("always writes the interval next to the value", () => {
    expect(formatEstimate(estimate(0.8, 0.551, 0.93), "rate")).toBe("80.0% [55.1%, 93.0%]");
  });

  it("signs deltas in points with a true minus", () => {
    expect(formatEstimate(estimate(-0.2, -0.35, -0.05), "delta")).toBe(
      "−20.0 pts [−35.0 pts, −5.0 pts]",
    );
    expect(formatValue(0.031, "delta")).toBe("+3.1 pts");
    expect(formatValue(0, "delta")).toBe("0.0 pts");
  });

  it("writes costs and latencies in their units", () => {
    expect(formatValue(0.0004, "usd")).toBe("$0.0004");
    expect(formatValue(1.5, "usd")).toBe("$1.50");
    expect(formatValue(812.4, "ms")).toBe("812 ms");
  });
});

describe("metricKind", () => {
  it.each([
    ["pass_rate", "rate"],
    ["pass^3", "rate"],
    ["pass@3", "rate"],
    ["backchannel_stop_rate", "rate"],
    ["cost_usd_per_trial", "usd"],
    ["response_latency_p95", "ms"],
    ["barge_in_reaction_p50", "ms"],
    ["false_interruptions_per_100_turns", "number"],
    ["dead_air_per_call", "number"],
  ])("%s is a %s", (metric, kind) => {
    expect(metricKind(metric)).toBe(kind);
  });
});

describe("labels", () => {
  it("names slices and the whole suite", () => {
    expect(sliceLabel({})).toBe("whole suite");
    expect(sliceLabel({ tag: "core", channel: "chat" })).toBe("tag=core, channel=chat");
  });

  it("keeps tiny p-values readable", () => {
    expect(pValue(0.0001)).toBe("<0.001");
    expect(pValue(0.04321)).toBe("0.043");
  });
});
