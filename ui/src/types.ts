// The JSON the UI API returns: `hecklr_schemas` records, the reports' `Analysis` and the
// bodies in `hecklr/ui_api/models.py`. Only the fields the app reads are declared.

export type Channel = "chat" | "voice" | "email";
export type Actor = "user" | "agent" | "tool" | "system";
export type RunStatus =
  | "pending"
  | "running"
  | "completed"
  | "budget_exceeded"
  | "cancelled"
  | "failed";
export type Verdict = "improvement" | "regression" | "no_change" | "inconclusive";
export type TrialStatus = "ok" | "agent_error" | "harness_error" | "timeout";
export type Slice = Record<string, string>;

/** A number with its confidence interval: Hecklr never shows a bare metric. */
export interface Estimate {
  value: number;
  ci_low: number;
  ci_high: number;
  confidence: number;
  n: number;
  method: string;
}

export interface Meta {
  version: string;
  project: string;
  root: string;
  user: string;
  alpha: number;
  min_effect: number;
  labels_file: string;
  mine_dir: string;
  scenarios_dir: string;
}

export interface AgentRef {
  name: string;
  version: string | null;
}

export interface RunInfo {
  id: string;
  suite: string | null;
  agent: AgentRef;
  git_sha: string | null;
  created_at: string;
  status: RunStatus;
  seed: number;
  adaptive: boolean;
  spent_usd: string;
  simulator_model: string | null;
  judge_model: string | null;
}

export interface RunCounts {
  planned: number;
  ok: number;
  agent_error: number;
  harness_error: number;
  timeout: number;
}

export interface RunItem {
  info: RunInfo;
  counts: RunCounts;
  planned: number;
  baseline_id: string | null;
}

export interface TrialCounts {
  total: number;
  ok: number;
  agent_error: number;
  timeout: number;
  harness_error: number;
  undecided: number;
  grade_errors: number;
  analyzed: number;
  contradiction_rate: Estimate | null;
}

export interface RunSummary {
  run_id: string;
  pass_rate: Estimate | null;
  cost_per_trial: Estimate | null;
  counts: TrialCounts;
  problems: string[];
}

export interface MetricSummary {
  run_id: string;
  metric: string;
  slice: Slice;
  estimate: Estimate;
}

export interface Comparison {
  id: string;
  metric: string;
  slice: Slice;
  baseline_estimate: Estimate;
  candidate_estimate: Estimate;
  delta: Estimate;
  p_value: number;
  q_value: number;
  verdict: Verdict;
  trials_needed: number | null;
}

export interface FailureCause {
  cause: string;
  rationale: string | null;
}

export interface ScenarioResult {
  scenario_id: string;
  title: string;
  tags: string[];
  trials: number;
  passes: number;
  pass_rate: Estimate | null;
  baseline_pass_rate: Estimate | null;
  flip: "pass_to_fail" | "fail_to_pass" | null;
  cause: FailureCause | null;
}

export interface FlipGroup {
  direction: "pass_to_fail" | "fail_to_pass";
  cause: string;
  scenario_ids: string[];
  rationale: string | null;
}

export interface TrialRow {
  trial_id: string;
  scenario_id: string;
  persona_id: string;
  channel: Channel;
  index: number;
  status: TrialStatus;
  passed: boolean | null;
  excluded: string | null;
  causes: FailureCause[];
  labels: Record<string, string>;
  duration_ms: number;
  cost_usd: string;
}

export interface JudgeTrust {
  grader: string;
  grader_version: string;
  judge_model: string | null;
  n: number;
  kappa: Estimate;
  trusted: boolean;
}

export interface Analysis {
  candidate: RunInfo;
  baseline: RunInfo | null;
  stats: { alpha: number; min_effect: number };
  counts: TrialCounts;
  baseline_counts: TrialCounts | null;
  summaries: MetricSummary[];
  baseline_summaries: MetricSummary[];
  comparisons: Comparison[];
  verdict: Verdict | null;
  scenarios: ScenarioResult[];
  flips: FlipGroup[];
  judges: JudgeTrust[];
  trials: TrialRow[];
  problems: string[];
}

export interface EventRef {
  trial_id: string;
  event_id: string;
}

