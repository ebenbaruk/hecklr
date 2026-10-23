// Renders window.HECKLR_BENCH (written by `hecklr bench site`) as one sortable table per track.
// Every value is shown with its 95% interval and a bar on a shared scale per column, so rows whose
// bars overlap are visibly not distinguishable. Data is only ever inserted as text.
"use strict";

(function () {
  const data = window.HECKLR_BENCH;
  const TRACKS = {
    voice: {
      label: "Voice",
      note:
        "Scripted callers over voice (3 latency, 3 barge-in, 3 hesitation tasks); every stack " +
        "runs the same acme rules brain, so differences come from transport, VAD, turn " +
        "detection, ASR and TTS. Latency: end of caller speech to first agent audio.",
      fixed: [
        ["task_success", "Task success", "pct", "desc"],
        ["response_latency_p50", "Latency p50", "ms", "asc"],
        ["response_latency_p95", "Latency p95", "ms", "asc"],
        ["barge_in_reaction_p95", "Barge-in p95", "ms", "asc"],
        ["false_interruptions_per_100_turns", "False interr. /100 turns", "num", "asc"],
        ["dead_air_per_call", "Dead air /call", "num", "asc"],
        ["cost_per_conversation_usd", "Cost /call", "usd", "asc"],
      ],
      judges: true,
      sortBy: "task_success",
    },
    chat: {
      label: "Chat",
      note:
        "Guided simulated customers (LLM) over chat, graded by tool calls, world state, safety " +
        "checks and LLM judges. Latency: time to first token.",
      fixed: [
        ["task_success", "Task success", "pct", "desc"],
        ["response_latency_p50", "First token p50", "ms", "asc"],
        ["response_latency_p95", "First token p95", "ms", "asc"],
        ["cost_per_conversation_usd", "Cost /conv.", "usd", "asc"],
      ],
      judges: true,
      sortBy: "task_success",
    },
    calibration: {
      label: "Calibration",
      note:
        "Reference agents with known timing: what the harness measures when an agent answers " +
        "exactly 600 ms after the caller stops and stops 150 ms after a barge-in.",
      fixed: [
        ["response_latency_p50", "Latency p50", "ms", "asc"],
        ["response_latency_p95", "Latency p95", "ms", "asc"],
        ["barge_in_reaction_p50", "Barge-in p50", "ms", "asc"],
        ["barge_in_reaction_p95", "Barge-in p95", "ms", "asc"],
      ],
      judges: false,
      sortBy: "response_latency_p50",
    },
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  if (!data) {
    $("empty").hidden = false;
    $("empty").textContent = "data.js is missing: build the site with `hecklr bench site`.";
    return;
  }

  $("version").textContent = "v" + data.benchmark_version;
  const state = { track: null, sort: null, dir: "desc", open: new Set() };
  const rowsOf = (track) => data.rows.filter((row) => row.stack.track === track);
  const available = Object.keys(TRACKS).filter((t) => rowsOf(t).length > 0);

  function format(kind, x) {
    switch (kind) {
      case "pct":
        return (x * 100).toFixed(1) + "%";
      case "ms":
        return Math.round(x).toLocaleString("en-US") + " ms";
      case "usd":
        return "$" + x.toFixed(4);
      default:
        return x.toFixed(2);
    }
  }

  function columns(track) {
    const spec = TRACKS[track];
    const cols = spec.fixed.map(([key, label, kind, better]) => ({ key, label, kind, better }));
    if (spec.judges) {
      const judges = new Set();
      rowsOf(track).forEach((row) =>
        Object.keys(row.metrics)
          .filter((k) => k.startsWith("judge:"))
          .forEach((k) => judges.add(k)),
      );
      const judgeCols = [...judges].sort().map((key) => ({
        key,
        label: "Judge: " + key.slice(6).replace(/_/g, " "),
        kind: "pct",
        better: "desc",
      }));
      cols.splice(1, 0, ...judgeCols);
    }
    return cols.filter((c) => rowsOf(track).some((row) => row.metrics[c.key]));
  }

  function scaleOf(track, col) {
    if (col.kind === "pct") return 1;
    const highs = rowsOf(track)
      .map((row) => row.metrics[col.key])
      .filter(Boolean)
      .map((e) => e.ci_high);
    return Math.max(...highs, 1e-9);
  }

  function estimateCell(estimate, col, scale) {
    const td = el("td");
    if (!estimate) {
      td.append(el("span", "na", "n/a"));
      return td;
    }
    td.append(el("span", "value", format(col.kind, estimate.value)));
    td.append(
      el("span", "ci", "[" + format(col.kind, estimate.ci_low) + ", " + format(col.kind, estimate.ci_high) + "]"),
    );
    const bar = el("div", "ci-bar");
    bar.setAttribute("aria-hidden", "true");
    const pos = (x) => Math.max(0, Math.min(100, (x / scale) * 100));
    const range = el("span", "range");
    range.style.left = pos(estimate.ci_low) + "%";
    range.style.width = Math.max(pos(estimate.ci_high) - pos(estimate.ci_low), 0.5) + "%";
    const dot = el("span", "dot");
    dot.style.left = "calc(" + pos(estimate.value) + "% - 1px)";
    bar.append(range, dot);
    td.append(bar);
    td.title = estimate.method + ", n = " + estimate.n;
    return td;
  }

  function detailRow(row, span) {
    const tr = el("tr", "details");
    const td = el("td");
    td.colSpan = span;
    const dl = el("dl");
    const add = (term, value) => {
      dl.append(el("dt", null, term));
      const dd = el("dd");
      if (value instanceof Node) dd.append(value);
      else dd.textContent = value;
      dl.append(dd);
    };
    add("What", row.stack.description);
    add("Why it is here", row.stack.why);
    Object.entries(row.stack.components).forEach(([k, v]) => add(k, v));
    add("Conversations", row.counts.analyzed + " analyzed of " + row.counts.total +
      " (" + row.scenarios + " scenarios x " + row.trials_per_scenario + " trials; " +
      row.counts.agent_error + " agent errors, " + row.counts.timeout + " timeouts, " +
      row.counts.harness_error + " harness errors)");
    add("Spend", "$" + Number(row.spend.hecklr_usd).toFixed(4) + " Hecklr + $" +
      Number(row.spend.agent_usd).toFixed(4) + " agent");
    add("Run", el("code", null, row.run_id + " · " + row.created_at + " · " + row.duration_s + " s"));
    add("Machine", row.environment.platform + ", " + row.environment.cpus + " CPUs, Python " +
      row.environment.python + ", hecklr " + row.environment.hecklr +
      (row.environment.git_sha ? " @ " + row.environment.git_sha.slice(0, 10) : ""));
    add("Libraries", Object.entries(row.environment.packages)
      .filter(([, v]) => v !== "not installed")
      .map(([k, v]) => k + " " + v).join(", "));
    add("Reproduce", el("code", null, "hecklr bench reproduce --stack " + row.stack.id));
    td.append(dl);
    tr.append(td);
    return tr;
  }

  function render() {
    const track = state.track;
    const cols = columns(track);
    const table = $("board");
    table.replaceChildren();
    $("track-note").textContent = TRACKS[track].note;
    const rows = rowsOf(track);
    $("empty").hidden = rows.length > 0;

    const head = el("thead");
    const htr = el("tr");
    htr.append(el("th", null, "Stack"));
    cols.forEach((col) => {
      const th = el("th");
      th.scope = "col";
      if (state.sort === col.key) {
        th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
      }
      const button = el("button", null, col.label);
      button.type = "button";
      button.addEventListener("click", () => {
        if (state.sort === col.key) state.dir = state.dir === "asc" ? "desc" : "asc";
        else {
          state.sort = col.key;
          state.dir = col.better;
        }
        render();
      });
      th.append(button);
      htr.append(th);
    });
    head.append(htr);
    table.append(head);

    const sign = state.dir === "asc" ? 1 : -1;
    const key = state.sort;
    const missing = state.dir === "asc" ? Infinity : -Infinity;
    rows.sort((a, b) => {
      const va = a.metrics[key] ? a.metrics[key].value : missing;
      const vb = b.metrics[key] ? b.metrics[key].value : missing;
      return (va - vb) * sign || a.stack.id.localeCompare(b.stack.id);
    });

    const scales = Object.fromEntries(cols.map((c) => [c.key, scaleOf(track, c)]));
    const body = el("tbody");
    rows.forEach((row) => {
      const tr = el("tr", "row");
      const name = el("td");
      const title = el("div", "stack-name", row.stack.name);
      if (row.stack.reference) title.append(el("span", "badge", "reference"));
      name.append(title);
      name.append(el("div", "stack-parts", Object.values(row.stack.components).join(" · ")));
      // A thinner row (conversations without a verdict) or a busy machine changes how far the
      // row can be trusted, so both sit next to its name.
      const [loadStart, loadEnd] = row.environment.load_average;
      name.append(
        el("div", "stack-parts",
          row.counts.analyzed + "/" + row.counts.total + " conversations with a verdict · load " +
          loadStart.toFixed(1) + "→" + loadEnd.toFixed(1) + " on " + row.environment.cpus + " CPUs"),
      );
      const open = state.open.has(row.stack.id);
      const toggle = el("button", "expand", open ? "Hide details" : "Details");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", String(open));
      toggle.addEventListener("click", () => {
        if (open) state.open.delete(row.stack.id);
        else state.open.add(row.stack.id);
        render();
      });
      name.append(toggle);
      tr.append(name);
      cols.forEach((col) => tr.append(estimateCell(row.metrics[col.key], col, scales[col.key])));
      body.append(tr);
      if (open) body.append(detailRow(row, cols.length + 1));
    });
    table.append(body);
  }

  function selectTrack(track) {
    state.track = track;
    state.sort = TRACKS[track].sortBy;
    const col = TRACKS[track].fixed.find((c) => c[0] === state.sort);
    state.dir = col ? col[3] : "desc";
    document.querySelectorAll("#tabs button").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.track === track));
    });
    render();
  }

  available.forEach((track) => {
    const button = el("button", null, TRACKS[track].label + " (" + rowsOf(track).length + ")");
    button.type = "button";
    button.setAttribute("role", "tab");
    button.dataset.track = track;
    button.addEventListener("click", () => selectTrack(track));
    $("tabs").append(button);
  });

  const method = $("method");
  [
    "Benchmark v" + data.benchmark_version + ", seed " + data.seed +
      "; one conversation at a time, so stacks never compete with themselves for the CPU.",
    ...Object.entries(data.tracks).map(([name, t]) =>
      name + ": " + t.trials + " trials per scenario (" + t.scenarios + ")" +
      (t.asr ? "; Hecklr hears the agent with " + t.asr : "")),
    "Simulated customers: " + data.models.simulator + "; judges: " + data.models.judge + ".",
    "Task success and judge scores: mean of per-scenario pass rates, cluster bootstrap " +
      "(Wilson on the scenario count when every scenario is all-or-nothing).",
    "Latency and barge-in: quantiles over every turn, bootstrapped over conversations. " +
      "Cost: Hecklr's own model spend plus what the agent reported spending, per conversation.",
  ].forEach((text) => method.append(el("li", null, text)));
  $("footer").textContent = "Generated " + data.generated_at + " by hecklr bench site.";

  if (available.length) selectTrack(available[0]);
  else $("empty").hidden = false;
})();
