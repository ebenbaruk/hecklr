import type { Estimate, Slice } from "./types";

/** How an estimate's numbers are written; every kind keeps its interval. */
export type EstimateKind = "rate" | "delta" | "usd" | "ms" | "number";

const MINUS = "−";

export function percent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

function signed(text: string, value: number): string {
  if (value > 0) return `+${text}`;
  return value < 0 ? `${MINUS}${text.replace("-", "")}` : text;
}

export function formatValue(value: number, kind: EstimateKind): string {
  switch (kind) {
    case "rate":
      return percent(value);
    case "delta":
      return signed(`${(Math.abs(value) * 100).toFixed(1)} pts`, value);
    case "usd":
      return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
    case "ms":
      return `${Math.round(value)} ms`;
    case "number":
      return value.toFixed(2);
  }
}

/** `80.0% [55.1%, 93.0%]`: the value and its interval, in one string. */
export function formatEstimate(estimate: Estimate, kind: EstimateKind): string {
  const interval = `${formatValue(estimate.ci_low, kind)}, ${formatValue(estimate.ci_high, kind)}`;
  return `${formatValue(estimate.value, kind)} [${interval}]`;
}

/** The unit a metric is reported in, from its name (`pass_rate`, `response_latency_p95`...). */
export function metricKind(metric: string): EstimateKind {
  if (metric.startsWith("pass") || metric.endsWith("_rate")) return "rate";
  if (metric.includes("cost")) return "usd";
  if (metric.includes("latency") || metric.includes("reaction")) return "ms";
  return "number";
}

export function sliceLabel(slice: Slice): string {
  const parts = Object.entries(slice).map(([key, value]) => `${key}=${value}`);
  return parts.length ? parts.join(", ") : "whole suite";
}

export function shortId(id: string): string {
  return id.slice(-8);
}

export function dateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

export function pValue(p: number): string {
  return p < 0.001 ? "<0.001" : p.toFixed(3);
}
