"""Tests for the messaging scripts.

The module is loaded by file path because ``messaging/`` is not a Python package. Loading it
here is itself the first assertion: if importing had side effects, this file would send an SMS
on import.
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMS_PATH = os.path.join(ROOT, "messaging", "twilio", "send_test_sms.py")

_spec = importlib.util.spec_from_file_location("send_test_sms", SMS_PATH)
send_test_sms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(send_test_sms)


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
        with open(os.path.join(ROOT, ".env.example")) as fh:
            example = fh.read()
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


if __name__ == "__main__":
    unittest.main()
