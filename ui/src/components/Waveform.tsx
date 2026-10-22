import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import RegionsPlugin from "wavesurfer.js/dist/plugins/regions.esm.js";

import type { Marker, Recording } from "../types";
import { button } from "./common";

const SPAN_COLORS: Record<string, string> = {
  user: "rgba(14, 165, 233, 0.22)",
  agent: "rgba(225, 29, 72, 0.18)",
};
// Any other actor (e.g. `system`) is neutral, so it is never mistaken for the caller.
const OTHER_SPAN_COLOR = "rgba(113, 113, 122, 0.2)";
const INSTANT_COLOR = "rgba(217, 119, 6, 0.9)";

// Instants (barge-ins) are thin lines: a text label would wrap over the neighbouring turns, so
// they are named by the legend and the marker list instead.
function markerLabel(marker: Marker): string {
  return marker.start_ms === marker.end_ms ? "" : marker.actor;
}

/** A recording's waveform with its turn markers (US-03): caller and agent spans, barge-ins. */
export function Waveform({ recording }: { recording: Recording }) {
  const container = useRef<HTMLDivElement>(null);
  const player = useRef<WaveSurfer | null>(null);
  const [playing, setPlaying] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current || !recording.url) {
      return;
    }
    const regions = RegionsPlugin.create();
    const surfer = WaveSurfer.create({
      container: container.current,
      url: recording.url,
      height: 72,
      waveColor: "#71717a",
      progressColor: "#0ea5e9",
      cursorColor: "#e11d48",
      normalize: true,
      plugins: [regions],
    });
    player.current = surfer;
    surfer.on("ready", () => {
      for (const marker of recording.markers) {
        const instant = marker.start_ms === marker.end_ms;
        regions.addRegion({
          start: marker.start_ms / 1000,
          ...(instant ? {} : { end: marker.end_ms / 1000 }),
          color: instant ? INSTANT_COLOR : (SPAN_COLORS[marker.actor] ?? OTHER_SPAN_COLOR),
          content: markerLabel(marker),
          drag: false,
          resize: false,
        });
      }
    });
    surfer.on("play", () => {
      setPlaying(true);
    });
    surfer.on("pause", () => {
      setPlaying(false);
    });
    surfer.on("error", (error: Error) => {
      setFailed(error.message);
    });
    return () => {
      surfer.destroy();
      player.current = null;
    };
  }, [recording]);

  return (
    <figure className="mb-4">
      <figcaption className="mb-1 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono text-xs">{recording.path}</span>
        {recording.url && (
          <button
            type="button"
            className={button}
            onClick={() => {
              void player.current?.playPause();
            }}
          >
            {playing ? "Pause" : "Play"}
          </button>
        )}
        <span className="text-xs text-zinc-500">
          <span className="mr-1 inline-block size-2 rounded-sm bg-sky-500/50" /> caller
          <span className="mx-1 ml-3 inline-block size-2 rounded-sm bg-rose-500/50" /> agent
          <span className="mx-1 ml-3 inline-block h-2 w-0.5 bg-amber-600" /> interruption
        </span>
      </figcaption>
      {recording.url ? (
        <div
          ref={container}
          className="rounded-md border border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950"
          aria-label={`Waveform of ${recording.path} with ${recording.markers.length} turn markers`}
          role="img"
        />
      ) : (
        <p className="text-sm text-zinc-500">{recording.note}</p>
      )}
      {failed && <p className="text-sm text-rose-600">Could not load the recording: {failed}</p>}
      <details className="mt-1 text-xs text-zinc-500">
        <summary>Turn markers ({recording.markers.length})</summary>
        <ul className="mt-1 font-mono">
          {recording.markers.map((m, i) => (
            <li key={i}>
              {m.actor} {m.kind}: {m.start_ms}
              {m.end_ms !== m.start_ms && `–${m.end_ms}`} ms
            </li>
          ))}
        </ul>
      </details>
    </figure>
  );
}
