"""`revl migrate --coexistence`: the view with both versions live (issue #2685).

The plan asks what happens when the old version is REPLACED. This view asks the
other question a reviewer has before a canary: can the new version run beside
the old one at all, and what does the composition hold while it does? It reads
the same compiled documents and answers three things:

  * G2 per `(key, realm)`: a key is provided once per realm, so a new version
    that provides a key the old one already provides in the same realm cannot be
    live beside it; placed in another realm (its own `isolate`) it can;
  * which consumers each version serves, since a consumer reads the provider in
    its own realm;
  * the authority held during the overlap, which is everything held before and
    everything held after, with what the migration adds and retires named.

It is a view: nothing is admitted, swapped or written, and a plan without the
flag keeps the shape it had.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.migrate import plan  # noqa: E402


V1 = """
service Cache { fn get(key: Str) -> Str }
component CacheImpl provides cache: Cache {
  provide cache { fn get(key) = key }
}
"""

# v2 declared in the same slot under the same name: it replaces v1, it cannot
# be live beside it
V2_SAME_SLOT = """
service Cache {
  fn get(key: Str) -> Str
  fn size() -> Int
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn size() = 0
  }
}
"""

# v2 declared into a canary realm under its own name
V2_CANARY = """
service Cache {
  fn get(key: Str) -> Str
  fn size() -> Int
}
component CacheCanary provides cache: Cache {
  isolate cache in realm("canary")
  provide cache {
    fn get(key) = key
    fn size() = 0
  }
}
"""

# v2 under another key altogether
V2_OTHER_KEY = """
service Cache {
  fn get(key: Str) -> Str
  fn size() -> Int
}
component CacheTwo provides cache2: Cache {
  provide cache2 {
    fn get(key) = key
    fn size() = 0
  }
}
"""

CONSUMERS = """
component Shared requires cache: Cache { }
component Tenant requires cache: Cache { isolate cache in realm("canary") }
"""
AGAINST = V1 + CONSUMERS


def _overlap(new_src: str, against_src: str = AGAINST, old_src: str = V1) -> dict:
    result = plan(compile_source(old_src), compile_source(new_src),
                  compile_source(against_src), overlap=True)
    return result["coexistence"]


# ------------------------------------------------------------- G2 per realm

def test_the_same_slot_and_name_cannot_be_live_twice():
    view = _overlap(V2_SAME_SLOT)
    assert view["possible"] is False
    assert view["g2Holds"] is False
    [slot] = view["g2"]
    assert (slot["key"], slot["realm"], slot["holds"]) == ("cache", "", False)
    assert [(p["component"], p["version"]) for p in slot["providers"]] == [
        ("CacheImpl", "old"), ("CacheImpl", "new")]
    # one component per name: the new version needs another name as well
    assert view["nameCollisions"] == ["CacheImpl"]
    # the realms the composition already has are offered as places to put it
    assert view["existingRealms"] == ["canary"]


def test_a_version_placed_in_its_own_realm_coexists():
    view = _overlap(V2_CANARY)
    assert view["possible"] is True
    assert view["g2Holds"] is True
    assert view["nameCollisions"] == []
    [slot] = view["g2"]
    assert (slot["key"], slot["realm"], slot["holds"]) == ("cache", "canary", True)
    assert [(p["component"], p["version"]) for p in slot["providers"]] == [
        ("CacheCanary", "new")]


def test_a_version_under_another_key_coexists():
    view = _overlap(V2_OTHER_KEY)
    assert view["possible"] is True
    assert [(s["key"], s["holds"]) for s in view["g2"]] == [("cache2", True)]


def test_g2_is_checked_per_realm_not_per_key():
    # the old version already runs in the canary realm too; the new version in
    # that realm conflicts there while the shared slot is untouched
    old = V1 + """
