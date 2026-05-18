from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "container-test.yml"
README_PATH = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _step(workflow: dict, name: str) -> dict:
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


def _install_run(workflow: dict) -> str:
    return _step(workflow, "Install SDK and runner deps")["run"]


def _inputs(workflow: dict) -> dict:
    # YAML parses the bare `on` key as boolean True in some PyYAML versions.
    on_block = workflow.get("on") or workflow.get(True)
    return on_block["workflow_dispatch"]["inputs"]


class TestHappyPath:
    def test_lockfile_content_input_is_declared(self, workflow):
        inputs = _inputs(workflow)
        assert "lockfile_content" in inputs
        spec = inputs["lockfile_content"]
        assert spec.get("required", False) is False
        assert spec.get("default", None) == ""
        desc = spec.get("description", "").lower()
        assert "lockfile" in desc or "pip-compile" in desc

    def test_install_step_pipes_lockfile_through_pip_install_dash_r(self, workflow):
        run = _install_run(workflow)
        assert "${{ github.event.inputs.lockfile_content }}" in run
        assert "> /tmp/submission-lockfile.txt" in run
        assert "pip install -r /tmp/submission-lockfile.txt" in run
        assert "--only-binary=:all:" in run
        assert "--no-build-isolation" in run
        assert "--no-cache-dir" in run
        assert "if [ -n" in run
        assert "then" in run
        assert "fi" in run

    def test_sdk_and_baseline_pip_installs_are_unchanged(self, workflow):
        run = _install_run(workflow)
        assert "pip install --upgrade pip" in run
        assert (
            'pip install "git+https://github.com/alexberardi/jarvis-command-sdk.git@${{ github.event.inputs.sdk_ref }}"'
            in run
        )
        assert "pip install pyyaml requests" in run

    def test_other_workflow_inputs_are_untouched(self, workflow):
        inputs = _inputs(workflow)
        assert set(inputs.keys()) == {
            "repo_url",
            "submission_id",
            "callback_url",
            "callback_token",
            "is_bundle",
            "lockfile_content",
            "sdk_ref",
        }

    def test_readme_documents_lockfile_content_input(self):
        readme = README_PATH.read_text()
        assert "lockfile_content" in readme
        assert "packages" not in readme.split("## Workflow inputs")[1].split("##")[0] or "lockfile" in readme
        lower = readme.lower()
        assert "lockfile" in lower or "pip-compile" in lower or "pre-resolved" in lower


class TestEdgeCases:
    def test_old_packages_input_is_removed(self, workflow):
        inputs = _inputs(workflow)
        assert "packages" not in inputs

    def test_install_step_does_not_reference_old_packages_input(self, workflow):
        run = _install_run(workflow)
        assert "github.event.inputs.packages" not in run
        assert "json.load(sys.stdin)" not in run
        assert "pip install $extra" not in run

    def test_install_step_does_not_install_from_source_anywhere_in_extras_path(self, workflow):
        run = _install_run(workflow)
        assert "--no-binary" not in run
        assert "--only-binary=:all:" in run

    def test_lockfile_input_default_is_empty_string(self, workflow):
        inputs = _inputs(workflow)
        assert inputs["lockfile_content"]["default"] == ""

    def test_workflow_permissions_are_unchanged(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}

    def test_harness_and_callback_steps_are_untouched(self, workflow):
        harness = _step(workflow, "Run harness")
        assert harness.get("continue-on-error") is True
        env = harness.get("env", {})
        assert "JARVIS_HARNESS_REPO_DIR" in env
        assert "JARVIS_HARNESS_COMMAND_DIR" in env
        assert "JARVIS_HARNESS_TEST_DIR" in env

        callback = _step(workflow, "Post callback")
        cb_env = callback.get("env", {})
        assert "CALLBACK_URL" in cb_env
        assert "CALLBACK_TOKEN" in cb_env
