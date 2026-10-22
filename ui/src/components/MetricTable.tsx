import { useState } from "react";

import { metricKind, sliceLabel } from "../format";
import type { MetricSummary } from "../types";
import { EstimateText, TableWrap, td, th } from "./common";

/** A run's metrics, whole suite first; slices behind a toggle since there can be many. */
export function MetricTable({ summaries }: { summaries: MetricSummary[] }) {
  const [slices, setSlices] = useState(false);
  const whole = summaries.filter((s) => Object.keys(s.slice).length === 0);
  const sliced = summaries.filter((s) => Object.keys(s.slice).length > 0);
  const shown = slices ? [...whole, ...sliced] : whole;
  return (
    <>
      <TableWrap>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th className={th}>Metric</th>
              <th className={th}>Slice</th>
              <th className={th}>Value [95% CI]</th>
              <th className={th}>n</th>
              <th className={th}>Interval</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((s) => (
              <tr
                key={`${s.metric}-${sliceLabel(s.slice)}`}
                className="border-b border-zinc-100 dark:border-zinc-800/60"
              >
                <td className={`${td} font-mono text-xs`}>{s.metric}</td>
                <td className={td}>{sliceLabel(s.slice)}</td>
                <td className={td}>
                  <EstimateText estimate={s.estimate} kind={metricKind(s.metric)} />
                </td>
                <td className={`${td} tabular-nums`}>{s.estimate.n}</td>
                <td className={`${td} text-xs text-zinc-500`}>{s.estimate.method}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>
      {sliced.length > 0 && (
        <label className="mt-2 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={slices}
            onChange={(e) => {
              setSlices(e.target.checked);
            }}
          />
          Show {sliced.length} sliced metrics (channel, persona, tag…)
        </label>
      )}
    </>
  );
}
