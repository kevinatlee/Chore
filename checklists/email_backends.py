import logging

from django.core.mail.backends.base import BaseEmailBackend


logger = logging.getLogger(__name__)


class DisabledEmailBackend(BaseEmailBackend):
    """Fail-closed backend used when outbound email is disabled."""

    def send_messages(self, email_messages):
        messages = list(email_messages or [])
        if messages:
            logger.warning(
                "Outbound email is disabled; suppressed %d message(s).", len(messages)
            )
        return 0