component CacheCanaryOld provides cache: Cache {
  isolate cache in realm("canary")
  provide cache { fn get(key) = key }
}
"""
    against = old + CONSUMERS
    view = _overlap(V2_CANARY, against, old)
    [slot] = view["g2"]
    assert (slot["key"], slot["realm"], slot["holds"]) == ("cache", "canary", False)
    assert [p["version"] for p in slot["providers"]] == ["old", "new"]


# ------------------------------------------------------- who serves whom

def test_each_consumer_is_served_by_the_provider_in_its_own_realm():
    view = _overlap(V2_CANARY)
    serving = {(c["component"], c["realm"]): c["servedBy"]
               for c in view["serving"]}
    # the shared consumer stays on the old version; the tenant consumer, which
    # only the canary realm provides, is served by the new one
    assert serving == {("Shared", ""): ["old"], ("Tenant", "canary"): ["new"]}


def test_a_conflicting_slot_is_marked_so_in_serving():
    view = _overlap(V2_SAME_SLOT)
    [shared] = [c for c in view["serving"] if c["component"] == "Shared"]
    assert shared["servedBy"] == ["new", "old"]
    assert shared["conflict"] is True
    assert view["possible"] is False


def test_a_retained_provider_that_conflicts_with_the_new_one_is_named():
    # a component the migration does not replace already provides `cache` in the
    # canary realm; the new version placed there conflicts with it, and the
    # serving row says so instead of dropping the retained provider
    against = AGAINST + """
component KeptCanary provides cache: Cache {
  isolate cache in realm("canary")
  provide cache { fn get(key) = key }
}
"""
    view = _overlap(V2_CANARY, against)
    [slot] = view["g2"]
    assert (slot["realm"], slot["holds"]) == ("canary", False)
    assert [p["version"] for p in slot["providers"]] == ["retained", "new"]
    [tenant] = [c for c in view["serving"] if c["component"] == "Tenant"]
    assert tenant["servedBy"] == ["new", "retained"]
    assert tenant["conflict"] is True


def test_the_replaced_components_are_not_consumers_of_the_overlap():
    # the old version spans two components; the helper requires `cache`, but it
    # is being replaced, so it is not listed as a consumer
    old = V1 + """
service Helper { fn help() -> Str }
component CacheHelper requires cache: Cache provides helper: Helper {
  provide helper { fn help() = "h" }
}
"""
    against = old + CONSUMERS
    view = _overlap(V2_CANARY, against, old)
    assert [c["component"] for c in view["serving"]] == ["Shared", "Tenant"]


def test_a_provider_that_isolates_only_some_keys_is_read_per_key():
    old = """
service Cache { fn get(key: Str) -> Str }
service Meta { fn tag() -> Str }
component CacheImpl provides cache: Cache, meta: Meta {
  isolate cache in realm("x")
  provide cache { fn get(key) = key }
  provide meta { fn tag() = "t" }
}
"""
    new = """
service Cache { fn get(key: Str) -> Str }
service Meta { fn tag() -> Str }
component CacheNext provides cache: Cache, meta: Meta {
  isolate cache in realm("y")
  provide cache { fn get(key) = key }
  provide meta { fn tag() = "t" }
}
"""
    view = _overlap(new, old, old)
    slots = {(s["key"], s["realm"]): s["holds"] for s in view["g2"]}
    # `cache` is placed in realm y beside the old one in x: fine. `meta` is
    # provided by both in the shared realm: the only conflict.
    assert slots == {("cache", "y"): True, ("meta", ""): False}
    assert view["possible"] is False


def test_a_routed_consumer_is_read_in_each_routed_realm():
    # `R` binds `cache` across realms a and b. The old version provides b; the
    # new one is declared into b as well. R must be read in a and b, not in the
    # shared realm where nothing provides `cache`.
    old = """
service Cache { fn get(key: Str) -> Str }
component CacheB provides cache: Cache {
  isolate cache in realm("b")
  provide cache { fn get(key) = key }
}
"""
    against = old + """
component KeptA provides cache: Cache {
  isolate cache in realm("a")
  provide cache { fn get(key) = key }
}
component R requires cache: Cache { isolate cache in realms("a", "b") }
"""
    new = """
