"""Post the harness result back to the pantry callback URL.

Reads the harness JSON output, normalizes it to the ContainerResultCallback
schema that pantry expects, and POSTs it with the one-time X-Pantry-Token
header issued when this workflow was dispatched.
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stderr", required=True, type=Path)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()

    callback_url = os.environ["CALLBACK_URL"]
    callback_token = os.environ["CALLBACK_TOKEN"]

    payload = _load_harness_output(args.output, args.stderr)

    print(f"Posting result to {callback_url} (passed={payload['passed']}, run={args.run_url})")
    resp = requests.post(
        callback_url,
        headers={"X-Pantry-Token": callback_token, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    print(f"Callback response: {resp.status_code} {resp.text[:500]}")
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
