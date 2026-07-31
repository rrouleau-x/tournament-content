"""Runs the Node wizard-completion tests as part of the pytest suite.

The wizard's completion decision (which checklist fields count as filled
after a possibly-partial create flow) is real JavaScript in app.js. A
Python test can't execute it; this runs the Node harness so the
mid-sequence-failure regression stays guarded in CI. Skips if node is
unavailable (the Python suite must still pass on minimal hosts)."""

import os
import shutil
import subprocess


def test_wizard_ui_node_harness():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([node, os.path.join("tests", "test_wizard_ui.js")],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, f"node wizard tests failed:\n{r.stdout}\n{r.stderr}"
    assert "All wizard completion cases pass" in r.stdout
