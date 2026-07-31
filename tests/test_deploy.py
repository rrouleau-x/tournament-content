"""deploy.py integration tests — the three sacred-shell guarantees plus the
transactional publish flow, all against throwaway local git repos.

Covers (per design review):
  - staged app-shell change aborts before anything is written
  - validation failure writes nothing
  - dry run leaves everything untouched
  - no-op creates no commit
  - only app/data.json appears in the publish commit
  - push failure does not update mirror nor claim success
  - missing manifest blocks deploy
  - draft status blocks deploy
  - successful publish updates remote + mirror
"""

import json
import os

import pytest

from compile import compile_bundle, content_digest, serialize
from conftest import FIXTURE_DIR, FIXTURE_TOURNAMENT, git
from deploy import DeployResult, deploy_tournament
from pipeline import EXIT_OK, PlatformError


def read_remote(workdir, path="app/data.json"):
    """Read a file at origin/main from the local clone, parsed as JSON."""
    return json.loads(git(workdir, "show", f"origin/main:{path}"))


@pytest.fixture()
def valid_target(app_repo):
    return {
        FIXTURE_TOURNAMENT: {
            "repo": "owner/app-repo",
            "appPath": "app/data.json",
            "workDir": app_repo["workdir"],
            "mirrorTo": "",
        }
    }


@pytest.fixture()
def compiled_digest():
    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    return content_digest(serialize(bundle))


def test_staged_shell_change_aborts_before_writing(app_repo, valid_target):
    """The sacred-shell guard: a pre-staged index.html change must abort
    the deploy before ANY write/commit/push."""
    workdir = app_repo["workdir"]
    os.makedirs(os.path.join(workdir, "app"), exist_ok=True)
    with open(os.path.join(workdir, "app", "index.html"), "w") as f:
        f.write("<html>shell</html>\n")
    git(workdir, "add", "app/index.html")  # staged shell change

    before_head = git(workdir, "rev-parse", "HEAD")
    with pytest.raises(PlatformError, match="NOT clean"):
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    assert git(workdir, "rev-parse", "HEAD") == before_head
    # Nothing staged, nothing committed, remote untouched
    assert git(workdir, "status", "--porcelain") != ""  # the staged file remains
    assert read_remote(workdir) == {"seed": True}


def test_validation_failure_writes_nothing(app_repo, valid_target, tmp_path):
    """A broken bundle must abort before the app repo is touched."""
    workdir = app_repo["workdir"]
    # Point at a tournament copy with a blocking error (bad drive format)
    bad_tdir = tmp_path / "bad"
    import shutil
    shutil.copytree(FIXTURE_DIR, bad_tdir)
    with open(os.path.join(bad_tdir, "hotels.json")) as f:
        hotels = json.load(f)
    hotels["hotels"]["official"][0]["drive"] = "7.9 miles away"
    with open(os.path.join(bad_tdir, "hotels.json"), "w") as f:
        json.dump(hotels, f, indent=2)
        f.write("\n")

    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target,
                               tdir=str(bad_tdir))
    assert result.status == "blocked"
    assert result.exit_code == 1
    assert read_remote(workdir) == {"seed": True}
    assert git(workdir, "status", "--porcelain") == ""


def test_dry_run_leaves_everything_untouched(app_repo, valid_target):
    before = git(app_repo["workdir"], "rev-parse", "HEAD")
    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target,
                               dry_run=True)
    assert result.status == "dryrun"
    assert result.changed is True
    assert git(app_repo["workdir"], "rev-parse", "HEAD") == before
    assert git(app_repo["workdir"], "status", "--porcelain") == ""
    assert read_remote(app_repo["workdir"]) == {"seed": True}


def test_noop_creates_no_commit(app_repo, valid_target):
    """After a successful publish, a second run with identical content is a
    no-op — no new commit, exit 0."""
    r1 = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    assert r1.status == "published"
    head_after = git(app_repo["workdir"], "rev-parse", "HEAD")
    r2 = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    assert r2.status == "noop"
    assert r2.exit_code == EXIT_OK
    assert git(app_repo["workdir"], "rev-parse", "HEAD") == head_after


def test_publish_commit_contains_only_data_json(app_repo, valid_target):
    deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    files = git(app_repo["workdir"], "show", "--name-only", "--format=", "HEAD")
    assert files.strip() == "app/data.json"


def test_publish_updates_remote_and_mirror(app_repo, valid_target, tmp_path):
    mirror = tmp_path / "mirror" / "data.json"
    valid_target[FIXTURE_TOURNAMENT]["mirrorTo"] = str(mirror)

    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    assert result.status == "published"

    # Remote has the real content
    remote = read_remote(app_repo["workdir"])
    assert remote["tournament"]["name"] == "Sporting Jax Boys Invitational"

    # Mirror exists and matches remote exactly
    assert os.path.exists(mirror)
    with open(mirror) as f:
        assert json.load(f) == remote

    # No temp files left behind
    assert not any(n.startswith(".data.json.tmp") for n in os.listdir(str(tmp_path / "mirror")))


