"""Completion-email sending, moved here from the runner.

Previously the runner sent the experiment-completion email directly, which meant
the untrusted runner pod carried the Gmail OAuth credentials (GMAIL_CREDS) and
needed outbound internet access to reach the Google API. Both are now confined
to the backend: the runner POSTs a small request to /sendEmail and the backend
(trusted, already internet-facing) does the actual send.
"""
import base64
import json
import logging
import os
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def gmail_auth_from_env():
    """Build an authenticated Gmail service from the GMAIL_CREDS env secret."""
    creds_json = os.getenv('GMAIL_CREDS')
    if creds_json is None:
        raise ValueError("GMAIL_CREDS environment variable not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials(
        token=None,
        refresh_token=creds_dict.get('refresh_token'),
        token_uri=creds_dict.get('token_uri'),
        client_id=creds_dict.get('client_id'),
        client_secret=creds_dict.get('client_secret'),
        scopes=SCOPES)
    return build('gmail', 'v1', credentials=creds)


def create_message(to, subject, message_text):
    """Build a base64url-encoded Gmail message payload."""
    message = MIMEText(message_text)
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {'raw': raw}


def send_completion_email(creator_email, name, status, passes, fails):
    """Send an experiment-completion email. Best-effort: logs and swallows errors.

    The runner treats email as non-critical (a failed send must never fail the
    experiment), so this returns the sent message id on success or None on
    failure rather than raising.
    """
    try:
        service = gmail_auth_from_env()
        subject = f'GLADOS Experiment: {name}'
        body = (
            "Experiment stats: \n\n"
            f" Status: {status}\n\n"
            f" Passes: {passes}\n\n"
            f" Fails: {fails}\n\n"
            "Thank you for using GLADOS!\n\n"
        )
        message = create_message(creator_email, subject, body)
        sent = service.users().messages().send(userId="me", body=message).execute()
        logger.info("Sent completion email to %s (message id %s)", creator_email, sent.get('id'))
        return sent.get('id')
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.error("Failed to send completion email to %s: %s", creator_email, err)
        return None
