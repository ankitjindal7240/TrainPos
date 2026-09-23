"""Classify ingestion errors without making retry decisions from broad exceptions."""


def is_terminal_parser_or_validation_error(error):
    """Return true only for explicit parser/domain validation failures.

    Parsers and the centralized order-creation validation deliberately raise
    ValueError for malformed, immutable email content. Database, IMAP, and all
    unexpected errors remain retryable by default.
    """
    return isinstance(error, ValueError)


def sanitized_error_message(error):
    """Keep an audit-friendly error without persisting arbitrary verbose output."""
    message = " ".join(str(error).split()) or "Email processing failed."
    return message[:500]