def test_push_failure_no_mirror_no_success(app_repo, valid_target, tmp_path):
    """A rejected push must not update the mirror and must not claim
    success. Simulated with a pre-push hook that refuses (fetch still
    works, so we exercise the real publish path up to the push)."""
    mirror = tmp_path / "mirror" / "data.json"
    valid_target[FIXTURE_TOURNAMENT]["mirrorTo"] = str(mirror)
    workdir = app_repo["workdir"]

    hooks = os.path.join(workdir, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    hook = os.path.join(hooks, "pre-push")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)

    before = git(workdir, "rev-parse", "HEAD")
    with pytest.raises(PlatformError) as exc:
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    assert "push FAILED" in str(exc.value)
    # Mirror never written
    assert not os.path.exists(mirror)
    # Worktree rolled back: HEAD back at starting commit, index clean
    assert git(workdir, "rev-parse", "HEAD") == before
    assert git(workdir, "status", "--porcelain") == ""
    # Remote untouched
    assert read_remote(workdir) == {"seed": True}


def test_missing_manifest_blocks(app_repo, valid_target, tmp_path):
    """A tournament with no manifest.json must block — the gate can't be
    skipped by deleting metadata."""
    bad_tdir = tmp_path / "nomanifest"
    import shutil
    shutil.copytree(FIXTURE_DIR, bad_tdir)
    os.unlink(os.path.join(bad_tdir, "manifest.json"))

    with pytest.raises(PlatformError, match="manifest"):
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target,
                          tdir=str(bad_tdir))


def test_draft_status_blocks(app_repo, valid_target, tmp_path):
    draft_tdir = tmp_path / "draft"
    import shutil
    shutil.copytree(FIXTURE_DIR, draft_tdir)
    with open(os.path.join(draft_tdir, "manifest.json")) as f:
        m = json.load(f)
    m["status"] = "draft"
    with open(os.path.join(draft_tdir, "manifest.json"), "w") as f:
        json.dump(m, f, indent=2)
        f.write("\n")

    with pytest.raises(PlatformError, match="draft"):
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target,
                          tdir=str(draft_tdir))
    # allow-draft overrides
    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target,
                               allow_draft=True, tdir=str(draft_tdir))
    assert result.status == "published"


def test_unapproved_change_blocks(app_repo, valid_target, tmp_path):
    """Editing content after approval changes the digest — publish must
    block until the new content is approved (digest-tied approval)."""
    import shutil
    from compile import compile_bundle, content_digest, serialize
    tdir = tmp_path / "edited"
    shutil.copytree(FIXTURE_DIR, tdir)
    # Edit a module: rate change
    with open(os.path.join(tdir, "hotels.json")) as f:
        hotels = json.load(f)
    hotels["hotels"]["official"][0]["rate"] = "$150/night"
    with open(os.path.join(tdir, "hotels.json"), "w") as f:
        json.dump(hotels, f, indent=2)
        f.write("\n")

    # Deploy without approving → blocked (digest mismatch vs published revision)
    with pytest.raises(PlatformError, match="digest mismatch"):
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target, tdir=str(tdir))

    # Approve the new digest → publish succeeds
    from pipeline import write_revision, REVISION_APPROVED
    bundle, _, _ = compile_bundle(str(tdir))
    digest = content_digest(serialize(bundle))
    write_revision(str(tdir), REVISION_APPROVED, digest, reviewer="tester")
    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target, tdir=str(tdir))
    assert result.status == "published"
    remote = read_remote(app_repo["workdir"])
    assert remote["hotels"]["official"][0]["rate"] == "$150/night"


def test_missing_revision_blocks(app_repo, valid_target, tmp_path):
    """A manifest with no revision object must block — can't publish content
    that was never approved."""
    import shutil
    tdir = tmp_path / "norev"
    shutil.copytree(FIXTURE_DIR, tdir)
    with open(os.path.join(tdir, "manifest.json")) as f:
        m = json.load(f)
    del m["revision"]
    with open(os.path.join(tdir, "manifest.json"), "w") as f:
        json.dump(m, f, indent=2)
        f.write("\n")

    with pytest.raises(PlatformError, match="approve"):
        deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target, tdir=str(tdir))


def test_result_envelope_shape(app_repo, valid_target):
    result = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
    d = result.to_dict()
    assert set(d) == {"status", "message", "exit_code", "tournament", "digest",
                      "changed", "blocking", "warnings", "destination"}
    assert d["status"] == "published"
    assert d["exit_code"] == 0


def test_concurrent_publish_serializes(app_repo, valid_target):
    """Two simultaneous publishes to the same target must serialize on the
    per-workDir lock: both complete, the remote ends with valid content,
    and no interleaving corrupts the clone. Regression for the deploy
    race the 8.5-review flagged (no mutex around the git critical section
    while the admin server is multithreaded)."""
    import threading

    results = {}

    def worker(tag):
        try:
            r = deploy_tournament(FIXTURE_TOURNAMENT, targets=valid_target)
            results[tag] = ("ok", r.status)
        except Exception as e:  # noqa: BLE001
            results[tag] = ("err", str(e)[:80])

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # Both must succeed (first publishes, second no-ops on identical
    # content) — the lock prevents both passing the clean-check and
    # interleaving writes.
    assert set(results) == {"a", "b"}, results
    for tag, (kind, status) in results.items():
        assert kind == "ok", f"{tag}: {results[tag]}"
        assert status in ("published", "noop"), f"{tag}: {results[tag]}"
    # Exactly one published, one no-op (same content, serialized)
    statuses = sorted(v[1] for v in results.values())
    assert statuses == ["noop", "published"], results

    # Remote is valid, unpolluted content — not an interleaved hybrid
    remote = read_remote(app_repo["workdir"])
    assert remote["tournament"]["name"] == "Sporting Jax Boys Invitational"
    # Worktree clean after both runs
    assert git(app_repo["workdir"], "status", "--porcelain") == ""
