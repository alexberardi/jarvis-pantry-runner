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

## Sandbox

The harness runs inside `docker run --network=none --read-only --memory=128m`
with a `tmpfs` at `/tmp`. The submission and SDK dirs are bind-mounted
read-only; only a small `/output` dir is writable for the harness's JSON
result. SDK and submitted-lockfile installs (the latter still under the
`--only-binary=:all: --no-build-isolation` lockdown from #13) happen in a
separate pre-stage docker step (network on) into a named volume that's
mounted read-only into the sandbox. This is the defense against a malicious
submission that tries to exfiltrate the callback token, hit external
services, or chew CPU/RAM during `run()`.

## Workflow inputs

| Input | Description |
|-------|-------------|
| `repo_url` | HTTPS URL of the submitted package |
| `submission_id` | Pantry submission ID (for logging only) |
| `callback_url` | Where to POST the result |
| `callback_token` | One-time token scoped to a single submission |
| `is_bundle` | `true` for multi-component bundles |
| `lockfile_content` | Pre-resolved pip lockfile (output of `pip-compile` or equivalent). Installed with `--only-binary=:all: --no-build-isolation --no-cache-dir` so no submitted-package `setup.py` ever runs on the GHA host. |
| `sdk_ref` | Ref of `jarvis-command-sdk` to install (default `main`) |

`harness.py` is vendored in this repo. Keep it in sync with
`jarvis-pantry/app/services/test_harness.py` — they share an env-var contract
(`JARVIS_HARNESS_REPO_DIR`, `JARVIS_HARNESS_COMMAND_DIR`,
`JARVIS_HARNESS_TEST_DIR`). When pantry's harness changes, copy it here.

## Manual dispatch (debugging)

From the Actions tab you can kick off the workflow by hand for any public
repo URL. The callback will fail with a 401 unless you supply a valid
`callback_token`, but the test output is visible in the job log.