service Cache { fn get(key: Str) -> Str }
component CacheB2 provides cache: Cache {
  isolate cache in realm("b")
  provide cache { fn get(key) = key }
}
"""
    view = _overlap(new, against, old)
    assert [(c["component"], c["realm"], c["servedBy"], c["conflict"])
            for c in view["serving"]] == [("R", "b", ["new", "old"], True)]
    assert view["existingRealms"] == ["a", "b"]


# ------------------------------------------------- authority in the overlap

SERVICES = """
service Agent { emission[llm, mail] fn run() -> Str }
service LLM { emission[llm] fn ask(prompt: Str) -> Str }
service Mail { emission[mail] fn send(to: Str) }
"""
MODEL_ONLY = SERVICES + """
component AgentImpl requires llm: LLM provides agent: Agent {
  provide agent {
    fn run() {
      emit llm.ask("summarize")
      return "ok"
    }
  }
}
"""
MODEL_AND_MAIL = SERVICES + """
component AgentImpl requires llm: LLM, mail: Mail provides agent: Agent {
  provide agent {
    fn run() {
      emit llm.ask("summarize")
      emit mail.send("x@example.com")
      return "ok"
    }
  }
}
"""
FRONT = "component Front requires agent: Agent { }\n"


def test_authority_during_the_overlap_is_before_united_with_after():
    view = _overlap(MODEL_AND_MAIL, MODEL_ONLY + FRONT, MODEL_ONLY)
    auth = view["authority"]
    assert auth["added"] == ["emit:AgentImpl:mail.send"]
    assert auth["retired"] == []
    assert set(auth["held"]) == {"emit:AgentImpl:llm.ask",
                                 "emit:AgentImpl:mail.send"}
    assert (auth["before"], auth["after"], auth["overlap"]) == (1, 2, 2)


def test_what_the_migration_gives_up_is_held_only_during_the_overlap():
    view = _overlap(MODEL_ONLY, MODEL_AND_MAIL + FRONT, MODEL_AND_MAIL)
    auth = view["authority"]
    assert auth["retired"] == ["emit:AgentImpl:mail.send"]
    assert auth["added"] == []
    # while both are live the composition still holds the retired crossing
    assert "emit:AgentImpl:mail.send" in auth["held"]
    assert (auth["before"], auth["after"], auth["overlap"]) == (2, 1, 2)


def test_held_is_everything_before_united_with_everything_after():
    # the old and new versions use different component names, so before, after
    # and the overlap are three different sets
    renamed = MODEL_AND_MAIL.replace("AgentImpl", "AgentNext")
    view = _overlap(renamed, MODEL_ONLY + FRONT, MODEL_ONLY)
    auth = view["authority"]
    assert auth["retired"] == ["emit:AgentImpl:llm.ask"]
    assert set(auth["added"]) == {"emit:AgentNext:llm.ask",
                                  "emit:AgentNext:mail.send"}
    assert auth["overlap"] == auth["before"] + len(auth["added"])
    assert auth["overlap"] == auth["after"] + len(auth["retired"])
    assert set(auth["held"]) == (
        {"emit:AgentImpl:llm.ask"} | set(auth["added"]))


# ---------------------------------------------------------------- the CLI

def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_cli_coexistence_is_in_the_plan_only_when_asked(tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", V1)
    new = _write(tmp_path, "new.rvl", V2_CANARY)
    against = _write(tmp_path, "against.rvl", AGAINST)

    def tree() -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(tmp_path.iterdir())}

    before = tree()
    args = ["migrate", str(old), str(new), "--against", str(against)]
    assert main(args + ["--json"]) == 0
    assert "coexistence" not in json.loads(capsys.readouterr().out)

    assert main(args + ["--coexistence", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["coexistence"]["possible"] is True

    assert main(args + ["--coexistence"]) == 0
    human = capsys.readouterr().out
    assert "coexistence (both versions live" in human
    assert "G2 `cache` in realm `canary`: ok" in human
    assert "consumer Tenant (`cache` in realm `canary`) served by: new" in human
    assert tree() == before


def test_cli_a_conflict_says_how_to_make_room(tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", V1)
    new = _write(tmp_path, "new.rvl", V2_SAME_SLOT)
    against = _write(tmp_path, "against.rvl", AGAINST)
    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--coexistence"]) == 0
    human = capsys.readouterr().out
    assert "possible: no" in human
    assert "G2 `cache`: CONFLICT" in human
    assert "a canary slice, `revl canary`" in human
    assert "name collision: `CacheImpl`" in human


def test_the_view_composes_with_the_policy(tmp_path, capsys):
    # both flags together: the plan carries both blocks
    old = _write(tmp_path, "old.rvl", MODEL_ONLY)
    new = _write(tmp_path, "new.rvl", MODEL_AND_MAIL)
    against = _write(tmp_path, "against.rvl", MODEL_ONLY + FRONT)
    policy = _write(tmp_path, "p.policy", "component Agent* may reach llm\n")
    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--policy", str(policy), "--coexistence", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["classification"] == "refused"
    assert doc["coexistence"]["authority"]["added"] == [
        "emit:AgentImpl:mail.send"]
