#!/bin/sh
# Check ax.yaml against google/ax's own schema code at the pinned commit.
#
# AX has no CRDs: its kinds are protobuf messages (pkg/apis/v1alpha1/ax.proto)
# and `ax apply` decodes each YAML document with the package's UnmarshalYAML,
# which goes through protojson and rejects unknown or misspelled fields. The
# API server then runs ValidateTask / ValidateWorkspace. This script runs that
# same code on ax.yaml, in a throwaway Go module under a temp directory, so no
# go.mod is added to this repository.
#
# Needs Go 1.21 or newer and network access: AX's go.mod asks for Go 1.27.1,
# which the go command downloads when GOTOOLCHAIN=auto (the default).
#
#   sh examples/ax/validate.sh
set -eu

AX_COMMIT=9edb7b1b0ca55f71670da02674b5cd6adeb49b7e
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat > "$work/go.mod" <<'EOF'
module axmanifestcheck

go 1.27.1
EOF

cat > "$work/manifest_test.go" <<'EOF'
package axmanifestcheck

import (
	"bytes"
	"errors"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/google/ax/pkg/apis/v1alpha1"
	"gopkg.in/yaml.v3"
)

// decodeAll splits the manifest the way `ax apply` does (cmd/ax/main.go,
// runApply + applyDocument): skip empty documents, read `kind`, then decode
// into the matching v1alpha1 type through its strict UnmarshalYAML.
func decodeAll(t *testing.T, data []byte) ([]*v1alpha1.Task, []*v1alpha1.Workspace) {
	t.Helper()
	var tasks []*v1alpha1.Task
	var workspaces []*v1alpha1.Workspace
	dec := yaml.NewDecoder(bytes.NewReader(data))
	for i := 1; ; i++ {
		var doc yaml.Node
		if err := dec.Decode(&doc); err != nil {
			if errors.Is(err, io.EOF) {
				return tasks, workspaces
			}
			t.Fatalf("document %d: %v", i, err)
		}
		if doc.Kind == 0 || (doc.Kind == yaml.DocumentNode && len(doc.Content) == 0) {
			continue
		}
		var head struct {
			Kind string `yaml:"kind"`
		}
		if err := doc.Decode(&head); err != nil {
			t.Fatalf("document %d: reading kind: %v", i, err)
		}
		switch head.Kind {
		case v1alpha1.KindTask:
			var task v1alpha1.Task
			if err := doc.Decode(&task); err != nil {
				t.Fatalf("document %d (Task): %v", i, err)
			}
			tasks = append(tasks, &task)
		case v1alpha1.KindWorkspace:
			var ws v1alpha1.Workspace
			if err := doc.Decode(&ws); err != nil {
				t.Fatalf("document %d (Workspace): %v", i, err)
			}
			workspaces = append(workspaces, &ws)
		default:
			t.Fatalf("document %d: unexpected kind %q", i, head.Kind)
		}
	}
}

