"""Post the harness result back to the pantry callback URL.

Reads the harness JSON output, normalizes it to the ContainerResultCallback
schema that pantry expects, signs it with HMAC-SHA256 over
``{submission_id}|{nonce}|{body_bytes}`` keyed by the
``PANTRY_CALLBACK_SIGNING_KEY`` env var, and POSTs it with an
``X-Pantry-HMAC`` header.

The signing key is sourced from a GitHub Actions environment secret gated to
the ``callback`` job — submitted package code (which runs in the ``test``
job) never sees it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import requests


def _load_harness_output(output_path: Path, stderr_path: Path) -> dict:
    if not output_path.exists() or output_path.stat().st_size == 0:
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        return {
            "passed": False,
            "summary": "FAIL - harness produced no output",
            "test_count": 0,
            "pass_count": 0,
            "fail_count": 1,
            "errors": [stderr[-2000:]] if stderr else ["harness did not write JSON"],
            "raw_output": stderr,
        }

    try:
        raw = json.loads(output_path.read_text())
    except json.JSONDecodeError as e:
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        return {
            "passed": False,
            "summary": f"FAIL - could not parse harness output: {e}",
            "test_count": 0,
            "pass_count": 0,
            "fail_count": 1,
            "errors": [stderr[-2000:]] if stderr else [],
            "raw_output": output_path.read_text(),
        }

    passed = raw.get("passed", 0)
    failed = raw.get("failed", 1)
    return {
        "passed": failed == 0,
        "summary": raw.get("summary", "Unknown"),
        "test_count": passed + failed,
        "pass_count": passed,
        "fail_count": failed,
        "errors": raw.get("errors", []),
        "raw_output": output_path.read_text(),
    }


def sign_callback(
    *, submission_id: str, nonce: str, body_bytes: bytes, signing_key: str,
) -> str:
    """HMAC-SHA256 over `{submission_id}|{nonce}|{body_bytes}` as lowercase hex.

    The body bytes are appended verbatim — Pantry reads ``request.body()`` to
    recover the exact same bytes, no canonicalization layer in between.
    """
    msg = f"{submission_id}|{nonce}|".encode("utf-8") + body_bytes
    return hmac.new(signing_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stderr", required=True, type=Path)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()

    callback_url = os.environ["CALLBACK_URL"]
    submission_id = os.environ["SUBMISSION_ID"]
    nonce = os.environ["NONCE"]
    signing_key = os.environ["PANTRY_CALLBACK_SIGNING_KEY"]

    payload = _load_harness_output(args.output, args.stderr)
    body_bytes = json.dumps(payload).encode("utf-8")
    signature = sign_callback(
        submission_id=submission_id,
        nonce=nonce,
        body_bytes=body_bytes,
        signing_key=signing_key,
    )

    print(f"Posting result to {callback_url} (passed={payload['passed']}, run={args.run_url})")
    resp = requests.post(
        callback_url,
        data=body_bytes,
        headers={
            "X-Pantry-HMAC": signature,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    print(f"Callback response: {resp.status_code} {resp.text[:500]}")
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
