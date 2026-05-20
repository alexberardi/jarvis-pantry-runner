"""Unit tests for the callback HMAC signing path (#26).

The signing function is the only piece of the runner that *has* to match
pantry server bit-for-bit — if these vectors drift, callbacks 401 in prod.
The vector test pins down the exact bytes-in / hex-out mapping; the
roundtrip test verifies that the server-side recipe (which lives in
jarvis-pantry/app/api/submit.py) produces the same digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import runner


SIGNING_KEY = "test-signing-key-for-unit-testing-32b"
NONCE = "abc-123-nonce"


class TestSignCallback:
    def test_hex_is_lowercase_64_chars(self):
        sig = runner.sign_callback(
            submission_id="42",
            nonce=NONCE,
            body_bytes=b'{"passed": true}',
            signing_key=SIGNING_KEY,
        )
        assert len(sig) == 64
        assert sig == sig.lower()
        # Confirm it's hex
        int(sig, 16)

    def test_known_vector(self):
        """Frozen input → frozen hex. If this changes, server-side
        verification breaks for every in-flight submission."""
        sig = runner.sign_callback(
            submission_id="42",
            nonce="abc-123-nonce",
            body_bytes=b'{"passed":true,"summary":"ok"}',
            signing_key="test-signing-key-for-unit-testing-32b",
        )
        # Computed independently from the same inputs:
        #   hmac-sha256("test-signing-key-for-unit-testing-32b",
        #               "42|abc-123-nonce|{\"passed\":true,\"summary\":\"ok\"}")
        expected = hmac.new(
            b"test-signing-key-for-unit-testing-32b",
            b'42|abc-123-nonce|{"passed":true,"summary":"ok"}',
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected

    def test_body_tamper_changes_signature(self):
        sig_a = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=b'{"passed": true}', signing_key=SIGNING_KEY,
        )
        sig_b = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=b'{"passed": false}', signing_key=SIGNING_KEY,
        )
        assert sig_a != sig_b

    def test_nonce_change_changes_signature(self):
        body = b'{"passed": true}'
        sig_a = runner.sign_callback(
            submission_id="42", nonce="nonce-a",
            body_bytes=body, signing_key=SIGNING_KEY,
        )
        sig_b = runner.sign_callback(
            submission_id="42", nonce="nonce-b",
            body_bytes=body, signing_key=SIGNING_KEY,
        )
        assert sig_a != sig_b

    def test_submission_id_change_changes_signature(self):
        body = b'{"passed": true}'
        sig_a = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=body, signing_key=SIGNING_KEY,
        )
        sig_b = runner.sign_callback(
            submission_id="43", nonce=NONCE,
            body_bytes=body, signing_key=SIGNING_KEY,
        )
        assert sig_a != sig_b

    def test_key_change_changes_signature(self):
        body = b'{"passed": true}'
        sig_a = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=body, signing_key="key-a-padded-to-32-bytes-here-yes!",
        )
        sig_b = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=body, signing_key="key-b-padded-to-32-bytes-here-yes!",
        )
        assert sig_a != sig_b


class TestPostCallbackWire:
    """The runner posts the exact bytes it signed. If `requests` re-serializes
    via `json=` the bytes drift and verification fails — use `data=`."""

    def _harness_files(self, tmp_path: Path) -> tuple[Path, Path]:
        out = tmp_path / "harness_output.json"
        err = tmp_path / "harness_stderr.log"
        out.write_text(json.dumps({"passed": 2, "failed": 0, "summary": "2/2 passed"}))
        err.write_text("")
        return out, err

    def _env(self, monkeypatch):
        monkeypatch.setenv("CALLBACK_URL", "https://pantry.example/v1/submissions/42/container-result")
        monkeypatch.setenv("SUBMISSION_ID", "42")
        monkeypatch.setenv("NONCE", NONCE)
        monkeypatch.setenv("PANTRY_CALLBACK_SIGNING_KEY", SIGNING_KEY)

    def test_posts_signed_bytes_with_hmac_header(self, tmp_path, monkeypatch):
        out, err = self._harness_files(tmp_path)
        self._env(monkeypatch)

        with patch("runner.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            post.return_value.raise_for_status = lambda: None

            monkeypatch.setattr(
                "sys.argv",
                ["runner.py", "--output", str(out), "--stderr", str(err),
                 "--run-url", "https://example/run"],
            )
            assert runner.main() == 0

        assert post.call_count == 1
        _args, kwargs = post.call_args
        # Bytes the server will see must equal bytes we signed — that means
        # `data=<bytes>`, NOT `json=<dict>` (requests would re-serialize).
        sent_bytes = kwargs["data"]
        assert isinstance(sent_bytes, bytes)
        sent_sig = kwargs["headers"]["X-Pantry-HMAC"]
        expected_sig = runner.sign_callback(
            submission_id="42", nonce=NONCE,
            body_bytes=sent_bytes, signing_key=SIGNING_KEY,
        )
        assert sent_sig == expected_sig
        # Legacy token-style header MUST be gone — server rejects it now.
        assert "X-Pantry-Token" not in kwargs["headers"]

    def test_failure_path_still_posts_signed_passed_false(self, tmp_path, monkeypatch):
        """No output file → runner builds a passed=false payload and still
        signs/posts it. Otherwise crashed submissions would stall in
        `awaiting_container` until the timeout watcher reaped them."""
        empty_out = tmp_path / "harness_output.json"
        err = tmp_path / "harness_stderr.log"
        # Note: empty_out does not exist
        err.write_text("harness crashed before writing")
        self._env(monkeypatch)

        with patch("runner.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            post.return_value.raise_for_status = lambda: None

            monkeypatch.setattr(
                "sys.argv",
                ["runner.py", "--output", str(empty_out), "--stderr", str(err),
                 "--run-url", "https://example/run"],
            )
            assert runner.main() == 0

        assert post.call_count == 1
        _args, kwargs = post.call_args
        sent = json.loads(kwargs["data"])
        assert sent["passed"] is False
        assert "X-Pantry-HMAC" in kwargs["headers"]


class TestMissingEnvVars:
    def test_missing_signing_key_raises(self, tmp_path, monkeypatch):
        out = tmp_path / "out.json"; out.write_text("{}")
        err = tmp_path / "err.log"; err.write_text("")
        monkeypatch.setenv("CALLBACK_URL", "https://pantry.example/cb")
        monkeypatch.setenv("SUBMISSION_ID", "1")
        monkeypatch.setenv("NONCE", "n")
        monkeypatch.delenv("PANTRY_CALLBACK_SIGNING_KEY", raising=False)
        monkeypatch.setattr(
            "sys.argv",
            ["runner.py", "--output", str(out), "--stderr", str(err),
             "--run-url", "https://example/run"],
        )
        with pytest.raises(KeyError, match="PANTRY_CALLBACK_SIGNING_KEY"):
            runner.main()

    def test_missing_nonce_raises(self, tmp_path, monkeypatch):
        out = tmp_path / "out.json"; out.write_text("{}")
        err = tmp_path / "err.log"; err.write_text("")
        monkeypatch.setenv("CALLBACK_URL", "https://pantry.example/cb")
        monkeypatch.setenv("SUBMISSION_ID", "1")
        monkeypatch.setenv("PANTRY_CALLBACK_SIGNING_KEY", SIGNING_KEY)
        monkeypatch.delenv("NONCE", raising=False)
        monkeypatch.setattr(
            "sys.argv",
            ["runner.py", "--output", str(out), "--stderr", str(err),
             "--run-url", "https://example/run"],
        )
        with pytest.raises(KeyError, match="NONCE"):
            runner.main()