func manifest(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile(os.Getenv("AX_MANIFEST"))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestManifestDecodesAndValidates(t *testing.T) {
	tasks, workspaces := decodeAll(t, manifest(t))
	if len(tasks) != 1 || len(workspaces) != 1 {
		t.Fatalf("want 1 Task and 1 Workspace, got %d and %d", len(tasks), len(workspaces))
	}
	task, ws := tasks[0], workspaces[0]
	for _, doc := range []struct {
		kind string
		err  error
	}{
		{"Task", v1alpha1.ValidateTask(task)},
		{"Workspace", v1alpha1.ValidateWorkspace(ws)},
	} {
		if doc.err != nil {
			t.Fatalf("%s rejected by the API server's validation: %v", doc.kind, doc.err)
		}
	}

	refs := task.GetSpec().WorkspaceRefs()
	if len(refs) != 1 || refs[0].GetName() != ws.GetMetadata().GetName() {
		t.Fatalf("the Task must bind the Workspace %q, binds %v", ws.GetMetadata().GetName(), refs)
	}
	if got := task.GetSpec().WorkspacePaths()[0]; got != "/workspace/app" {
		t.Fatalf("workspace mount path %q; every path in ax.yaml assumes /workspace/app", got)
	}

	// Agent Substrate refuses a container image that is not pinned by digest
	// (ateapi.proto, Container.image). AX does not check this itself.
	if !strings.Contains(task.GetSpec().GetImage(), "@sha256:") {
		t.Fatalf("image %q is not pinned by digest", task.GetSpec().GetImage())
	}

	var gate *v1alpha1.MCPServer
	for _, s := range ws.GetSpec().GetMcp().GetServers() {
		if s.GetName() == "revl-gate" {
			gate = s
		}
	}
	if gate == nil {
		t.Fatal("no MCP server named revl-gate")
	}
	if gate.GetEndpoint() != "" || gate.GetCommand() == "" {
		t.Fatalf("revl-gate must be a command (stdio) server: %+v", gate)
	}
	if !strings.Contains(strings.Join(gate.GetArgs(), " "), "revl mcp serve") {
		t.Fatalf("revl-gate does not run `revl mcp serve`: %v", gate.GetArgs())
	}

	var proxy *v1alpha1.MCPServer
	for _, s := range ws.GetSpec().GetMcp().GetServers() {
		if s.GetName() == "revl-proxy" {
			proxy = s
		}
	}
	if proxy == nil {
		t.Fatal("no MCP server named revl-proxy")
	}
	if proxy.GetEndpoint() != "" || proxy.GetCommand() == "" {
		t.Fatalf("revl-proxy must be a command (stdio) server: %+v", proxy)
	}
	proxyArgs := strings.Join(proxy.GetArgs(), " ")
	if !strings.Contains(proxyArgs, "revl mcp proxy") {
		t.Fatalf("revl-proxy does not run `revl mcp proxy`: %v", proxy.GetArgs())
	}
	// issue #1463 landed, so this entry is live: it names its upstream after
	// `--` and declares the inverse that makes close_ticket witnessed.
	if !strings.Contains(proxyArgs, "-- python3 tickets-mcp.py") {
		t.Fatalf("revl-proxy does not name its upstream after `--`: %v", proxy.GetArgs())
	}
	if !strings.Contains(proxyArgs, "--undo close_ticket=reopen_ticket") {
		t.Fatalf("revl-proxy does not declare an undo: %v", proxy.GetArgs())
	}
	if strings.Contains(proxyArgs, "--trust-read-only-hints") {
		t.Fatalf("revl-proxy trusts read-only hints; the example's point is that it does not: %v", proxy.GetArgs())
	}
}

// The controller hands bound workspaces to the runner as AX_WORKSPACES_YAML
// (internal/controller/reconciler.go, marshalWorkspaces), and the runner reads
// them back (cmd/ax-task-runner/main.go, loadWorkspaces). The MCP entry and the
// inlined files must survive that trip unchanged.
func TestWorkspaceSurvivesTheRunnerHandoff(t *testing.T) {
	_, workspaces := decodeAll(t, manifest(t))
	var sb strings.Builder
	enc := yaml.NewEncoder(&sb)
	for _, ws := range workspaces {
		if err := enc.Encode(ws); err != nil {
			t.Fatal(err)
		}
	}
	if err := enc.Close(); err != nil {
		t.Fatal(err)
	}
	var back v1alpha1.Workspace
	if err := yaml.NewDecoder(strings.NewReader(sb.String())).Decode(&back); err != nil {
		t.Fatalf("runner-side decode: %v", err)
	}
	orig := workspaces[0]
	if len(back.GetSpec().GetFiles()) != len(orig.GetSpec().GetFiles()) {
		t.Fatalf("files lost in the handoff")
	}
	for i, f := range orig.GetSpec().GetFiles() {
		if back.GetSpec().GetFiles()[i].GetContent() != f.GetContent() {
			t.Fatalf("file %s changed in the handoff", f.GetPath())
		}
	}
	got := back.GetSpec().GetMcp().GetServers()
	want := orig.GetSpec().GetMcp().GetServers()
	if len(got) != len(want) {
		t.Fatalf("mcp servers lost in the handoff: %d != %d", len(got), len(want))
	}
	for i := range want {
		if got[i].GetName() != want[i].GetName() ||
			got[i].GetCommand() != want[i].GetCommand() ||
			got[i].GetEndpoint() != want[i].GetEndpoint() ||
			strings.Join(got[i].GetArgs(), "\x00") != strings.Join(want[i].GetArgs(), "\x00") {
			t.Fatalf("mcp server %d changed in the handoff: %v != %v", i, got[i], want[i])
		}
	}
}

// A control: the decoder above must be strict, or passing it proves nothing.
func TestAMisspelledFieldIsRejected(t *testing.T) {
	bad := strings.Replace(string(manifest(t)), "command: sh", "commnad: sh", 1)
	if bad == string(manifest(t)) {
		t.Fatal("control did not change the manifest")
	}
	var ws v1alpha1.Workspace
	dec := yaml.NewDecoder(strings.NewReader(bad))
	var err error
	for {
		var doc yaml.Node
		if derr := dec.Decode(&doc); derr != nil {
			break
		}
		var head struct {
			Kind string `yaml:"kind"`
		}
		_ = doc.Decode(&head)
		if head.Kind == v1alpha1.KindWorkspace {
			err = doc.Decode(&ws)
		}
	}
	if err == nil {
		t.Fatal("a misspelled MCPServer field was accepted; the check is vacuous")
	}
}
EOF

cd "$work"
go get "github.com/google/ax@${AX_COMMIT}"
go mod tidy
AX_MANIFEST="$here/ax.yaml" go test -count=1 -v ./...
