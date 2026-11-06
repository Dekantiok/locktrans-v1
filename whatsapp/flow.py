"""WhatsApp conversation workflow over the shared agent loop.

The sender number is an endpoint, never identity: every escrow tool
call uses the backend-verified email as actor. Email OTP binds number
to identity; a bare 6-digit reply while creation is parked resumes it.
Plaintext codes are never stored or sent to the model.
"""
import logging

from agent.loop import (
    ESCROW_CODE_RE,
    OTP_MESSAGE_RE,
    find_pending_join_code,
    get_or_create_conversation,
    run_turn,
)
from escrow.forms import ContractCreationForm
from escrow.invitations import send_counterparty_invitation
from escrow.models import EscrowDraft, TransitionError
from escrow.services import (
    audit,
    consume_otp,
    create_escrow_from_validated,
    escrow_next_step,
    issue_otp,
    resume_escrow_after_verification,
)
from escrow.utils import normalize_email

from . import adapter
from .models import WhatsAppLink
from voice.transcriber import (
    VoiceConfigError,
    VoiceTranscriptionError,
    transcribe_audio,
)

logger = logging.getLogger(__name__)

EMAIL_HINT = (
    "Welcome to Locktrans. To begin, please reply with your email address "
    "so I can verify it's you."
)
CODE_SENT_TEMPLATE = (
    "I sent a 6-digit code to {email}. Please reply with the code here."
)
VERIFIED_TEMPLATE = (
    "Verified, I can now help with escrows for {email}. "
    "Describe the deal and I'll guide you through it."
)
NO_CODE_PENDING = (
    "I don't have a code waiting for this number. "
    "Reply with your email address and I'll send you a fresh code."
)
CREATED_TEMPLATE = (
    "Verified, your escrow {code} is created. The counterparty has been "
    "invited by email to review and accept the terms."
)
NEED_EMAIL_FIRST = (
    "Please share your email address first so I can verify it's you, "
    "then we'll continue."
)


def get_link(wa_id):
    link, _ = WhatsAppLink.objects.get_or_create(wa_id=wa_id)
    return link


def sync_conversation_owner(conversation, link):
    verified = normalize_email(link.verified_email)
    if verified and conversation.owner_email != verified:
        conversation.owner_email = verified
        conversation.save(update_fields=['owner_email'])
    elif not verified and conversation.owner_email:
        conversation.owner_email = None
        conversation.save(update_fields=['owner_email'])


def _session_key(wa_id):
    return f"whatsapp:{wa_id}"


def _pending_creation(conversation):
    pending = conversation.pending_action or {}
    email = pending.get('verification_email') or (
        pending.get('email') if pending.get('verification_required') else ''
    )
    email = normalize_email(email or '')
    draft_id = pending.get('draft_id') or (pending.get('arguments') or {}).get('draft_id')
    if pending.get('tool') == 'request_escrow_creation' and email and draft_id:
        return email, draft_id
    return None, None


