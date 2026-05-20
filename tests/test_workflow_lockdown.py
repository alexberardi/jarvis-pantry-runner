from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "container-test.yml"
README_PATH = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _step(workflow: dict, name: str, job: str = "test") -> dict:
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in job {job!r}")


def _pre_stage_run(workflow: dict) -> str:
    """The pre-stage step is where SDK + submission lockfile are installed,
    inside a docker volume, before the sandboxed harness runs."""
    return _step(workflow, "Pre-stage sandbox image and deps")["run"]


def _sandbox_run(workflow: dict) -> str:
    return _step(workflow, "Run harness in sandbox")["run"]


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
        step = _step(workflow, "Pre-stage sandbox image and deps")
        run = step["run"]
        # Lockfile content reaches the install via a workflow input → env var,
        # written to a file, then `pip install -r <file>` against it. The
        # ${{ ... }} interpolation may land in either `run` or the step's
        # `env:` block, depending on how the indirection is wired.
        env_values = " ".join(step.get("env", {}).values())
        assert "${{ github.event.inputs.lockfile_content }}" in run + env_values
        assert "submission-lockfile.txt" in run
        assert "pip install" in run and "-r " in run and "submission-lockfile.txt" in run
        assert "--only-binary=:all:" in run
        assert "--no-build-isolation" in run
        assert "--no-cache-dir" in run
        # Guarded behind an emptiness check so an empty lockfile doesn't
        # trigger a spurious `pip install -r`.
        assert "if [" in run
        assert "fi" in run

    def test_sdk_install_pinned_to_input_ref(self, workflow):
        run = _pre_stage_run(workflow)
        assert "git+https://github.com/alexberardi/jarvis-command-sdk.git@" in run
        # The ref must come from the sdk_ref input — either via direct
        # interpolation or an env-var indirection set from that input.
        env = _step(workflow, "Pre-stage sandbox image and deps").get("env", {})
        sdk_ref_source = env.get("SDK_REF", "")
        assert "${{ github.event.inputs.sdk_ref }}" in run or "${{ github.event.inputs.sdk_ref }}" in sdk_ref_source

    def test_baseline_runtime_deps_present(self, workflow):
        run = _pre_stage_run(workflow)
        assert "pyyaml" in run
        assert "requests" in run

    def test_other_workflow_inputs_are_untouched(self, workflow):
        # `nonce` replaces `callback_token` per #26 — the rest are unchanged
        # from the prior shape this file was guarding.
        inputs = _inputs(workflow)
        assert set(inputs.keys()) == {
            "repo_url",
            "submission_id",
            "callback_url",
            "nonce",
            "is_bundle",
            "lockfile_content",
            "sdk_ref",
        }

    def test_readme_documents_lockfile_content_input(self):
        readme = README_PATH.read_text()
        assert "lockfile_content" in readme
        lower = readme.lower()
        assert "lockfile" in lower or "pip-compile" in lower or "pre-resolved" in lower


class TestEdgeCases:
    def test_old_packages_input_is_removed(self, workflow):
        inputs = _inputs(workflow)
        assert "packages" not in inputs

    def test_install_step_does_not_reference_old_packages_input(self, workflow):
        run = _pre_stage_run(workflow)
        assert "github.event.inputs.packages" not in run
        assert "json.load(sys.stdin)" not in run
        assert "pip install $extra" not in run

    def test_install_step_does_not_install_from_source_anywhere_in_extras_path(self, workflow):
        run = _pre_stage_run(workflow)
        assert "--no-binary" not in run
        assert "--only-binary=:all:" in run

    def test_lockfile_input_default_is_empty_string(self, workflow):
        inputs = _inputs(workflow)
        assert inputs["lockfile_content"]["default"] == ""

    def test_workflow_permissions_are_unchanged(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}

    def test_harness_step_contract_unchanged(self, workflow):
        # Harness step lives in the `test` job; env-var contract still gets to
        # the harness process via docker `-e`. Callback step shape is now
        # covered by tests/test_workflow_callback_isolation.py since it lives
        # in its own job after the #26 split.
        harness = _step(workflow, "Run harness in sandbox")
        assert harness.get("continue-on-error") is True
        run = harness["run"]
        assert "JARVIS_HARNESS_REPO_DIR" in run
        assert "JARVIS_HARNESS_COMMAND_DIR" in run
        assert "JARVIS_HARNESS_TEST_DIR" in run


class TestSandbox:
    """Sandbox invariants (#11): the harness must run inside a docker
    container with no network, a read-only rootfs, and a memory cap, so a
    malicious submission can't exfiltrate the callback token or chew
    runner resources during run()."""

    def test_harness_runs_inside_docker(self, workflow):
        run = _sandbox_run(workflow)
        assert "docker run" in run

    def test_sandbox_has_no_network(self, workflow):
        assert "--network=none" in _sandbox_run(workflow)

    def test_sandbox_rootfs_is_read_only(self, workflow):
        assert "--read-only" in _sandbox_run(workflow)

    def test_sandbox_has_memory_cap(self, workflow):
        assert "--memory=128m" in _sandbox_run(workflow)

    def test_sandbox_provides_writable_tmpfs(self, workflow):
        # Read-only rootfs would otherwise block Python's transient writes.
        assert "--tmpfs /tmp" in _sandbox_run(workflow)

    def test_submission_is_mounted_read_only(self, workflow):
        run = _sandbox_run(workflow)
        assert "/submission:ro" in run

    def test_runner_scripts_mounted_read_only(self, workflow):
        run = _sandbox_run(workflow)
        assert "/runner:ro" in run

    def test_deps_volume_mounted_read_only_into_sandbox(self, workflow):
        # Deps are populated in the pre-stage step (network on) and the
        # sandbox sees them read-only — submitted code can't poison them.
        run = _sandbox_run(workflow)
        assert "harness-deps:/deps:ro" in run

    def test_sandbox_has_no_pip_install(self, workflow):
        # All installs happen in the pre-stage step. The sandbox must not
        # reach for the network even for SDK install.
        assert "pip install" not in _sandbox_run(workflow)
