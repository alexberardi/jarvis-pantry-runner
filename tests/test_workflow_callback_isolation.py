"""Two-job split + HMAC-input shape (#26).

The callback job holds the signing key. The test job runs submitted code.
The whole point of splitting is that those two privileges never meet — if
this file ever fails because someone moved a step across the line, that's
exactly the kind of regression we want to catch.
"""

from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "container-test.yml"
)
README_PATH = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _inputs(workflow: dict) -> dict:
    # YAML parses the bare `on` key as boolean True in some PyYAML versions.
    on_block = workflow.get("on") or workflow.get(True)
    return on_block["workflow_dispatch"]["inputs"]


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def _step(workflow: dict, job: str, name: str) -> dict:
    for step in _steps(workflow, job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in job {job!r}")


def _all_step_text(workflow: dict, job: str) -> str:
    chunks: list[str] = []
    for step in _steps(workflow, job):
        for key in ("run", "uses", "with", "env"):
            v = step.get(key)
            if isinstance(v, str):
                chunks.append(v)
            elif isinstance(v, dict):
                chunks.append(repr(v))
    return "\n".join(chunks)


class TestJobsStructure:
    def test_two_jobs_test_and_callback(self, workflow):
        jobs = workflow["jobs"]
        assert set(jobs.keys()) == {"test", "callback"}

    def test_callback_depends_on_test(self, workflow):
        assert workflow["jobs"]["callback"]["needs"] == "test"

    def test_callback_runs_even_when_test_fails(self, workflow):
        # `if: always()` so a failed harness still posts a passed=false result.
        assert workflow["jobs"]["callback"]["if"] == "${{ always() }}" \
            or workflow["jobs"]["callback"]["if"] == "always()"


class TestCallbackPrivilegeIsolation:
    """The signing key lives in the GHA environment `pantry-callback`.
    Anything inside the callback job has access; anything outside it does not.
    """

    def test_callback_job_is_environment_gated(self, workflow):
        assert workflow["jobs"]["callback"]["environment"] == "pantry-callback"

    def test_test_job_is_not_environment_gated(self, workflow):
        # The test job runs submitted code, so it must NOT inherit access to
        # `pantry-callback` secrets. (No `environment:` key at all.)
        assert "environment" not in workflow["jobs"]["test"]

    def test_signing_key_only_referenced_in_callback_job(self, workflow):
        test_text = _all_step_text(workflow, "test")
        callback_text = _all_step_text(workflow, "callback")
        assert "PANTRY_CALLBACK_SIGNING_KEY" not in test_text
        assert "PANTRY_CALLBACK_SIGNING_KEY" in callback_text


class TestNoSubmittedCodeInCallbackJob:
    """If submitted code can run in the callback job it can exfil the signing
    key — the entire #25/#26 design exists to prevent that. Lock it down."""

    def test_callback_job_does_not_clone_submission_repo(self, workflow):
        text = _all_step_text(workflow, "callback")
        assert "git clone" not in text
        assert "github.event.inputs.repo_url" not in text

    def test_callback_job_does_not_run_harness(self, workflow):
        text = _all_step_text(workflow, "callback")
        assert "harness.py" not in text
        # Also: no `docker run` of the submitted code.
        assert "docker run" not in text

    def test_callback_job_does_not_install_submitted_dependencies(self, workflow):
        """Lockfile install / submission-lockfile interactions stay in `test`.
        The callback job only installs trusted top-level pip packages from
        the runner repo's own deps (currently just `requests`)."""
        text = _all_step_text(workflow, "callback")
        assert "lockfile_content" not in text
        assert "submission-lockfile" not in text


class TestSubmittedCodeStaysInTestJob:
    def test_lockfile_install_step_is_in_test_job(self, workflow):
        step = _step(workflow, "test", "Pre-stage sandbox image and deps")
        run = step["run"]
        # The lockfile-driven pip install must live in test, not callback.
        assert "submission-lockfile.txt" in run
        assert "pip install" in run

    def test_harness_run_step_is_in_test_job(self, workflow):
        step = _step(workflow, "test", "Run harness in sandbox")
        assert "docker run" in step["run"]
        assert "harness.py" in step["run"]


class TestInputShape:
    def test_callback_token_is_removed_from_inputs(self, workflow):
        inputs = _inputs(workflow)
        assert "callback_token" not in inputs

    def test_nonce_is_a_required_input(self, workflow):
        inputs = _inputs(workflow)
        assert "nonce" in inputs
        assert inputs["nonce"]["required"] is True

    def test_input_set_is_exactly(self, workflow):
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


class TestArtifactHandoff:
    """Test job hands the harness result to callback job via an artifact —
    that's the only data channel between them."""

    def test_test_job_uploads_artifact(self, workflow):
        step = _step(workflow, "test", "Upload harness artifact")
        assert "actions/upload-artifact" in step["uses"]
        assert step.get("if") == "${{ always() }}" or step.get("if") == "always()"

    def test_callback_job_downloads_artifact(self, workflow):
        step = _step(workflow, "callback", "Download harness artifact")
        assert "actions/download-artifact" in step["uses"]


class TestReadmeDocumentsTheSplit:
    def test_readme_mentions_two_job_model(self):
        readme = README_PATH.read_text().lower()
        assert "callback" in readme
        assert "test" in readme
        assert "pantry-callback" in readme

    def test_readme_documents_signing_key_setup(self):
        readme = README_PATH.read_text()
        assert "PANTRY_CALLBACK_SIGNING_KEY" in readme