export type Payload =
  | {
      kind: "utterance";
      text: string;
      turn: number;
      end_ms: number | null;
      latency_ms: number | null;
      interrupted: boolean;
    }
  | {
      kind: "audio_span";
      start_ms: number;
      end_ms: number;
      content: string;
      recording: string | null;
    }
  | { kind: "tool_call"; call_id: string; tool: string; args: Record<string, unknown>; source: string }
  | {
      kind: "tool_result";
      call_id: string;
      tool: string;
      result: unknown;
      error: string | null;
      latency_ms: number;
      fault: string | null;
    }
  | { kind: "interrupt"; target: "user" | "agent"; style: string; reaction_ms: number | null }
  | {
      kind: "silence";
      start_ms: number;
      end_ms: number;
      attributed_to: "user" | "agent" | null;
      reason: string;
    }
  | { kind: "email"; message: { subject: string; body: string; message_id: string } }
  | { kind: "handoff"; reason: string | null; target: string | null }
  | { kind: "error"; source: string; message: string; retryable: boolean };

export interface Event {
  id: string;
  trial_id: string;
  seq: number;
  t_ms: number;
  actor: Actor;
  payload: Payload;
}

export interface Grade {
  id: string;
  trial_id: string;
  grader: string;
  grader_version: string;
  criterion_index: number | null;
  status: "ok" | "skipped" | "error";
  value: boolean | number;
  passed: boolean;
  rationale: string | null;
  evidence: EventRef[];
  judge_model: string | null;
}

export interface HumanLabel {
  id: string;
  trial_id: string;
  grade_id: string;
  grader: string;
  grader_version: string;
  passed: boolean;
  labeler: string;
  note: string | null;
  created_at: string;
}

export interface Trial {
  id: string;
  run_id: string;
  scenario_id: string;
  persona_id: string;
  channel: Channel;
  index: number;
  seed: number;
  status: TrialStatus;
  end_reason: string | null;
  duration_ms: number;
  cost_usd: string;
  turns: number;
  contradictions: number;
  error: string | null;
}

export interface WorldChange {
  collection: string;
  key: string;
  change: string;
  before: string | null;
  after: string | null;
}

export interface Marker {
  start_ms: number;
  end_ms: number;
  actor: string;
  kind: string;
}

export interface Recording {
  path: string;
  url: string | null;
  origin_ms: number;
  markers: Marker[];
  note: string | null;
}

export interface TrialDetail {
  trial: Trial;
  row: TrialRow | null;
  title: string;
  events: Event[];
  grades: Grade[];
  world_changes: WorldChange[];
  recordings: Recording[];
  labels: HumanLabel[];
}

export interface QueueItem {
  run_id: string;
  trial_id: string;
  scenario_id: string;
  title: string;
  grade: Grade;
  transcript: Event[];
  label: HumanLabel | null;
}

export interface LabelQueue {
  runs: string[];
  graders: string[];
  labeled: number;
  items: QueueItem[];
}

export interface Agreement {
  grader: string;
  grader_version: string;
  judge_model: string | null;
  n: number;
  kappa: Estimate;
  precision: Estimate | null;
  recall: Estimate | null;
  trusted: boolean;
}

export interface AgreementReport {
  labels: number;
  judges: Agreement[];
  unmatched: number;
}

export type ProposalStatus = "proposed" | "approved" | "rejected";

export interface Scenario {
  id: string;
  title: string;
  goal: string;
  tags: string[];
}

export interface ScenarioProposal {
  id: string;
  cluster_id: string;
  scenario: Scenario;
  status: ProposalStatus;
  reviewer: string | null;
  note: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface ProposalItem {
  proposal: ScenarioProposal;
  yaml: string;
  cluster_label: string | null;
  source: string;
}

export interface DecisionResult {
  proposal: ScenarioProposal;
  written: string | null;
}

export interface FailureCluster {
  id: string;
  label: string;
  description: string | null;
  source: "run" | "production";
  trial_ids: string[];
  conversation_ids: string[];
  created_at: string;
}

export interface ClusterItem {
  cluster: FailureCluster;
  proposals: ScenarioProposal[];
}

export interface Conversation {
  id: string;
  source: string;
  turns: { role: "user" | "agent"; text: string }[];
}

export interface ClusterMember {
  id: string;
  exemplar: boolean;
  run_id: string | null;
  conversation: Conversation | null;
}

export interface ClusterDetail {
  cluster: FailureCluster;
  members: ClusterMember[];
  proposals: ScenarioProposal[];
}
