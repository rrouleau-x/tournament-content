"""Shared fixtures for the pipeline test suite.

All tests run against temporary directories — no real GitHub remotes, no
touching the live content repo or the app repo. The committed fixture
tournament (sporting-jax-2026) is copied per test and mutated as needed;
deploy tests build throwaway bare repos + clones and pass explicit
targets/workdir overrides so nothing outside the temp dirs is touched.
"""

import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
FIXTURE_TOURNAMENT = "savannah-united/sporting-jax-2026"
FIXTURE_DIR = os.path.join(REPO_ROOT, "orgs", "savannah-united",
                           "tournaments", "sporting-jax-2026")

sys.path.insert(0, SCRIPTS)


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture()
def tournament_copy(tmp_path):
    """A scratch copy of the fixture tournament modules (safe to mutate)."""
    dst = tmp_path / "sporting-jax-2026"
    shutil.copytree(FIXTURE_DIR, dst)
    return str(dst)


@pytest.fixture()
def app_repo(tmp_path):
    """A throwaway app repo: bare origin + clone, main seeded with a
    placeholder data.json. Returns a dict of paths."""
    base = tmp_path / "app"
    origin = base / "origin.git"
    workdir = base / "work"
    origin.mkdir(parents=True)
    git(str(base), "init", "--bare", str(origin))
    git(str(base), "clone", str(origin), str(workdir))
    git(workdir, "config", "user.email", "test@example.com")
    git(workdir, "config", "user.name", "Test")
    os.makedirs(os.path.join(workdir, "app"), exist_ok=True)
    with open(os.path.join(workdir, "app", "data.json"), "w") as f:
        f.write('{"seed": true}\n')
    git(workdir, "add", "app/data.json")
    git(workdir, "commit", "-m", "seed")
    git(workdir, "branch", "-M", "main")
    git(workdir, "push", "-u", "origin", "main")
    return {"origin": str(origin), "workdir": str(workdir)}


@pytest.fixture()
def targets(app_repo):
    """A publish target dict pointing at the throwaway app repo."""
    return {
        FIXTURE_TOURNAMENT: {
            "repo": "owner/app-repo",
            "appPath": "app/data.json",
            "workDir": app_repo["workdir"],
            "mirrorTo": "",
        }
    }
