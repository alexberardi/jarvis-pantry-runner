# jarvis-pantry-runner

Public, one-purpose repo: runs Jarvis package test harnesses on behalf of
[jarvis-pantry](https://github.com/alexberardi/jarvis-pantry). When a user
submits a package, pantry dispatches the `container-test` workflow here with
the repo URL and a per-submission nonce. The workflow checks out the
submission, runs the SDK test harness against it, and POSTs the result back
to pantry signed with HMAC-SHA256.

**You shouldn't need to edit anything in here manually.** All runtime
parameters are passed as `workflow_dispatch` inputs; the signing key lives in
a GHA environment secret (see "One-time setup" below).

## Why a separate repo?

Fly machines can't run Docker-in-Docker, so pantry can't sandbox untrusted
submission code on its own infra. GitHub Actions gives us a clean, isolated
VM per submission for free.

## Two-job model (#26)

The workflow is split into two jobs so submitted code can't exfiltrate the
callback signing key:

| Job | What it does | Has signing key? |
|-----|--------------|------------------|
| `test` | Clones the submission, runs the harness in a docker sandbox, uploads the result as a build artifact | **No** |
| `callback` | Downloads the harness artifact and POSTs the signed result back to pantry | **Yes** (gated to the `pantry-callback` GHA environment) |

The two jobs only share data through the `harness-result` artifact. The
`callback` job never runs submitted code — it never clones the repo, never
installs the submission's lockfile, never invokes the harness.

`callback` runs with `needs: test` and `if: always()`, so a failed harness
still posts a `passed=false` result back to pantry (otherwise the submission
would stall in `awaiting_container` until the timeout watcher reaped it).

## Sandbox

The harness runs inside `docker run --network=none --read-only --memory=128m`
with a `tmpfs` at `/tmp`. The submission and SDK dirs are bind-mounted
read-only; only a small `/output` dir is writable for the harness's JSON
result. SDK and submitted-lockfile installs (the latter still under the
`--only-binary=:all: --no-build-isolation` lockdown from #13) happen in a
separate pre-stage docker step (network on) into a named volume that's
mounted read-only into the sandbox. This is the defense against a malicious
submission that tries to hit external services or chew CPU/RAM during
`run()`. The signing-key exfil path is closed by the two-job split (#26),
not by the sandbox itself.

## Callback authentication (HMAC, not bearer)

The callback is signed with HMAC-SHA256 over

```
{submission_id}|{nonce}|{request_body_bytes}
```

keyed by `PANTRY_CALLBACK_SIGNING_KEY` (shared with the pantry server). The
signature goes in the `X-Pantry-HMAC` header. The runner posts the exact
bytes it signed (`requests.post(data=<bytes>)`, not `json=<dict>`) so the
server can recover the same bytes from `request.body()` and recompute the
digest without any canonicalization in between.

This replaces the pre-#26 `X-Pantry-Token` scheme. The old one-time token was
injected as a `workflow_dispatch` input, which meant every step in the
workflow could read it from `$GITHUB_EVENT_PATH` — submitted code included.

## Workflow inputs

| Input | Description |
|-------|-------------|
| `repo_url` | HTTPS URL of the submitted package |
| `submission_id` | Pantry submission ID (used in the HMAC + for logging) |
| `callback_url` | Where to POST the result |
| `nonce` | Per-submission value mixed into the HMAC. Public (no exfil risk). |
| `is_bundle` | `true` for multi-component bundles |
| `lockfile_content` | Pre-resolved pip lockfile (output of `pip-compile` or equivalent). Installed with `--only-binary=:all: --no-build-isolation --no-cache-dir` so no submitted-package `setup.py` ever runs on the GHA host. |
| `sdk_ref` | Ref of `jarvis-command-sdk` to install (default `main`) |

`harness.py` is vendored in this repo. Keep it in sync with
`jarvis-pantry/app/services/test_harness.py` — they share an env-var contract
(`JARVIS_HARNESS_REPO_DIR`, `JARVIS_HARNESS_COMMAND_DIR`,
`JARVIS_HARNESS_TEST_DIR`). When pantry's harness changes, copy it here.

## One-time setup: pantry-callback GHA environment

The signing key MUST be installed as a secret in a GitHub Actions environment
named `pantry-callback`. Only the `callback` job has access to that
environment, which is the whole point of the split.

```bash
# Generate a key (≥ 32 bytes of randomness)
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# Install it as an env-scoped secret on this repo
gh secret set PANTRY_CALLBACK_SIGNING_KEY \
  --env pantry-callback \
  --repo alexberardi/jarvis-pantry-runner
```

Set the **same** value as the `PANTRY_CALLBACK_SIGNING_KEY` env var on the
pantry server. Rotation procedure lives in
`jarvis-pantry/docs/ops/callback-signing-key-rotation.md`.

If the environment doesn't exist yet, create it once in the repo settings UI
(`Settings → Environments → New environment → "pantry-callback"`). No reviewers
or wait timers needed.

## Manual dispatch (debugging)

From the Actions tab you can kick off the workflow by hand for any public
repo URL. The callback will fail with a 401 unless you supply a valid
`nonce` and the GHA environment has the same signing key the target pantry
server is using, but the test output is visible in the job log.