def _try_resume_creation(conversation, link, wa_id, code, base_url=''):
    """Finish a parked escrow creation when the creation OTP arrives."""
    email, draft_id = _pending_creation(conversation)
    if not email:
        return None
    result = consume_otp(email, code, 'create')
    conversation.messages.create(role='user', content='[one-time code withheld]')
    if not result.ok:
        conversation.messages.create(role='assistant', content=result.message)
        return result.message
    try:
        draft = EscrowDraft.objects.get(id=draft_id)
    except (EscrowDraft.DoesNotExist, ValueError, TypeError):
        conversation.pending_action = {}
        conversation.save(update_fields=['pending_action'])
        reply = "That draft is no longer available. Tell me the deal terms and we'll set it up again."
        conversation.messages.create(role='assistant', content=reply)
        return reply
    if not (
        (draft.owner_email and draft.owner_email == email)
        or (draft.owner_session and draft.owner_session == _session_key(wa_id))
    ):
        reply = "That code does not match this chat. Please confirm the draft again."
        conversation.messages.create(role='assistant', content=reply)
        return reply
    form = ContractCreationForm({
        'title': draft.title,
        'description': draft.description,
        'amount': draft.amount,
        'fee_payer': draft.fee_payer,
        'creator_email': email,
        'creator_role': draft.creator_role,
        'counterparty_email': draft.counterparty_email,
        'inspection_days': draft.inspection_days,
    })
    if not form.is_valid():
        reply = "The draft is missing something, tell me the full deal terms and we'll fix it together."
        conversation.messages.create(role='assistant', content=reply)
        return reply
    contract = create_escrow_from_validated(form.cleaned_data)
    draft.status = 'converted'
    draft.save(update_fields=['status'])
    # The creator email was just proven by the consumed code, advance past
    # pending_verification so the counterparty can join.
    confirmed_fields = {}
    if email == contract.buyer_email:
        confirmed_fields['buyer_confirmed'] = True
    elif email == contract.seller_email:
        confirmed_fields['seller_confirmed'] = True
    try:
        contract.transition_to('awaiting_counterparty', **confirmed_fields)
    except TransitionError:
        logger.warning("Created %s but could not advance it", contract.code)
    else:
        send_counterparty_invitation(contract, base_url=base_url)
    conversation.owner_email = email
    conversation.pending_action = {}
    conversation.save(update_fields=['owner_email', 'pending_action'])
    link.verified_email = email
    link.pending_email = ''
    link.save(update_fields=['pending_email', 'verified_email'])
    audit('escrow.created_via_whatsapp', actor=email, escrow=contract)
    reply = CREATED_TEMPLATE.format(code=contract.code)
    conversation.messages.create(role='assistant', content=reply)
    return reply


def _join_reply_after_verify(conversation, email):
    """Complete a pending join right after WhatsApp email verification."""
    code = find_pending_join_code(conversation)
    if conversation.pending_context:
        conversation.pending_context = {}
        conversation.save(update_fields=['pending_context'])
    if not code:
        return None
    contract, kind, error = resume_escrow_after_verification(code, email)
    if contract is None:
        return f"You're verified, but I can't continue that escrow: {error}"
    audit(f'counterparty.{kind}_via_whatsapp', actor=email, escrow=contract)
    if kind == 'joined':
        return (f"Verified and joined, escrow {contract.code}. "
                + escrow_next_step(contract, email))
    role = 'buyer' if email == contract.buyer_email else 'seller'
    return (f"Verified, welcome back. You're the {role} on escrow "
            f"{contract.code}, currently {contract.status}. "
            + escrow_next_step(contract, email))


def _looks_like_email(text):
    text = (text or '').strip()
    return '@' in text and '.' in text and ' ' not in text and len(text) <= 254


