"""`revl update check|fetch|status` (docs/updates.md)."""

from __future__ import annotations

import json

from _update_feed_fixture import serve_feed
from revl.__main__ import main


def _args(feed, command, *extra):
    return ["update", command, "--repo", "owner/app", "--project", "app",
            "--api-base", feed.base_url, *extra]


def test_check_reports_an_available_update_and_exits_10(capsys, monkeypatch):
    monkeypatch.delenv("REVL_UPDATE_API_BASE", raising=False)
    feed = serve_feed(version="9.9.9")
    try:
        rc = main(_args(feed, "check", "--json"))
    finally:
        feed.close()
    out = capsys.readouterr().out
    assert rc == 10
    report = json.loads(out)
    assert report["ok"] is True
    assert report["updateAvailable"] is True
    assert report["bump"] == "major"
    assert report["feed"] == {"repo": "owner/app", "channel": "stable"}
    assert report["release"]["version"] == "9.9.9"


def test_check_reports_up_to_date_and_exits_0(capsys):
    feed = serve_feed(version="0.0.1")
    try:
        rc = main(_args(feed, "check"))
    finally:
        feed.close()
    out = capsys.readouterr().out
    assert rc == 0
    assert "up to date" in out


def test_fetch_stages_the_release_and_status_reports_it(capsys, tmp_path):
    feed = serve_feed(version="9.9.9")
    try:
        rc = main(_args(feed, "fetch", "--home", str(tmp_path), "--json"))
        out = capsys.readouterr().out
        assert rc == 0
        report = json.loads(out)
        staged = report["staged"]
        assert report["release"]["version"] == "9.9.9"
        assert report["staged"]["sources"]["count"] == 1
        assert (tmp_path / "staged").is_dir()
        rc = main(["update", "status", "--home", str(tmp_path), "--json"])
        status = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert len(status["staged"]) == 1
        assert status["staged"][0]["version"] == "9.9.9"
        assert staged["directory"]
    finally:
        feed.close()


def test_an_unreachable_feed_is_exit_2_with_a_named_refusal(capsys):
    rc = main(["update", "check", "--repo", "owner/app", "--project", "app",
               "--api-base", "http://127.0.0.1:9", "--json"])
    out = capsys.readouterr().out
    assert rc == 2
    refusal = json.loads(out)["refusal"]
    assert refusal["code"] == "FEED_UNREACHABLE"


def test_status_on_a_fresh_home_is_empty(capsys, tmp_path):
    rc = main(["update", "status", "--home", str(tmp_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["staged"] == []
    assert report["journal"] == []
    assert report["current"]["version"]


def test_a_missing_manifest_path_is_a_named_refusal_exit_2(capsys, tmp_path):
    missing = tmp_path / "no-such-release.json"
    rc = main(["update", "check", "--manifest", str(missing)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "MANIFEST_UNREADABLE" in err
