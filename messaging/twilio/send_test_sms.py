"""Send a test SMS through Twilio.

Importing this module has no side effects: the message is sent only when the file is run
directly, and only after the required environment variables are present. Before, importing it
sent an SMS (and three of its four inputs were unvalidated, so a missing variable surfaced as
a cryptic error from the Twilio client).
"""

import os
import sys

REQUIRED_VARIABLES = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "SPAIN_PHONE_NUMBER",
)


def load_env_file():
    """Load a .env file if python-dotenv is installed. Absence is not an error."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def missing_variables():
    return [name for name in REQUIRED_VARIABLES if not os.getenv(name)]


def main():
    load_env_file()

    missing = missing_variables()
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    from twilio.rest import Client

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    message = client.messages.create(
        from_=os.environ["TWILIO_PHONE_NUMBER"],
        to=os.environ["SPAIN_PHONE_NUMBER"],
        body="Test from simple-cloud!",
    )
    print(message.sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
