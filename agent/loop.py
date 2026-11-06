"""Bounded agent loop: the model proposes, the backend disposes.

One turn: persist the user message, ask the model, execute any approved
tool calls against the registry (never trusting model arguments), feed
results back, and return the final reply. Consequential tools run only
when the user has confirmed the exact pending proposal; everything else
degrades to a safe message when the model is unreachable.
"""
import json
import logging
import re

from django.conf import settings
from django.utils import timezone

from agent.client import BasetenError, chat
from agent.models import AIConversation, ConversationMessage
from agent.registry import (
    CONFIRMATION_TTL_SECONDS,
    CONSEQUENTIAL_TOOLS,
    TOOLS,
    execute,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Locktrans's escrow assistant. You help users create and \
track escrows safely.

Style: reply in 1-2 short sentences, at most one question per message. \
Sound like chat, not a form. Never send a long message. Never use em dashes; use commas or periods instead.

Drafting rules, extract, don't interrogate:
- Pull every detail you can from what the user already wrote (e.g. \
"sell my laptop for 250000 to ada@example.com" already gives title, \
amount, counterparty, and creator_role=seller). Never ask for anything \
the user already told you.
- Escrow codes look like LKTR plus 5 letters/digits (e.g. LKTRaB3xK). When the user shares one, they want to join or check that escrow, ask for nothing else first except verification if needed.
- Never mention, list, reopen, or resume a draft the user has not asked about in this conversation.
- Save what you have immediately with create_escrow_draft, even partial, \
and top it up with update_escrow_draft as more arrives. Missing \
description? Reuse the title. Missing fee_payer? Assume buyer pays. \
Missing inspection window? Assume 3 days. Fold assumptions into the \
final summary instead of asking about them.
- Only ask for what cannot be guessed: usually just their own email \
address, plus the amount or the other party's email if truly absent. One \
missing thing per message, most important first.
- Guide the whole deal end to end, never send anyone to a page:
  CREATE: describe the deal, confirm terms, then the creator pastes \
  the emailed code here and the escrow exists, give them the LKTR \
  code to share.
  JOIN/CONTINUE: the user gives you the LKTR code (words like join, continue, resume, track all mean this, chat history is wiped on refresh, so this is how anyone picks up an existing escrow); get their email and \
  call request_verification_code WITH that code (it completes the \
  join when they paste the returned code). If already verified, check \
  status, show the terms, and after they confirm call \
  request_counterparty_join. Joined means the status moved, never \
  say someone has or has not joined without checking the backend.
  FUND: buyer only, status awaiting_funding. Check status first, then \
  request_payment_link after confirmation and share the link. Payment \
  is verified by the backend, never by the user's say-so.
  SHIP/RECEIVE: seller says shipped -> confirm dispatch; buyer says \
  received -> confirm receipt (inspection starts). Each step updates \
  the server and notifies the other party automatically.
  BANK: before release the seller must have payout details saved. \
  The moment receipt is confirmed, ask the SELLER for their PayPal email \
  if seller_bank_saved is false, and whenever a seller checks an \
  escrow in dispatch/transit/inspection without saved details, \
  ask then too. Do not wait until release is attempted. \
  Offer one tap first: ask "Should I send it to your verified email \
  (their address)?" and call request_seller_bank_details with \
  use_verified_email true when they agree; only ask them to type a \
  different paypal_email when they decline. Payouts go via PayPal. \
  After saving, confirm the masked email back to the seller. \
  RELEASE: buyer confirms in inspection -> request_release_approval. \
  If bank details are missing the seller is notified automatically; \
  say so and wait. After release, confirm payout to both parties.
- When asked for status or "what's next", check \
list_my_escrows first when they ask what is new or what needs them, then get_authorized_escrow_status for the one they pick, never answer from memory.
- When the draft is complete, FIRST call request_escrow_creation right \
away, the backend holds it and tells you to present the exact terms. \
THEN show the terms briefly and ask for confirmation. Always include \
the backend fee breakdown (amount, service fee, buyer pays, seller \
receives) exactly as given, never omit the fee. Never show terms \
and ask for confirmation WITHOUT calling the tool in the same turn, \
otherwise the user is asked to confirm twice.
- After the user confirms, call the SAME tool again with IDENTICAL \
arguments (copy them exactly, e.g. draft_id stays a number). Do not \
re-check, re-read, or re-validate first, just make the identical call.

Rules you must follow:
- Explain what you are about to do BEFORE doing anything consequential, \
and only proceed after the user explicitly confirms the exact terms.
- To create, fund, confirm, or dispute anything, use your tools, never \
claim success from conversation alone.
- Start helping immediately; no account or verification is needed to talk, \
quote fees, or assemble a draft.
- Creating an escrow needs two user steps: first they confirm the exact \
terms, then they paste the 6-digit code sent to the creator email. That \
code arrives as their next chat message, just continue the conversation; \
the app verifies it with the backend and tells you the result through \
your tools. Never ask the user to read a code aloud or type it anywhere \
outside this chat; never repeat a code back.
- If the user is not verified, get their email and call request_verification_code immediately, they paste the code here in chat and everything continues. Never send them to a form or page.
- You can only see escrows the verified user participates in.
- Never ask for an escrow code, email verification, or any identifying \
detail unless the user wants to join, track, or act on a specific \
escrow. No unprompted lookups, no fishing for details.
- Never mention tool names, proposals, or raw results in chat. Speak \
plainly: "send the code", "confirm the terms", never the machinery \
behind them.
- If you do not know something, say so."""

# A message that is only a 6-digit code is an OTP being pasted, not chat.
OTP_MESSAGE_RE = re.compile(r'^\s*\d{6}\s*$')
ESCROW_CODE_RE = re.compile(r'LKTR[A-Za-z0-9]{5}')


def find_pending_join_code(conversation):
    """Escrow the user meant to join, from stored context or recent chat.

    Checked after email verification succeeds so the join completes in
    the same message instead of another model round-trip.
    """
    context = conversation.pending_context or {}
    code = (context.get('join_escrow_reference') or '').strip()
    if code:
        return code
    rows = conversation.messages.filter(role='user').order_by('-id')[:10]
    for row in rows:
        match = ESCROW_CODE_RE.search(row.content or '')
        if match:
            return match.group(0)
    return ''

DEGRADED_REPLY = (
    "The AI assistant is temporarily unavailable, but nothing is lost, "
    "you can continue with the manual escrow forms on this site."
)


def redact(text):
    """Strip OTP-like content before it reaches model context or logs."""
    if OTP_MESSAGE_RE.match(text or ''):
        return '[one-time code withheld]'
    return text


def get_or_create_conversation(channel, channel_identity):
    conversation, _ = AIConversation.objects.get_or_create(
        channel=channel,
        channel_identity=channel_identity,
    )
    return conversation


def _normalize_arguments(arguments):
    """Coerce model-generated args to canonical form before comparing.

    The model sometimes re-emits an id as a string ("2" vs 2) on its
    confirmation retry. Without this, the retry never matches the
    proposal and the user is asked to confirm forever.
    """
    normalized = {}
    for key, value in (arguments or {}).items():
        if key == 'draft_id':
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        normalized[key] = value
    return normalized


def _proposal_matches(pending, name, arguments):
    if not pending or pending.get('tool') != name:
        return False
    try:
        return json.dumps(_normalize_arguments(pending.get('arguments')),
                          sort_keys=True) == json.dumps(
                              _normalize_arguments(arguments), sort_keys=True)
    except (TypeError, ValueError):
        return False


def _proposal_fresh(pending):
    proposed_at = (pending or {}).get('proposed_at')
    if not proposed_at:
        return False
    try:
        from datetime import datetime
        moment = datetime.fromisoformat(proposed_at)
    except (ValueError, TypeError):
        return False
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    age = (timezone.now() - moment).total_seconds()
    return 0 <= age < CONFIRMATION_TTL_SECONDS

def _confirmation_reminder(pending):
    """Tell the model the exact awaiting proposal so it can echo it.

    Tool calls are hidden from history (never replayed), so without this
    the model cannot know which arguments it proposed, it rebuilds the
    draft and the confirmation never matches. The reminder carries only
    backend-stored proposal data (tool + args, no OTPs); execution still
    requires the user's confirmation and full backend validation.
    """
    return (
        "Reminder: you already proposed "
        f"{pending.get('tool')} with arguments "
        f"{json.dumps(pending.get('arguments') or {}, sort_keys=True)} "
        "and the user was asked to confirm. If their latest message "
        "confirms it, call that tool NOW with IDENTICAL arguments, do "
        "not create another draft, re-read, or re-validate first. If "
        "they changed the terms or declined, handle the change instead "
        "(a new proposal replaces this one)."
    )


def propose_action(conversation, name, arguments):
    """Record the exact proposal the user is being asked to confirm."""
    conversation.pending_action = {
        'tool': name,
        'arguments': arguments or {},
        'proposed_at': timezone.now().isoformat(),
    }
    conversation.save(update_fields=['pending_action'])


def creation_terms_text(arguments):
    """Server-computed fee breakdown for a creation proposal.

    The model must present exact terms before the user confirms, but
    prose comes from the model — without backend numbers the fee is
    easily omitted. Returns '' when the draft can't be resolved, so
    callers always degrade to the plain confirmation prompt.
    """
    try:
        from escrow.forms import ContractCreationForm
        from escrow.models import EscrowDraft
        from escrow.services import calculate_service_fee
        draft_id = (arguments or {}).get('draft_id')
        draft = EscrowDraft.objects.filter(id=draft_id).first()
        if draft is None:
            return ''
        form = ContractCreationForm({
            'title': draft.title,
            'description': draft.description,
            'amount': draft.amount,
            'fee_payer': draft.fee_payer,
            'creator_email': draft.owner_email or 'you@example.com',
            'creator_role': draft.creator_role,
            'counterparty_email': draft.counterparty_email,
            'inspection_days': draft.inspection_days,
        })
        if not form.is_valid():
            return ''
        cleaned = form.cleaned_data
        fee = calculate_service_fee(cleaned['amount'])
        amount = cleaned['amount']
        if cleaned['fee_payer'] == 'buyer':
            buyer_pays, seller_gets = amount + fee, amount
        else:
            buyer_pays, seller_gets = amount, amount - fee
        return (
            f"Exact terms (USD): amount ${amount:,.2f}, "
            f"service fee ${fee:,.2f} (paid by {cleaned['fee_payer']}), "
            f"buyer pays ${buyer_pays:,.2f}, "
            f"seller receives ${seller_gets:,.2f}."
        )
    except Exception:
        logger.exception("Could not build creation terms")
        return ''


def clear_proposal(conversation):
    conversation.pending_action = {}
    conversation.save(update_fields=['pending_action'])


def restart_thread(conversation):
    """Page load means a brand-new chat, no exceptions.

    History, pending proposals, pending verification, flow context, and
    the verified owner are all cleared, and this session's unfinished
    drafts are abandoned. Nothing about an escrow is lost: its state
    lives on the backend, and the user resumes only by asking to join
    or continue with the LKTR code and re-verifying their email.
    """
    conversation.messages.all().delete()
    conversation.pending_action = {}
    conversation.pending_email = None
    conversation.pending_context = {}
    conversation.owner_email = None
    conversation.save(update_fields=[
        'pending_action', 'pending_email', 'pending_context', 'owner_email'])
    _abandon_session_drafts(conversation)
    return {}


# Backwards-compatible alias; page loads now always restart.
def reset_thread_unless_live(conversation):
    return restart_thread(conversation)


def _abandon_session_drafts(conversation):
    """Abandon this web session's unfinished drafts on restart."""
    if conversation.channel != 'web':
        return
    from escrow.models import EscrowDraft
    identity = conversation.channel_identity or ''
    session_key = identity.split('session:', 1)[-1] if 'session:' in identity else ''
    if not session_key:
        return
    EscrowDraft.objects.filter(
        owner_session=session_key, status__in=('draft', 'proposed'),
    ).update(status='abandoned')


def _maybe_auto_propose_creation(conversation, actor, session_key,
                                   draft_touched_this_turn):
    """Park a creation proposal when this turn built a complete draft.

    The model sometimes presents terms in prose without calling the
    creation tool; without a recorded proposal the user's "yes" then
    hits the gate cold and they are asked to confirm twice. Recording
    the proposal at end-of-turn (never mid-turn, so it cannot execute
    in the same turn it was made) means one confirmation always lands.

    Only fires when the model actually created or updated a draft this
    turn. Parking any complete draft ever seen would clobber a live
    proposal for another action (e.g. a dispatch confirmation), and the
    next "yes" would then execute the wrong thing — a stray escrow.
    """
    if (conversation.pending_action or {}).get('tool'):
        return
    if not draft_touched_this_turn:
        return
    from escrow.models import EscrowDraft
    qs = EscrowDraft.objects.filter(status__in=('draft', 'proposed'))
    if session_key:
        qs = qs.filter(owner_session=session_key)
    elif actor:
        qs = qs.filter(owner_email=actor)
    else:
        return
    draft = qs.order_by('-id').first()
    if draft is None:
        return
    if not (draft.title and draft.amount is not None and draft.fee_payer
            and draft.creator_role and draft.counterparty_email
            and (draft.owner_email or actor)):
        return
    propose_action(conversation, 'request_escrow_creation',
                   {'draft_id': draft.id})


AFFIRMATIVE_RE = re.compile(
    r'^\s*(yes|yep|yeah|yup|confirm|confirmed|i confirm|go ahead|proceed|'
    r'do it|ok|okay|sure|please do|yes please|yes confirm)\b',
    re.IGNORECASE)
_CHANGE_WORDS_RE = re.compile(
    r'\b(but|change|changed|instead|wait|stop|cancel|different|actually|'
    r'not now|no)\b', re.IGNORECASE)


def _is_affirmative(text):
    """Clear confirmation of the pending proposal, in natural phrasing.

    "Yes, release them." counts; "yes but change the amount" never does -
    any change/negation word sends the turn back to the model instead.
    """
    text = (text or '').strip()
    if not AFFIRMATIVE_RE.match(text):
        return False
    return not _CHANGE_WORDS_RE.search(text)


def _direct_reply(name, outcome):
    """Deterministic reply for a directly-executed confirmed proposal."""
    if not outcome.get('ok'):
        return f"That didn't go through: {outcome.get('error') or 'please try again.'}"
    data = outcome.get('data') or {}
    if name == 'request_escrow_creation':
        if data.get('verification_required'):
            return (f"I've sent a 6-digit code to {data.get('email')}. "
                    "Paste it here in chat to finish creating the escrow.")
        return (f"Your escrow {data.get('reference')} is created. "
                "Share the code with the other party so they can join.")
    if name == 'request_counterparty_join':
        return (f"Joined, escrow {data.get('reference')} is now "
                f"{data.get('status')}. {data.get('next') or ''}").strip()
    if name == 'request_payment_link':
        return (f"Here's your secure payment link: {data.get('payment_url')} "
                "- payment is verified by the backend once completed, "
                "and the seller will be notified to ship.")
    if name == 'request_delivery_confirmation':
        if data.get('status') == 'in_inspection':
            note = ("Receipt confirmed, the inspection window has "
                    "started. Say \"release\" when you're happy, or "
                    "\"dispute\" if something is wrong.")
            if data.get('seller_bank_saved') is False:
                note += (" The seller has been asked for their payout "
                         "bank details, which are needed before release.")
            return note
        return ("Marked as dispatched, the buyer has been notified. "
                "I'll update you when they confirm receipt.")
    if name == 'request_release_approval':
        return (f"Funds released, the seller has been paid. "
                f"Escrow {data.get('reference')} is complete.")
    if name == 'request_seller_bank_details':
        if data.get('already_saved'):
            return (f"Payout email already saved: {data.get('paypal_email')} "
                    f"({data.get('masked')}).")
        return (f"Saved. Payouts will go to {data.get('paypal_email')} "
                f"({data.get('masked')}). "
                "The buyer can now release the funds.")
    if name == 'request_dispute_submission':
        return ("Dispute raised, funds are frozen until it's resolved. "
                "Our team will review the escrow.")
    if name == 'request_counterparty_invitation':
        return (f"Share escrow code {data.get('code')} with the other party "
                "so they can join and verify.")
    return "Done, that's completed."


def _try_direct_confirmation(conversation, user_text, actor, base_url, session_key):
    """Execute a fresh pending proposal on a clear 'yes', no model call.

    The model occasionally re-presents terms in prose instead of
    re-calling the tool, which made users confirm twice. A bare
    affirmative reply to a recorded proposal is unambiguous, so the
    backend executes the exact stored arguments deterministically.
    Returns the reply string, or None to fall through to the model.
    """
    pending = conversation.pending_action or {}
    name = pending.get('tool')
    if name not in CONSEQUENTIAL_TOOLS:
        return None
    if not _proposal_fresh(pending):
        return None
    if not _is_affirmative(user_text):
        return None
    arguments = pending.get('arguments') or {}
    clear_proposal(conversation)
    ConversationMessage.objects.create(
        conversation=conversation, role='assistant', content='',
        tool_name=name,
        tool_arguments=_redact_tool_arguments(name, arguments))
    outcome = execute(name, arguments, actor_email=actor,
                      base_url=base_url, session_key=session_key,
                      conversation=conversation)
    ConversationMessage.objects.create(
        conversation=conversation, role='tool',
        content=json.dumps(outcome), tool_name=name)
    data = outcome.get('data') or {}
    verification_email = data.get('verification_email') or (
        data.get('email') if data.get('verification_required') else None)
    if outcome.get('ok') and verification_email:
        conversation.pending_action = {
            'tool': name,
            'arguments': arguments,
            'proposed_at': timezone.now().isoformat(),
            'verification_email': verification_email,
            'draft_id': data.get('draft_id'),
        }
        conversation.save(update_fields=['pending_action'])
    reply = _direct_reply(name, outcome)
    ConversationMessage.objects.create(
        conversation=conversation, role='assistant', content=reply)
    return reply


def confirmed(conversation, name, arguments):
    pending = conversation.pending_action or {}
    return _proposal_matches(pending, name, arguments) and _proposal_fresh(pending)


SENSITIVE_ARG_KEYS = {'paypal_email', 'account_number', 'bank_code', 'code', 'otp', 'password'}

SENSITIVE_TOOL_ARGS = {
    'request_seller_bank_details': {'paypal_email', 'account_number'},
    'create_escrow_draft': set(),
    'update_escrow_draft': set(),
    'get_escrow_draft': set(),
}


def _redact_tool_arguments(name, arguments):
    """Persist tool arguments with secrets masked.

    WhatsApp threads live indefinitely and bank details already flowed
    through chat rows once. Masking here keeps history/audit safe while
    leaving every live value usable in the same turn it was captured.
    """
    arguments = dict(arguments or {})
    sensitive = set(SENSITIVE_ARG_KEYS) | SENSITIVE_TOOL_ARGS.get(name, set())
    for key in list(arguments.keys()):
        if key in sensitive or 'otp' in key.lower() or 'code' in key.lower():
            arguments[key] = '[withheld]'
    return arguments


def _history_messages(conversation, limit=20):
    """Replayable history: plain user/assistant turns only.

    Tool calls and results are internal reasoning, never replayed, so
    nothing can be re-executed from history and no call ids need persisting.
    """
    rows = conversation.messages.order_by('-id')[:limit]
    history = []
    for row in reversed(list(rows)):
        if row.role == 'user':
            history.append({'role': 'user', 'content': redact(row.content)})
        elif row.role == 'assistant' and not row.tool_name and row.content:
            history.append({'role': 'assistant', 'content': row.content})
    return history


def run_turn(conversation, user_text, base_url='', session_key=''):
    """Run one bounded turn. Always returns a reply string, never raises."""
    actor = (conversation.owner_email or '').strip() or None
    ConversationMessage.objects.create(
        conversation=conversation, role='user', content=user_text,
    )
    direct = _try_direct_confirmation(
        conversation, user_text, actor, base_url, session_key)
    if direct is not None:
        return direct
    model_messages = (
        [{'role': 'system', 'content': SYSTEM_PROMPT}]
        + _history_messages(conversation)
    )
    if actor:
        model_messages.append({'role': 'system', 'content':
            f"This chat is verified as {actor}, that is the user you are "
            f"talking to. Never ask for their email again; use it with your "
            f"tools directly."})
    pending = conversation.pending_action or {}
    if pending.get('tool'):
        if _proposal_fresh(pending):
            model_messages.append(
                {'role': 'system',
                 'content': _confirmation_reminder(pending)})
        else:
            # Stale proposal: drop it so the model starts clean instead
            # of echoing dead arguments into another gated round-trip.
            clear_proposal(conversation)
    max_iterations = getattr(settings, 'AGENT_MAX_ITERATIONS', 8)
    draft_touched_this_turn = False

    try:
        for _ in range(max_iterations):
            try:
                result = chat(model_messages, tools=TOOLS)
            except BasetenError:
                logger.exception("Agent inference failed; degrading gracefully")
                return DEGRADED_REPLY

            assistant_text = result.get('text') or ''
            calls = result.get('tool_calls') or []
            ConversationMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=assistant_text,
            )
            if not calls:
                _maybe_auto_propose_creation(
                    conversation, actor, session_key, draft_touched_this_turn)
                return assistant_text or DEGRADED_REPLY

            model_messages.append({
                'role': 'assistant',
                'content': assistant_text,
                'tool_calls': [
                    {'id': c['id'], 'type': 'function',
                     'function': {'name': c['name'],
                                 'arguments': json.dumps(c['arguments']) }}
                    for c in calls
                ],
            })
            for call in calls:
                name, arguments = call['name'], call['arguments']
                if name in ('create_escrow_draft', 'update_escrow_draft'):
                    draft_touched_this_turn = True
                ConversationMessage.objects.create(
                    conversation=conversation,
                    role='assistant',
                    content='',
                    tool_name=name,
                    tool_arguments=_redact_tool_arguments(name, arguments),
                )
                if name in CONSEQUENTIAL_TOOLS and not confirmed(
                    conversation, name, arguments
                ):
                    propose_action(conversation, name, arguments)
                    prompt = (
                        'CONFIRMATION_REQUIRED: present the exact terms to '
                        'the user and ask for explicit confirmation first. '
                        'Do not call this tool again until they confirm.'
                    )
                    if name == 'request_escrow_creation':
                        terms = creation_terms_text(arguments)
                        if terms:
                            prompt += f' {terms} Always show this fee breakdown.'
                    outcome = {'ok': False, 'error': prompt}
                else:
                    if name in CONSEQUENTIAL_TOOLS:
                        clear_proposal(conversation)
                    outcome = execute(
                        name, arguments,
                        actor_email=actor, base_url=base_url,
                        session_key=session_key,
                        conversation=conversation,
                    )
                    data = outcome.get('data') or {}
                    verification_email = data.get(
                        'verification_email') or (
                        data.get('email') if data.get('verification_required') else None
                    )
                    if outcome.get('ok') and verification_email:
                        # Creation is parked on email proof: remember what the
                        # user is verifying for so the chat view can resume it
                        # when the code arrives as the next chat message.
                        conversation.pending_action = {
                            'tool': name,
                            'arguments': arguments,
                            'proposed_at': timezone.now().isoformat(),
                            'verification_email': verification_email,
                            'draft_id': data.get('draft_id'),
                        }
                        conversation.save(update_fields=['pending_action'])
                ConversationMessage.objects.create(
                    conversation=conversation,
                    role='tool',
                    content=json.dumps(outcome),
                    tool_name=name,
                )
                model_messages.append({
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'content': json.dumps(outcome),
                })
        logger.warning("Agent hit iteration cap; degrading gracefully")
        return DEGRADED_REPLY
    except Exception:
        logger.exception("Agent loop failed; degrading gracefully")
        return DEGRADED_REPLY
