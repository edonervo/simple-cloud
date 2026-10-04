"""Send an email through the Gmail API.

Importing this module has no side effects, and needs no third-party package: the Google
libraries are imported inside the functions that use them. Before, the module was importable
only when the whole Google stack was installed.

The sender and recipient are read from the environment rather than hardcoded, so the two
addresses that used to sit in this file are no longer committed (see docs/secrets.md §1).
"""

import base64
import os
import sys
from email.message import EmailMessage

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Resolved next to this script, not the working directory, so the files are found whatever
# directory the script is launched from.
HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
TOKEN_FILE = os.path.join(HERE, "token.json")

REQUIRED_VARIABLES = ("GMAIL_SENDER", "GMAIL_RECIPIENT")


def load_env_file():
    """Load a .env file if python-dotenv is installed. Absence is not an error."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def missing_variables():
    return [name for name in REQUIRED_VARIABLES if not os.getenv(name)]


def authenticate():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    # token.json stores the user's access and refresh tokens, and is created automatically
    # when the authorization flow completes for the first time.
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=8080)
        # Save the credentials for the next run.
        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return creds


def send_email(sender, recipient, subject="Automated draft", body="This is automated draft mail"):
    """Send one message. Returns the API response, or None if the request failed."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = authenticate()

    message = EmailMessage()
    message.set_content(body)
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        service = build("gmail", "v1", credentials=creds)
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": encoded_message})
            .execute()
        )
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        return None

    print(f'Message Id: {sent["id"]}')
    return sent


def main():
    load_env_file()

    missing = missing_variables()
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    sent = send_email(os.environ["GMAIL_SENDER"], os.environ["GMAIL_RECIPIENT"])
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
