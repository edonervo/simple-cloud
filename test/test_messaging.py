"""Tests for the messaging scripts.

The modules are loaded by file path because ``messaging/`` is not a Python package. Loading
them here is itself the first assertion: if importing had side effects, this file would send
an SMS (and, before the Phase 4 refactor, would have needed the Google libraries installed).
"""

import base64
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMS_PATH = os.path.join(ROOT, "messaging", "twilio", "send_test_sms.py")
GMAIL_PATH = os.path.join(ROOT, "messaging", "gmail", "send_email.py")

_spec = importlib.util.spec_from_file_location("send_test_sms", SMS_PATH)
send_test_sms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(send_test_sms)

_gmail_spec = importlib.util.spec_from_file_location("send_email", GMAIL_PATH)
send_email = importlib.util.module_from_spec(_gmail_spec)
_gmail_spec.loader.exec_module(send_email)


def _env_example():
    with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as fh:
        return fh.read()


def _google_stubs(fail=False):
    """A ``sys.modules`` overlay standing in for the Google libraries.

    The libraries are imported inside the functions that use them, so they are not needed to
    import the module — only to exercise a send. The stub captures the ``raw`` body the script
    hands to the API, which is what the content assertions decode; nothing touches the network.
    """
    captured = {}

    class HttpError(Exception):
        pass

    creds = mock.Mock(valid=True, expired=False, refresh_token=None)
    credentials_mod = types.ModuleType("google.oauth2.credentials")
    credentials_mod.Credentials = mock.Mock()
    credentials_mod.Credentials.from_authorized_user_file.return_value = creds

    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = mock.Mock()

    flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    flow_mod.InstalledAppFlow = mock.Mock()

    def build(service, version, credentials=None):
        def send(userId=None, body=None):
            if fail:
                raise HttpError("simulated API failure")
            captured["raw"] = body["raw"]
            return mock.Mock(execute=mock.Mock(return_value={"id": "MSG1"}))

        messages = mock.Mock(send=mock.Mock(side_effect=send))
        users = mock.Mock(return_value=mock.Mock(messages=mock.Mock(return_value=messages)))
        return mock.Mock(users=users)

    discovery_mod = types.ModuleType("googleapiclient.discovery")
    discovery_mod.build = build

    errors_mod = types.ModuleType("googleapiclient.errors")
    errors_mod.HttpError = HttpError

    modules = {
        name: types.ModuleType(name)
        for name in (
            "google",
            "google.auth",
            "google.auth.transport",
            "google.oauth2",
            "google_auth_oauthlib",
            "googleapiclient",
        )
    }
    modules.update(
        {
            "google.auth.transport.requests": requests_mod,
            "google.oauth2.credentials": credentials_mod,
            "google_auth_oauthlib.flow": flow_mod,
            "googleapiclient.discovery": discovery_mod,
            "googleapiclient.errors": errors_mod,
        }
    )
    return modules, captured, credentials_mod


class SendTestSmsTests(unittest.TestCase):
    def test_importing_the_module_has_no_side_effects(self):
        # Reaching this method proves the import above ran without sending anything.
        self.assertTrue(callable(send_test_sms.main))

    def test_missing_variables_report_and_do_not_send(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(send_test_sms.main(), 1)
        reported = stderr.getvalue()
        for name in send_test_sms.REQUIRED_VARIABLES:
            self.assertIn(name, reported)

    def test_every_required_variable_is_listed_in_env_example(self):
        example = _env_example()
        for name in send_test_sms.REQUIRED_VARIABLES:
            self.assertIn(name, example)

    def test_a_configured_run_uses_the_environment_values(self):
        if importlib.util.find_spec("twilio") is None:
            self.skipTest("twilio is not installed in this environment")
        env = {
            "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_AUTH_TOKEN": "x" * 32,
            "TWILIO_PHONE_NUMBER": "+10000000000",
            "SPAIN_PHONE_NUMBER": "+10000000001",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("twilio.rest.Client") as client:
            client.return_value.messages.create.return_value = mock.Mock(sid="SM123")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(send_test_sms.main(), 0)
            self.assertEqual(client.return_value.messages.create.call_count, 1)


class SendEmailTests(unittest.TestCase):
    def test_importing_the_module_has_no_side_effects(self):
        # The Google libraries are absent in most environments; the import above succeeding
        # proves the module no longer requires them at import time.
        self.assertTrue(callable(send_email.main))

    def test_missing_variables_report_and_do_not_send(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stderr(stderr), \
             mock.patch.object(send_email, "send_email") as sender:
            self.assertEqual(send_email.main(), 1)
            sender.assert_not_called()
        reported = stderr.getvalue()
        for name in send_email.REQUIRED_VARIABLES:
            self.assertIn(name, reported)

    def test_every_required_variable_is_listed_in_env_example(self):
        example = _env_example()
        for name in send_email.REQUIRED_VARIABLES:
            self.assertIn(name, example)

    def test_credentials_are_resolved_next_to_the_script(self):
        gmail_dir = os.path.dirname(GMAIL_PATH)
        self.assertEqual(os.path.dirname(send_email.CREDENTIALS_FILE), gmail_dir)
        self.assertEqual(os.path.dirname(send_email.TOKEN_FILE), gmail_dir)

    def test_authenticate_reads_the_cached_token_next_to_the_script(self):
        modules, _captured, credentials_mod = _google_stubs()
        with mock.patch.dict(sys.modules, modules), \
             mock.patch.object(send_email.os.path, "exists", return_value=True):
            send_email.authenticate()
        credentials_mod.Credentials.from_authorized_user_file.assert_called_once_with(
            send_email.TOKEN_FILE, send_email.SCOPES
        )

    def test_a_configured_run_sends_one_message_with_the_expected_content(self):
        modules, captured, _credentials_mod = _google_stubs()
        env = {"GMAIL_SENDER": "sender@example.com", "GMAIL_RECIPIENT": "recipient@example.com"}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(sys.modules, modules), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(send_email, "TOKEN_FILE", os.path.join(tmp, "token.json")):
            # A token file must exist, or the script would start a consent flow and write one.
            with open(send_email.TOKEN_FILE, "w", encoding="utf-8") as token:
                token.write("{}")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(send_email.main(), 0)

        self.assertIn("raw", captured)
        message = base64.urlsafe_b64decode(captured["raw"]).decode()
        self.assertIn("To: recipient@example.com", message)
        self.assertIn("From: sender@example.com", message)
        self.assertIn("Subject: Automated draft", message)
        self.assertIn("This is automated draft mail", message)

    def test_a_failed_send_is_reported_and_returns_non_zero(self):
        modules, _captured, _credentials_mod = _google_stubs(fail=True)
        env = {"GMAIL_SENDER": "sender@example.com", "GMAIL_RECIPIENT": "recipient@example.com"}
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(sys.modules, modules), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(send_email, "TOKEN_FILE", os.path.join(tmp, "token.json")), \
             contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with open(send_email.TOKEN_FILE, "w", encoding="utf-8") as token:
                token.write("{}")
            self.assertEqual(send_email.main(), 1)
        self.assertIn("simulated API failure", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