def handle_text_message(wa_id, text, base_url=''):
    """Process one inbound text and deliver the reply. Returns reply string."""
    text = (text or '').strip()
    if not text:
        return ''
    conversation = get_or_create_conversation('whatsapp', wa_id)
    link = get_link(wa_id)
    sync_conversation_owner(conversation, link)

    # 1. Bare 6-digit code: creation OTP first, then general verification.
    if OTP_MESSAGE_RE.match(text):
        resumed = _try_resume_creation(conversation, link, wa_id, text.strip(),
                                         base_url=base_url)
        if resumed is not None:
            adapter.send_text_message(wa_id, resumed)
            return resumed
        pending_email = normalize_email(
            link.pending_email or conversation.pending_email or '')
        if pending_email:
            result = consume_otp(pending_email, text.strip(), 'reauth')
            conversation.messages.create(role='user', content='[one-time code withheld]')
            if result.ok:
                link.verified_email = pending_email
                link.pending_email = ''
                link.save(update_fields=['pending_email', 'verified_email'])
                if conversation.pending_email:
                    conversation.pending_email = None
                    conversation.save(update_fields=['pending_email'])
                sync_conversation_owner(conversation, link)
                audit('whatsapp.verified', actor=pending_email)
                reply = _join_reply_after_verify(conversation, pending_email)
                if reply is None:
                    reply = VERIFIED_TEMPLATE.format(email=pending_email)
                conversation.messages.create(role='assistant', content=reply)
            else:
                conversation.messages.create(role='assistant', content=result.message)
                reply = result.message
            adapter.send_text_message(wa_id, reply)
            return reply
        adapter.send_text_message(wa_id, NO_CODE_PENDING)
        return NO_CODE_PENDING

    # 2. Escrow code shared mid-chat: treat it as "join this specific
    # escrow" without waiting for the agent. Verified participants jump
    # straight to status; everyone else is asked for their email, and the
    # code is remembered so the join completes on verification.
    match = ESCROW_CODE_RE.search(text)
    if match and not _looks_like_email(text):
        code = match.group(0)
        if conversation.pending_context.get('join_escrow_reference') != code:
            conversation.pending_context = {
                **(conversation.pending_context or {}),
                'join_escrow_reference': code,
            }
            conversation.save(update_fields=['pending_context'])
        owner = (conversation.owner_email or '').strip()
        if owner:
            from escrow.services import resume_escrow_after_verification
            conversation.messages.create(role='user', content=text)
            contract, kind, error = resume_escrow_after_verification(code, owner)
            if contract is None:
                reply = f"I couldn't continue that escrow: {error}"
            else:
                audit(f'counterparty.{kind}_via_whatsapp', actor=owner,
                      escrow=contract)
                noun = 'joined' if kind == 'joined' else 'resumed'
                reply = (f"{contract.code}: {noun} — {contract.status}. "
                         + escrow_next_step(contract, owner))
            conversation.messages.create(role='assistant', content=reply)
            adapter.send_text_message(wa_id, reply)
            return reply
        link_pending = normalize_email(link.pending_email or '')
        reply = (f"Got it — {code}. What's your email address so I can "
                 "verify you and pull it up?")
        conversation.messages.create(role='user', content=text)
        conversation.messages.create(role='assistant', content=reply)
        adapter.send_text_message(wa_id, reply)
        if link_pending:
            adapter.send_text_message(
                wa_id, CODE_SENT_TEMPLATE.format(email=link_pending))
        return reply

    # 3. Email address: issue a verification code.
    if _looks_like_email(text):
        email = normalize_email(text)
        if link.verified_email == email:
            sync_conversation_owner(conversation, link)
            reply = f"You're already verified as {email}. What would you like to do?"
            conversation.messages.create(role='user', content=text)
            conversation.messages.create(role='assistant', content=reply)
            adapter.send_text_message(wa_id, reply)
            return reply
        if issue_otp(email, 'reauth') is None:
            reply = "I could not send a code to that address right now. Please try again in a minute."
            conversation.messages.create(role='user', content=text)
            conversation.messages.create(role='assistant', content=reply)
            adapter.send_text_message(wa_id, reply)
            return reply
        link.pending_email = email
        link.save(update_fields=['pending_email'])
        audit('whatsapp.otp_requested', actor=email)
        reply = CODE_SENT_TEMPLATE.format(email=email)
        conversation.messages.create(role='user', content=text)
        conversation.messages.create(role='assistant', content=reply)
        adapter.send_text_message(wa_id, reply)
        return reply

    # 3. Anything else goes to the shared bounded agent loop. Unverified
    # senders can still ask questions and build drafts; tools that need a
    # verified actor refuse with guidance, same as the website assistant.
    reply = run_turn(conversation, text, base_url=base_url, session_key=_session_key(wa_id))
    adapter.send_text_message(wa_id, reply or '')
    return reply or ''


def handle_voice_message(wa_id, audio_bytes, base_url=''):
    """Voice note into the agent: transcribe, then process as a text turn.

    The transcript is recorded as the user message so the conversation
    keeps a readable history. Transcription failures degrade to a typed
    follow-up, never to silence.
    """
    if not audio_bytes:
        reply = ("I couldn't download that voice note. "
                 "Please try again or type your message.")
        adapter.send_text_message(wa_id, reply)
        return reply
    try:
        transcript = transcribe_audio(audio_bytes)
    except (VoiceConfigError, VoiceTranscriptionError) as exc:
        adapter.send_text_message(wa_id, str(exc))
        return str(exc)
    audit('whatsapp.voice_transcribed', actor=wa_id,
          metadata={'chars': len(transcript)})
    return handle_text_message(wa_id, transcript, base_url=base_url)
