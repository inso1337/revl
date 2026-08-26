// item 318 — per-tool-call H1 proof on the java tier, mirroring
// tests/test_provide_method_witnessed.py. Driven on the cordis4j STUB Context
// (provide/get/effect are all the seam this proof needs — no reactive runtime),
// against REAL files, so the persist-vs-revert outcome is observable end to end.
//
// Fixture: backends/java/scenarios/method_witnessed.rvl (java-owned).
// Emitted classes referenced: revl.Components.{AgentPlugin, Ops, RevlActivation}.

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import java.nio.file.Files;
import java.nio.file.Path;

public final class RunMethodWitnessedH1 {
    private static Path mk(Path dir, int i) throws Exception {
        Path p = dir.resolve("artifact_" + i + ".txt");
        Files.writeString(p, "deliverable " + i);
        return p;
    }

    // the witnessed rename ran: original gone, backup present
    private static boolean mutated(Path p) {
        return !Files.exists(p) && Files.exists(Path.of(p + ".bak"));
    }

    // the world is as it started: original present, no backup residue
    private static boolean pristine(Path p) {
        return Files.exists(p) && !Files.exists(Path.of(p + ".bak"));
    }

    private static void check(boolean cond, String msg) {
        if (!cond) {
            System.err.println("method-witnessed H1: " + msg);
            System.exit(1);
        }
    }

    public static void main(String[] args) throws Exception {
        // 1. per-tool-call witnessed mutations PERSIST on a clean unload (commit)
        {
            Path dir = Files.createTempDirectory("h1-commit");
            Path[] files = { mk(dir, 0), mk(dir, 1), mk(dir, 2) };
            Context root = new Context();
            Disposable activation = new revl.Components.AgentPlugin().apply(root);
            revl.Components.Ops ops = root.get(revl.Components.Ops.class);
            for (Path p : files) {
                ops.touch(p.toString());               // one tool call == one crossing
                check(mutated(p), "the witnessed mutation did not apply on the call");
            }
            activation.dispose();                      // clean unload == implicit commit
            for (Path p : files) {
                check(mutated(p), "clean unload wrongly reverted a per-call mutation");
            }
        }

        // 2 & 3. per-tool-call witnessed mutations REVERT on abort, ALL calls,
        //        residue-free (the world is pristine on every path)
        {
            Path dir = Files.createTempDirectory("h1-abort");
            Path[] files = { mk(dir, 0), mk(dir, 1), mk(dir, 2) };
            Context root = new Context();
            Disposable activation = new revl.Components.AgentPlugin().apply(root);
            revl.Components.Ops ops = root.get(revl.Components.Ops.class);
            for (Path p : files) {
                ops.touch(p.toString());
                check(mutated(p), "the witnessed mutation did not apply on the call");
            }
            // the session rejects its work (item 245's reject drives this seam):
            // the next teardown reverts instead of committing.
            ((revl.Components.RevlActivation) activation).abort();
            activation.dispose();
            for (Path p : files) {
                check(pristine(p), "abort did not revert a per-call mutation residue-free");
            }
        }

        // 4. abort reverts even a single per-call mutation (all-or-nothing edge)
        {
            Path dir = Files.createTempDirectory("h1-one");
            Path p = mk(dir, 0);
            Context root = new Context();
            Disposable activation = new revl.Components.AgentPlugin().apply(root);
            revl.Components.Ops ops = root.get(revl.Components.Ops.class);
            ops.touch(p.toString());
            check(mutated(p), "the witnessed mutation did not apply on the call");
            ((revl.Components.RevlActivation) activation).abort();
            activation.dispose();
            check(pristine(p), "single-call abort must revert");
        }

        System.out.println("METHOD_WITNESSED_H1_OK");
    }
}
