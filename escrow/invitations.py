"""Counterparty invitation emails.

Sent once when an escrow first reaches awaiting_counterparty, from any
interface. Carries the code plus a prefilled join link; the counterparty
still proves their inbox by OTP, so the code alone grants nothing.
Best-effort: mail failure never breaks verification; one send per escrow.
"""
import logging

from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def invitation_already_sent(contract):
    from escrow.models import AuditEvent
    return AuditEvent.objects.filter(
        escrow=contract, action='invitation.sent').exists()


def send_counterparty_invitation(contract, base_url=''):
    """Email the counterparty their invite. Returns True when accepted.

    Accepts (not raises) on any failure. Skips quietly when already sent.
    """
    from escrow.services import audit
    if invitation_already_sent(contract):
        return False
    join_path = reverse('join_escrow_page')
    join_link = f"{(base_url or '').rstrip('/')}{join_path}?code={contract.code}"
    subject = f"You have a Locktrans escrow to review ({contract.code})"
    body = (
        f"Hello,\n\n"
        f"{contract.creator_email} started a Locktrans escrow with you:\n\n"
        f"  {contract.title}, {contract.amount} {contract.currency}\n"
        f"  Escrow code: {contract.code}\n\n"
        f"Review the terms and accept or reject them here:\n"
        f"  {join_link}\n\n"
        f"You will verify this email address with a one-time code before "
        f"anything is shown. The code alone does not grant access.\n\n"
        f"- Locktrans"
    )
    try:
        send_mail(subject, body, None, [contract.counterparty_email],
                  fail_silently=False)
    except Exception:
        logger.exception("Invitation email failed for %s", contract.code)
        return False
    audit('invitation.sent', actor=contract.creator_email, escrow=contract,
          metadata={'to': contract.counterparty_email})
    logger.info("Invitation sent for %s to %s",
                contract.code, contract.counterparty_email)
    return True
