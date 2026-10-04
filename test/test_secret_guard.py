"""Tests for scripts/secret_guard.py.

The most important invariant is negative: a finding must never render the value it found.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import secret_guard  # noqa: E402

# AWS's documented public example key. Not a real credential.
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret


class RedactionTests(unittest.TestCase):
    def test_render_never_contains_the_value(self):
        finding = secret_guard.Finding(
            "block", "x.py:1", "pattern", "aws-access-key-id",
            secret_guard._redact(FAKE_AWS_KEY, show_prefix=True),
        )
        self.assertNotIn(FAKE_AWS_KEY, finding.render())
        self.assertNotIn(FAKE_AWS_KEY, str(finding.as_dict()))

    def test_pattern_preview_shows_only_the_first_four_characters(self):
        preview = secret_guard._redact(FAKE_AWS_KEY, show_prefix=True)
        self.assertTrue(preview.startswith("AKIA"))
        self.assertNotIn(FAKE_AWS_KEY[4:], preview)

    def test_configured_value_preview_shows_nothing(self):
        preview = secret_guard._redact(FAKE_AWS_KEY, show_prefix=False)
        self.assertNotIn("AKIA", preview)


class DetectionTests(unittest.TestCase):
    def test_pattern_layer_finds_a_provider_shaped_key(self):
        findings = secret_guard.scan_text(f'aws_key = "{FAKE_AWS_KEY}"', "f.py", [], [])
        self.assertTrue(any(f.kind == "pattern" for f in findings))

    def test_inline_pragma_suppresses_the_line(self):
        line = f'aws_key = "{FAKE_AWS_KEY}"  # {secret_guard.INLINE_PRAGMA}'
        self.assertEqual(secret_guard.scan_text(line, "f.py", [], []), [])

    def test_allowlist_suppresses_a_known_literal(self):
        findings = secret_guard.scan_text(f'k = "{FAKE_AWS_KEY}"', "f.py", [FAKE_AWS_KEY], [])
        self.assertEqual(findings, [])

    def test_configured_value_is_found_and_redacted(self):
        secret = "7f3a91c2e4b8d6a0f1e2d3c4b5a69788"  # pragma: allowlist secret
        findings = secret_guard.scan_text(
            f"x = {secret}", "f.py", [], [("TWILIO_AUTH_TOKEN", secret)]
        )
        configured = [f for f in findings if f.kind == "configured-secret"]
        self.assertTrue(configured)
        self.assertNotIn(secret, configured[0].render())

    def test_placeholder_values_are_not_high_entropy(self):
        for value in ("your-token-here", "<insert-token>", "placeholder", "changeme12345678"):
            with self.subTest(value=value):
                self.assertFalse(secret_guard._is_high_entropy(value))

    def test_a_random_token_is_high_entropy(self):
        self.assertTrue(secret_guard._is_high_entropy("aB3xK9mQ2Lp7Zt4Rw8"))


class ScopeTests(unittest.TestCase):
    def test_an_unknown_scope_is_rejected_not_ignored(self):
        # A typo must not resolve to "no scopes", which would print a confident green.
        with self.assertRaises(ValueError):
            secret_guard._resolve_scopes(["histroy"])

    def test_all_expands_to_every_scope_and_deduplicates(self):
        self.assertEqual(secret_guard._resolve_scopes(["all", "tree"]), list(secret_guard.SCOPES))


class EnvExampleTests(unittest.TestCase):
    def test_a_blank_template_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env.example"), "w") as fh:
                fh.write("TWILIO_AUTH_TOKEN=\n")
            self.assertEqual(secret_guard._check_env_example_blank(tmp), [])

    def test_a_value_in_the_template_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env.example"), "w") as fh:
                fh.write("TWILIO_AUTH_TOKEN=abc123\n")
            findings = secret_guard._check_env_example_blank(tmp)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].severity, "block")


class EndToEndTests(unittest.TestCase):
    def test_a_staged_secret_blocks_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            with open(os.path.join(tmp, "leak.txt"), "w") as fh:
                fh.write(f'aws_key = "{FAKE_AWS_KEY}"\n')
            subprocess.run(["git", "-C", tmp, "add", "leak.txt"], check=True)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = secret_guard.main(["--root", tmp, "check", "--scope", "staged"])
            self.assertEqual(rc, secret_guard.EXIT_FINDINGS)
            self.assertNotIn(FAKE_AWS_KEY, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
