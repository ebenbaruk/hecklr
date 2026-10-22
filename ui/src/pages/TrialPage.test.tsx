import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type Mock, beforeEach, describe, expect, it, vi } from "vitest";

import { META, fakeApi, renderApp } from "../test/helpers";
import chat from "../test/fixtures/trial-chat.json";

// wavesurfer.js draws on a canvas from decoded audio, which jsdom has neither of: the fake
// records what the component asks it to draw and lets the test fire its events.
interface FakeSurfer {
  created: { url: string }[];
  regions: { start: number; end?: number; content: string; color: string }[];
  handlers: Record<string, (arg?: unknown) => void>;
  playPause: Mock;
}

const surfer: FakeSurfer = vi.hoisted(() => ({
  created: [],
  regions: [],
  handlers: {},
  playPause: vi.fn(),
}));

vi.mock("wavesurfer.js", () => ({
  default: {
    create: (options: { url: string }) => {
      surfer.created.push(options);
      return {
        on: (event: string, handler: (arg?: unknown) => void) => {
          surfer.handlers[event] = handler;
        },
        playPause: surfer.playPause,
        destroy: () => undefined,
      };
    },
  },
}));

vi.mock("wavesurfer.js/dist/plugins/regions.esm.js", () => ({
  default: {
    create: () => ({
      addRegion: (region: (typeof surfer.regions)[number]) => {
        surfer.regions.push(region);
      },
    }),
  },
}));

const RUN = chat.trial.run_id;
const TRIAL = chat.trial.id;

beforeEach(() => {
  surfer.created.length = 0;
  surfer.regions.length = 0;
  surfer.handlers = {};
});

describe("TrialPage on a real chat trial", () => {
  it("us03: shows the transcript, tool calls and the world diff", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${RUN}/trials/${TRIAL}`]: chat });
    renderApp(`/runs/${RUN}/trials/${TRIAL}`);

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(chat.title);
    const transcript = screen.getByRole("heading", { name: "Transcript" }).closest("section") as HTMLElement;
    expect(within(transcript).getAllByRole("listitem")).toHaveLength(6);
    const timeline = screen.getByRole("heading", { name: /Timeline/ }).closest("section") as HTMLElement;
    expect(within(timeline).getAllByText(/^call /)).toHaveLength(3);
    const world = screen.getByRole("heading", { name: /World state/ }).closest("section") as HTMLElement;
    expect(within(world).getByText("appointments[appointments-15]")).toBeInTheDocument();
    expect(within(world).getByText("added")).toBeInTheDocument();
  });

  it("us03: shows each grade with its rationale and verdict", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${RUN}/trials/${TRIAL}`]: chat });
    renderApp(`/runs/${RUN}/trials/${TRIAL}`);

    const grades = (await screen.findByRole("heading", { name: "Grades" })).closest("section") as HTMLElement;
    expect(within(grades).getByText("state")).toBeInTheDocument();
    expect(within(grades).getByText("the visit is booked at the slot asked for: False")).toBeInTheDocument();
    expect(within(grades).getByText("fail")).toBeInTheDocument();
  });
});

const VOICE_RUN = "01M5HR2GZ13J35YSB9H1DRFHTS";
const VOICE_TRIAL = "01M5HR2H0000000000000000AA";
const EVIDENCE = "01M5HR2H0000000000000000EV";

function voiceTrial() {
  return {
    ...chat,
    trial: { ...chat.trial, id: VOICE_TRIAL, run_id: VOICE_RUN, channel: "voice" },
    title: "Caller reschedules over the phone",
    events: [
      {
        id: EVIDENCE,
        trial_id: VOICE_TRIAL,
        seq: 0,
        t_ms: 500,
        actor: "user",
        payload: { kind: "utterance", text: "Move my visit please", turn: 0, end_ms: 1500, latency_ms: null, interrupted: false },
      },
      {
        id: "01M5HR2H0000000000000000IN",
        trial_id: VOICE_TRIAL,
        seq: 1,
        t_ms: 2500,
        actor: "user",
        payload: { kind: "interrupt", target: "agent", style: "barge_in", reaction_ms: 120 },
      },
    ],
    grades: [
      {
        ...chat.grades[0],
        grader: "judge:resolved",
        rationale: "The caller's visit was never moved.",
        evidence: [{ trial_id: VOICE_TRIAL, event_id: EVIDENCE }],
        judge_model: "openai/gpt-6-luna",
      },
    ],
    world_changes: [],
    recordings: [
      {
        path: "audio/mix.wav",
        url: `/api/runs/${VOICE_RUN}/recordings/audio/mix.wav`,
        origin_ms: 0,
        markers: [
          { start_ms: 500, end_ms: 1500, actor: "user", kind: "speech" },
          { start_ms: 1900, end_ms: 2800, actor: "agent", kind: "speech" },
          { start_ms: 2500, end_ms: 2500, actor: "user", kind: "barge_in" },
        ],
        note: null,
      },
      {
        path: "../escape.wav",
        url: null,
        origin_ms: 0,
        markers: [],
        note: "recording path is outside the run directory or not a WAV file",
      },
    ],
  };
}

/** Waits for the waveform's effect to create the player and subscribe to its events. */
async function subscribed(): Promise<void> {
  await waitFor(() => {
    expect(Object.keys(surfer.handlers)).toEqual(["ready", "play", "pause", "error"]);
  });
}

describe("TrialPage on a voice trial", () => {
  it("us03: draws the waveform with caller, agent and barge-in markers", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}`]: voiceTrial() });
    renderApp(`/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}`);

    expect(await screen.findByRole("img", { name: /Waveform of audio\/mix.wav with 3 turn markers/ })).toBeInTheDocument();
    await subscribed();
    expect(surfer.created.map((c) => c.url)).toEqual([`/api/runs/${VOICE_RUN}/recordings/audio/mix.wav`]);
    act(() => {
      surfer.handlers.ready?.();
    });
    expect(surfer.regions.map((r) => [r.start, r.end, r.content])).toEqual([
      [0.5, 1.5, "user"],
      [1.9, 2.8, "agent"],
      [2.5, undefined, ""],
    ]);
    expect(screen.getByText("recording path is outside the run directory or not a WAV file")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(surfer.playPause).toHaveBeenCalledOnce();
    act(() => {
      surfer.handlers.play?.();
    });
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("links judge evidence to the event in the transcript", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}`]: voiceTrial() });
    renderApp(`/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}#ev-${EVIDENCE}`);

    const link = await screen.findByRole("link", { name: "event 1" });
    expect(link).toHaveAttribute("href", `#ev-${EVIDENCE}`);
    expect(document.getElementById(`ev-${EVIDENCE}`)).toHaveClass("ring-2");
    expect(screen.getByText("The caller's visit was never moved.")).toBeInTheDocument();
    expect(screen.getByText(/stopped in 120 ms/)).toBeInTheDocument();
  });

  it("reports a recording that cannot be decoded", async () => {
    fakeApi({ "/api/meta": META, [`/api/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}`]: voiceTrial() });
    renderApp(`/runs/${VOICE_RUN}/trials/${VOICE_TRIAL}`);
    await screen.findByRole("img", { name: /Waveform/ });
    await subscribed();

    act(() => {
      surfer.handlers.error?.(new Error("decode failed"));
    });

    expect(screen.getByText("Could not load the recording: decode failed")).toBeInTheDocument();
  });
});
