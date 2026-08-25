# Findings — scheduled automations that RUN the agent (harness milestone 60, roadmap item 170)

Probe: revl-harness milestone 60 builds host-side scheduled automations
(create/list/cancel an automation {cadence, prompt, session}; the agent
runs on schedule, approval-gated, persisted across restarts). The driver
is HOST-side (`_start_automations` in `tools/web_server.py`) because the
in-revl scheduler cannot express the one thing an automation IS: a timer
body that runs the agent.

## The gap

Item 57's `every <d> { … }` timer bodies may only reach REQUIRED services
synchronously (G4):

```revl
every 30s {
  emit sessions.append(config.cron_session, { role: "cron", content: "tick" })
}
```

A sync emission to `sessions.append` works — the M52 Scheduler heartbeats
`tick` into the cron session and the console reads it. But a scheduled
AGENT run needs `emission async fn run_in(session_id, prompt)` — the
`Async[T]` in-flight window (item 106) — and a timer body has no async
colour today. So `every 60s { emit agent.run_in("cron", brief) }` is
unexpressible; the harness's scheduled automations run the agent from a
host loop on the session's event loop instead.

## The workload's honest workaround (milestone 60)

- `/api/schedule/automations` — create/list/cancel; each automation is
  `{name, every_ms, prompt, session}`.
- A task on the session loop (`_start_automations`) fires due automations
  via `agent.run_in` — a real scheduled agent turn, approval-gated like
  any other (a scheduled tool call pends for the human).
- `automations.json` in the data dir persists them; restart resumes
  ticking. Verified: an automation every 1500ms fired 3× in ~5s (session
  transcript auto-populated), survived a restart, and was cancelable.
- The revl timer side stays as-is: the M52 `every 30s` cron heartbeat,
  clock-pumped, readable at `/api/schedule/cron`.

## Proposal → roadmap item 170

Allow the timer body to carry the async-coloured arrow the way
provide-method bodies do (the `Async[T]` coercion + in-flight window item
106 built for spawned handles), same undo/dispose contract (unload
cancels, residue-free). Deliverable: `src/components/scheduler.rvl`
grows a configurable scheduled-agent-run automation (cadence + prompt +
session in config), `src/timer_tests.rvl` proves it fires on
`Clock.advance`, and the M52 `cron()` slice returns the scheduled agent's
replies instead of a hardcoded tick. The host-side driver stays as the
workload's persistence layer either way.
