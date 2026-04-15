# jarvis-pantry-runner

Public, one-purpose repo: runs Jarvis package test harnesses on behalf of
[jarvis-pantry](https://github.com/alexberardi/jarvis-pantry). When a user
submits a package, pantry dispatches the `container-test` workflow here with
the repo URL and a one-time callback token. The workflow checks out the
submission, runs the SDK test harness against it, and POSTs the result back
to pantry.

**You shouldn't need to edit anything in here manually.** All runtime
parameters are passed as `workflow_dispatch` inputs.

## Why a separate repo?

Fly machines can't run Docker-in-Docker, so pantry can't sandbox untrusted
submission code on its own infra. GitHub Actions gives us a clean, isolated
VM per submission for free.

## Workflow inputs

| Input | Description |
|-------|-------------|
| `repo_url` | HTTPS URL of the submitted package |
| `submission_id` | Pantry submission ID (for logging only) |
| `callback_url` | Where to POST the result |
| `callback_token` | One-time token scoped to a single submission |
| `is_bundle` | `true` for multi-component bundles |
| `packages` | JSON array of extra pip packages the harness needs |
| `sdk_ref` | Ref of `jarvis-command-sdk` to install (default `main`) |

`harness.py` is vendored in this repo. Keep it in sync with
`jarvis-pantry/app/services/test_harness.py` — they share an env-var contract
(`JARVIS_HARNESS_REPO_DIR`, `JARVIS_HARNESS_COMMAND_DIR`,
`JARVIS_HARNESS_TEST_DIR`). When pantry's harness changes, copy it here.

## Manual dispatch (debugging)

From the Actions tab you can kick off the workflow by hand for any public
repo URL. The callback will fail with a 401 unless you supply a valid
`callback_token`, but the test output is visible in the job log.
