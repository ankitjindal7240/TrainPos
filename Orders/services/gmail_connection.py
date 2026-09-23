import imaplib


GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993


def test_gmail_connection(email_address, app_password):
    """Perform only an IMAP authentication check; never ingest messages."""
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
        mail.login(email_address, app_password)
        return True
    except (imaplib.IMAP4.error, OSError):
        return False
    finally:
        if mail is not None:
            try:
                mail.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
