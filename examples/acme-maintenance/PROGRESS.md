# acme-maintenance progress

Owner: WS8 (reference agent, chat; faults F1-F6). Spec: `docs/SPEC.md` → "Testing strategy and
quality bar" (fault matrix SC8), "Core data model", "World sandbox", "Graders", "Simulator",
"Configuration". ADRs: 0004, 0004a, 0005a, 0006, 0006a, 0008a (this workstream).

## Plan

1. World `worlds/acme-maintenance.yaml`: customers, appointments, slots, invoices, refunds,
   policies, emergency tickets; declarative tools only (no `PythonOp`, so `hecklr run` never needs
   this directory on `sys.path`).
2. Agent `agent/acme_agent`: FastAPI chat service (`/chat` JSON, `/chat/stream` SSE), two brains
   (LLM tool loop via `hecklr_llm`; deterministic `rules` CI stand-in driven by the same prompt and
   tool-description files), tool backends (trial sandbox over HTTP, or a local in-process world),
   in-process callable for the `python` adapter.
3. Faults `../acme-faults/F1..F6`: one overlay file each, one changed line, README per fault.
4. Suite: `hecklr.yaml`, personas, guided scenarios (`scenarios/`), scripted scenarios
   (`scripted/`) for a free deterministic matrix.
5. Tests: world loads, suite validates via `hecklr_core`, rules-brain flows, each fault changes
   behaviour (graded with the real `hecklr_grade` graders), server over HTTP + SSE, live LLM test.
