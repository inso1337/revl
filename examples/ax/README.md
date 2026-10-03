# revl as the MCP gate inside a google/ax Workspace

[google/ax](https://github.com/google/ax) runs agent tasks on Kubernetes, each in
its own sandbox, and a `Workspace` wires git repos, MCP servers and skills into
the task. AX contains what happens inside the sandbox. It cannot undo what the
agent did outside it: a ticket filed, a row written, a push. This example puts
`revl mcp serve` in the Workspace as the task's only MCP server, so every
outside effect the agent causes goes through revl's gate and lands in revl's
write-ahead log, and `revl recover` can say afterwards what is still out in the
world.

It needs no change to AX. It is written against **google/ax commit
`9edb7b1b0ca55f71670da02674b5cd6adeb49b7e`** (`v0.3.1-7-g9edb7b1`, 2026-09-27).
AX is alpha and says breaking changes are expected, so this directory is kept
small and separate from revl's core: nothing under `src/` knows about AX.

| file | what it is |
|---|---|
| `ax.yaml` | the `Workspace` and `Task`, in AX's `ax.io/v1alpha1` schema |
| `Dockerfile` | the task image: AX's default runner image plus revl and its cordis-py runtime |
| `validate.sh` | decodes and validates `ax.yaml` with AX's own Go code at the pinned commit |
| `../../tests/test_examples_ax.py` | runs the gate exactly as the manifest launches it, and checks what `revl recover` reports |

## How it fits together

```
AX task sandbox (one Agent Substrate actor, gVisor)
  /workspace/app                       durable dir, snapshotted on suspend
    .revl/tickets.rvl                  operator host code, inlined by the Workspace
    .revl/after-agent.sh               runs the agent, then `revl recover`
    .revl/wal/revl-approval-<id>.wal   revl's write-ahead log
  your agent  --MCP over stdio-->  revl mcp serve  --host code-->  ticket system (outside)
```

1. AX's runner writes the Workspace's `files` into `/workspace/app` before the
   task command starts.
2. The task command is `sh after-agent.sh <your agent>`. Your agent reads
   `mcp.servers` from `$AX_METADATA_URL/metadata/v1alpha1/ax/workspaces` and
   starts `revl-gate`, which is `revl mcp serve` over stdio.
3. The agent sends revl source that composes the operator's `Tickets` service,
   loads it with `record: true`, and calls it. The call is an `emission` with no
   inverse, so it is an approval class (c) crossing: revl returns a ticket, and
   the call fires only after `revl_approve`. Each crossing is appended to the
   WAL, with its arguments, as it happens.
4. When the agent is done it commits the session (`revl_commit`, then
   `revl_commit_confirm`). When it exits, for any reason, `after-agent.sh` runs
   `revl recover` on every WAL and writes the verdict to
   `.revl/recovery/<id>.json` and a one-line summary to the task's stdout.

The agent's side, as an MCP client sees it:

```revl
use "tickets.rvl" { Tickets }

service Triage { emission[tickets] fn report(title: Str) }

component Agent requires tickets: Tickets provides triage: Triage {
  provide triage {
    fn report(title) { emit tickets.file(title) }
  }
}
```

```
revl_load   {source: <the block above>, record: true}
revl_call   {key: "triage", method: "report", args: ["disk full on build-7"]}
            -> approvalRequired, ticket {hash, crossings: [host_file (class c)]}
revl_approve {hash}
revl_call   {key: "triage", method: "report", args: ["disk full on build-7"]}   -> ok
revl_commit {}                        -> manifest {hash}
revl_commit_confirm {hash}            -> committed
```

## What AX isolates

Read from AX's source at the pinned commit:

- **One sandbox per task.** The controller creates an Agent Substrate actor
  named after the task from a per-task `ActorTemplate` whose sandbox class is
  gVisor (`internal/substrate/client.go`, `BuildActorTemplate`).
- **One durable directory.** `/workspace` is a `DurableDir` volume. Suspend
  snapshots it with scope `DATA`, which per Substrate's API keeps durable data
  but not process memory or root filesystem changes. Resume starts a fresh
  process tree on the restored `/workspace`.
- **Guest services only on request.** Process execution and file access into
  the sandbox (what `ax ssh` uses) are served only when `spec.debug` is true.
- **Not applied at this commit:** `spec.resources` is accepted by the schema
  but nothing in the controller reads it, and AX sets no Substrate egress
  policy, so what the sandbox can reach on the network is the cluster's
  default.
- **Credentials in the sandbox.** `spec.env` and, when the atespace has a
  `gemini-api-secret` or the controller has one in its own environment,
  `GEMINI_API_KEY` are set in the container environment, where the agent can
  read them.

## What revl covers

- **One door for outside effects.** The agent talks to one MCP server. It may
  compose the services the operator granted (`--provider tickets.rvl --grant
  Tickets`); `revl mcp serve` compiles everything the agent sends under the
  untrusted-author profile, so the agent cannot declare host code of its own,
  cannot call the provider's `host_file` directly, and cannot name files outside
  `--root /workspace/app`. `tests/test_examples_ax.py` checks the refusal.
- **Classified calls.** Every operation's effect class comes from the compiler,
  not from the tool author's say-so. Under the approval gate an emission with
  no inverse needs an approval before it fires.
- **A durable record.** With `record: true` the session keeps a JSON Lines WAL,
  fsynced per record, under `REVL_WAL_DIR`, set to `/workspace/app/.revl/wal`.
  Each outside crossing is recorded with its target and arguments.
- **A verdict after the fact.** `revl recover --wal FILE` reads that log with no
  live process and says whether the session committed, and which crossings are
  still out in the world. See [../../docs/crash-recovery.md](../../docs/crash-recovery.md).

## Failure, suspend, crash and deletion

What AX does to the task's processes is read from `runner/runner.go`,
`internal/controller/` and Substrate's `DeleteActor` workflow at the commit AX
pins (`agent-substrate/substrate` `944abe3278b8`). `revl mcp serve` installs no
signal handler, so SIGTERM ends it exactly as SIGKILL does.

| AX event | what happens to the processes | what happens to the WAL | what to do |
|---|---|---|---|
| the agent exits, success or failure | the runner logs the exit status and stays up; the controller never reads it, and the task stays `Running` | intact | nothing: `after-agent.sh` has already run `revl recover`; read `.revl/recovery/*.json` or the task's stdout |
| `ax suspend` | SIGTERM to the command's process group, 10 s grace, then SIGKILL; `/workspace` is snapshotted | kept in the snapshot and restored on resume | nothing runs automatically. On resume the old session is gone; `revl recover` on its WAL would report it as never committed |
| actor crash | the controller reverts the actor to its last snapshot | reverted too: records written after that snapshot are lost while the effects they describe are still out | not covered, see below |
| `ax delete` | the task goes `Terminating`; Substrate terminates the workload, deletes its volumes and releases its snapshots | destroyed | run `revl recover` **before** deleting, or rely on the verdict `after-agent.sh` already wrote to stdout. Nothing inside the sandbox survives deletion |

What the verdict says, measured by the tests:

- **The agent committed.** `verdict: rolled-forward`, no residue, exit 0.
- **The task was killed before commit** (a suspend's grace ran out, a delete, an
  OOM kill). `verdict: rolled-back`, exit 1, and each crossing that left the
  sandbox is listed as still out in the world, with its arguments, for
  example `tickets.file("disk full on build-7")`: "an emission is a one-way
  crossing; it has no inverse".

What `revl recover` does **not** do, and it matters here: the `revl recover`
command evaluates the log against an in-memory model of the world. It does not
call your ticket system, and it does not change the WAL (the test checks both).
Its "ran" and "re-attempted" lines describe that model. To have recovery
execute reconstructible inverses against the real system, a host passes its own
`World` adapter to `revl.recovery.recover(path, world=...)`. For a bare
emission like the one here there is nothing to execute: the verdict tells an
operator exactly what to reconcile by hand.

## What is not covered

- **Effects that do not go through the gate.** revl sees only the calls routed
  to it. If the agent has network access and a credential of its own, it can
  reach the outside world directly, and nothing here notices. Keep other MCP
  servers out of the Workspace and restrict egress at the Substrate level.
- **Credentials the host code uses.** `revl mcp serve` runs inside the sandbox,
  as a child of the agent, with the same user and filesystem. A token the
  provider's host code reads from the environment or a file, the agent can read
  too. Keeping it away from the agent needs the gate outside the sandbox (AX's
  `MCPServer.endpoint`), which needs an MCP transport over the network that
  `revl mcp serve` does not have: it speaks stdio only.
- **The approval is advisory here.** The example serves with
  `--approval-policy advisory`, so the calling identity may answer its own
  class (c) tickets, and the server says so on stderr at startup. Without the
  flag, `revl mcp serve` refuses that (issue #1706): approval then needs a
  second operator identity, which means serving with `--http` and an operator
  profile that grants `approve` only to a human, and a channel for that human
  into the sandbox. This example does not set those up.
- **Crash revert.** When Substrate reverts a crashed actor to its last snapshot,
  the WAL goes back with it, and so the record of crossings after the snapshot
  is lost. A WAL that must survive this has to live outside the actor.
- **Suspend and resume.** A suspended session is not resumed: the process that
  held it is gone and its approvals with it. Resume-safe outside effects are
  later work, not part of this example.
- **Undo.** The example's one outside effect has no inverse, so recovery can
  only name it. A reversible effect would be declared with a checked inverse in
  the operator's provider (see `docs/crash-recovery.md`); this example does not
  demonstrate that.
- **MCP servers you already run.** Putting revl in front of an existing MCP
  server without writing `.rvl` is `revl mcp proxy` (issue #1463), which is not
  released yet. `ax.yaml` has a commented slot for it. Today the route is
  `revl mcp import` on that server's tool list, reviewed and passed as
  `--provider`.

## Running it

No `kind` or `k3d` was available where this example was written, so it has not
been run in a cluster. A run needs:

1. A Kubernetes cluster with [Agent Substrate](https://github.com/agent-substrate/substrate)
   installed (its control API at `api.ate-system.svc.cluster.local:443`, and a
   `gvisor-default` sandbox config, which AX's templates name), and a snapshot
   bucket for the AX controller's `AX_SNAPSHOTS_BUCKET` (the default in AX's
   source is a test bucket of the AX project's, not yours).
2. The AX control plane at the pinned commit: `make deploy
   AX_IMAGE_REPO=<registry>` from a checkout of it (Redis, `ax-server`,
   `ax-controller`, built with `ko`), and the `ax` CLI from the same commit.
3. The task image: `docker build --build-arg REVL_REF=<revl commit> -t
   <registry>/revl-ax-task examples/ax`, pushed, and its digest put in
   `ax.yaml` as `spec.image`. Substrate refuses an image that is not pinned by
   digest.
4. An agent in that image that reads `mcp.servers` from the metadata endpoint
   and starts them. AX's default runner does not write an MCP client
   configuration at this commit (`internal/workspace/setup.go` handles git,
   files, skills and the goal bootstrap, not `mcp`), although AX's docs say a
   runner should. Put its command in `spec.command` after `after-agent.sh`.

Then:

```bash
ax apply -f examples/ax/ax.yaml
ax resume task revl-gated-triage     # tasks are created suspended
ax watch task revl-gated-triage
```

What can be checked without a cluster:

```bash
sh examples/ax/validate.sh                      # AX's schema, needs Go and network
python -m pytest tests/test_examples_ax.py      # revl's side; the last two need cordis-py
```

## Licensing

revl is under two licenses, split by path. [LICENSING.md](../../LICENSING.md)
is the authority; in short:

| part | license | what it covers here |
|---|---|---|
| the compiler, the MCP admission gate, the CLI (`src/revl/`) and the rest of the tooling | **AGPL-3.0-only** | `revl mcp serve` and `revl recover`, which this example runs inside the task image |
| the stdlib, the runtime shims under `backends/` (except the emitters), `examples/` | **MIT** | this directory's files, and the runtime code a compiled revl program carries |
| cordis-py | its own license | a separate project that revl does not relicense |

So, for an adopter:

- Running `revl mcp serve` in a cluster puts an AGPL-3.0-only program behind an
  interface other parties use. Section 13 of the AGPL applies to it: "if you
  modify the Program, your modified version must prominently offer all users
  interacting with it remotely through a computer network" the Corresponding
  Source of that version. An unmodified revl is already published; a modified
  one, including a modified gate, must be offered in source to the users who
  interact with it.
- The image `Dockerfile` builds contains revl. Pushing it where others can pull
  it is conveying AGPL-3.0-only object code, with the source obligations that
  come with that.
- The files in this directory are MIT; copy and adapt them freely.
- "revl" is a trademark; see [TRADEMARK.md](../../TRADEMARK.md). A modified
  gate may not be presented as revl.

This section describes the repository's licensing; it is not legal advice.

## Verified and assumed

| claim | how it was established |
|---|---|
| `ax.yaml` decodes strictly and passes `ValidateTask` / `ValidateWorkspace` | `validate.sh` runs AX's `v1alpha1` package at the pinned commit (3 Go tests, including a control that a misspelled field is rejected) |
| the `mcp` entry and inlined files survive the controller to runner handoff | `validate.sh`, re-encoding the way `marshalWorkspaces` does and decoding the way the runner does |
| `MCPServer` has `name`, `endpoint`, `command`, `args`, and no `cwd` or `env` | `pkg/apis/v1alpha1/ax.proto` |
| `spec.files` does not exist at `v0.3.1` | the `WorkspaceSpec` message at that tag |
| inlined files land under the workspace path before the command starts | `internal/workspace/setup.go`, `runner/runner.go` |
| the default runner does not materialise `mcp` | `internal/workspace/setup.go`; AX's `docs/runner.md` says it should |
| suspend is SIGTERM, 10 s, SIGKILL; the runner stays up after the command exits | `runner/runner.go` |
| tasks are created suspended; delete is two-phase; crashed actors are reverted | `internal/server/server.go`, `internal/controller/worker.go`, `internal/substrate/client.go` |
| `spec.resources` is not applied; no egress policy is set | no reader in AX's Go code at the pinned commit |
| delete removes volumes and snapshots | Substrate `cmd/ateapi/internal/controlapi/workflow_delete.go` at `944abe3278b8` |
| images must be pinned by digest | a comment on Substrate's `Container.image` field; not exercised |
| the revl flags in `ax.yaml` are valid; the gate starts from the manifest's own command | `tests/test_examples_ax.py` |
| what `revl recover` reports after a kill before and after commit | `tests/test_examples_ax.py`, with the pinned cordis-py |
| **assumed:** the base image digest runs the runner at the pinned commit | the digest is the one AX's `examples/task.yaml` uses; AX's own docs name two others |
| **partly assumed:** the `Dockerfile` builds | not built. The same install (a revl wheel, the cordis-py fork at its pin, `watchdog`) in a clean Python 3.12 venv passes `tests/test_examples_ax.py`; the base image and the git URLs were not exercised |
| **assumed:** gVisor, snapshot and egress behaviour as Substrate documents it | read from Substrate's API, not run |
| **assumed:** an agent's MCP client starts `revl-gate` from `command` and `args` as a normal stdio server | how MCP clients generally treat a command server; AX ships no client |
