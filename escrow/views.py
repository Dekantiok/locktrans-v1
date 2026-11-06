import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import ContractCreationForm, SettlementVaultForm
from .gateways import get_payment_gateway
from .invitations import send_counterparty_invitation
from .lifecycle import notify_event
from .models import (
    EscrowContract,
    PayoutRecord,
    SettlementVault,
    TransitionError,
    WebhookEvent,
)
from .services import (
    calculate_service_fee,
    confirm_funding_payment,
    consume_otp,
    contracts_for_email,
    create_escrow_from_validated,
    issue_otp,
    release_escrow_funds,
)
from .utils import normalize_email

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    'pending_verification': 'Pending Verification',
    'awaiting_counterparty': 'Awaiting Counterparty',
    'awaiting_funding': 'Awaiting Funding',
    'awaiting_dispatch': 'Awaiting Dispatch',
    'in_transit': 'In Transit',
    'in_inspection': 'In Inspection',
    'pending_payout': 'Funds Released',
    'completed': 'Completed',
    'disputed': 'Disputed',
    'cancelled': 'Cancelled',
}

STATUS_BADGES = {
    'pending_verification': 'bg-warning text-dark',
    'awaiting_counterparty': 'bg-dark text-white',
    'awaiting_funding': 'bg-primary-custom',
    'awaiting_dispatch': 'bg-warning text-dark',
    'in_transit': 'bg-secondary',
    'in_inspection': 'bg-primary-custom',
    'pending_payout': 'bg-success',
    'completed': 'bg-success',
    'disputed': 'bg-danger',
    'cancelled': 'bg-secondary',
}

# Human-readable summary of where an escrow sits in the lifecycle.
STATUS_STAGE = {
    'pending_verification': 1,
    'awaiting_counterparty': 2,
    'awaiting_funding': 3,
    'awaiting_dispatch': 4,
    'in_transit': 5,
    'in_inspection': 6,
    'pending_payout': 7,
    'completed': 8,
    'disputed': 8,
    'cancelled': 8,
}

MONEY_STAGES = 8


def money(value):
    return f"${value:,.2f}"


def dollars(value):
    return f"${value:,.2f}"


def get_contract_verified_email(request, contract):
    """Email this device verified for THIS contract, or ''.

    Buyer and seller are different people on different devices, so
    verification is scoped per escrow code: this browser holds at most
    one role per contract. The old global ``verified_emails`` list is
    only consulted as a one-party legacy fallback; a legacy session
    holding both parties is treated as unverified to force a fresh,
    single-role verify.
    """
    code = contract.code
    scoped = request.session.get('verified_contracts', {})
    email = normalize_email(scoped.get(code, ''))
    if email and email in contract.parties:
        return email
    my_email = normalize_email(request.session.get('my_escrow_verified', ''))
    if my_email and my_email in contract.parties:
        return my_email
    legacy = [normalize_email(e) for e in request.session.get('verified_emails', [])]
    matched = [e for e in legacy if e in contract.parties]
    if len(matched) == 1:
        return matched[0]
    return ''


def get_verified_emails(request):
    # Legacy accessor; new code uses the contract-scoped variant.
    return [normalize_email(e) for e in request.session.get('verified_emails', [])]


def set_contract_verified_email(request, code, email):
    email = normalize_email(email)
    scoped = dict(request.session.get('verified_contracts', {}))
    scoped[code] = email
    request.session['verified_contracts'] = scoped
    # Rotate the session key on privilege change to prevent fixation:
    # a pre-login session id must never become a verified-party session.
    try:
        request.session.cycle_key()
    except Exception:
        pass


def add_verified_email(request, email):
    # Unsafe no-op kept for old imports; use set_contract_verified_email.
    return None


def is_party(contract, request):
    return bool(get_contract_verified_email(request, contract))


def home_view(request):
    from agent.loop import get_or_create_conversation, restart_thread
    if not request.session.session_key:
        request.session.save()
    conversation = get_or_create_conversation(
        'web', f"session:{request.session.session_key}"
    )
    # Landing on the home page also restarts the chat from zero.
    request.session.pop('chat_verified_email', None)
    request.session.pop('chat_pending_email', None)
    pending = restart_thread(conversation)
    context = {
        'messages': list(conversation.messages.order_by('id')),
        'pending': pending or None,
        'owner_email': conversation.owner_email,
    }
    return render(request, 'home.html', context)


def join_escrow_page(request):
    error_message = None
    if request.method == 'POST':
        code = request.POST.get('code', '').strip()
        email = normalize_email(request.POST.get('email', ''))
        contract = EscrowContract.objects.filter(code=code).first()
        if contract is None or email != contract.counterparty_email:
            # One indistinct answer: never reveal whether the code
            # exists or which email it is linked to.
            error_message = (
                "That code and email could not be verified. "
                "Check both and try again."
            )
        elif contract.status != 'awaiting_counterparty':
            error_message = "This escrow is not currently accepting counterparty verification."
        elif issue_otp(email, 'join') is None:
            error_message = (
                "We could not send a verification code right now. "
                "Please wait a moment and try again."
            )
        else:
            return redirect('counterparty_verify_otp', code=contract.code)
    return render(request, 'escrow/join_escrow.html', {
        'error_message': error_message,
        'initial_code': request.GET.get('code', ''),
    })


def create_contract(request):
    if request.method == 'POST':
        form = ContractCreationForm(request.POST)
        if form.is_valid():
            contract = create_escrow_from_validated(form.cleaned_data)
            if issue_otp(contract.creator_email, 'create') is None:
                messages.error(
                    request,
                    "Your escrow was created but we could not email a verification code. "
                    "Please contact support.",
                )
            return redirect('verify_otp', code=contract.code)
    else:
        form = ContractCreationForm()
    return render(request, 'escrow/create_contract.html', {'form': form})


def _verify_and_advance(request, contract, intent, next_status, email):
    """
    Shared body for the two OTP verification views.

    One device holds one role per contract: if this browser already
    verified as the OTHER party for this escrow, refuse before burning
    an OTP attempt. Returns (redirect_response, error_message).
    consume_otp is called exactly once per submission so an attempt is
    only ever counted once.
    """
    email = normalize_email(email)
    existing = get_contract_verified_email(request, contract)
    if existing and existing != email:
        return None, (
            "This device already verified as "
            f"{existing}. The other party must verify on their own device."
        )
    result = consume_otp(email, request.POST.get('otp'), intent)
    if not result.ok:
        return None, result.message
    # The inbox was just proven by OTP: record which side verified. The
    # status tool and dashboard read these flags, without them a joined
    # counterparty still looks "unverified".
    confirmed_fields = {}
    if email == contract.buyer_email:
        confirmed_fields['buyer_confirmed'] = True
    elif email == contract.seller_email:
        confirmed_fields['seller_confirmed'] = True
    try:
        contract.transition_to(next_status, **confirmed_fields)
    except TransitionError:
        logger.warning(
            "Rejected %s -> %s on contract %s", contract.status, next_status, contract.code
        )
        return redirect('contract_detail', code=contract.code), None
    set_contract_verified_email(request, contract.code, email)
    return redirect('contract_detail', code=contract.code), None


def verify_otp_view(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    error_message = None
    if request.method == 'POST':
        was = contract.status
        redirect_response, error_message = _verify_and_advance(
            request, contract, 'create', 'awaiting_counterparty',
            contract.creator_email,
        )
        if redirect_response is not None:
            contract.refresh_from_db()
            if was != 'awaiting_counterparty' and contract.status == 'awaiting_counterparty':
                send_counterparty_invitation(
                    contract, base_url=request.build_absolute_uri('/')[:-1])
            return redirect_response
    return render(
        request, 'escrow/verify_otp.html',
        {'contract': contract, 'error_message': error_message},
    )


def counterparty_verify_otp(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    error_message = None
    if request.method == 'POST':
        was = contract.status
        redirect_response, error_message = _verify_and_advance(
            request, contract, 'join', 'awaiting_funding',
            contract.counterparty_email,
        )
        if redirect_response is not None:
            contract.refresh_from_db()
            if was != 'awaiting_funding' and contract.status == 'awaiting_funding':
                notify_event('counterparty_joined', contract)
            return redirect_response
    return render(
        request, 'escrow/counterparty_verify_otp.html',
        {'contract': contract, 'error_message': error_message},
    )


def contract_detail(request, code):
    contract = get_object_or_404(EscrowContract, code=code)

    if not is_party(contract, request):
        return render(
            request, 'escrow/contract_locked.html',
            {'contract': contract, 'status_label': STATUS_LABELS.get(contract.status, '')},
            status=403,
        )

    verified_email = get_contract_verified_email(request, contract)
    is_buyer = verified_email == contract.buyer_email
    is_seller = verified_email == contract.seller_email
    vault = getattr(contract, 'settlement_vault', None)

    context = {
        'contract': contract,
        'status_label': STATUS_LABELS.get(contract.status, contract.status),
        'badge_class': STATUS_BADGES.get(contract.status, 'bg-secondary'),
        'stage': STATUS_STAGE.get(contract.status, 1),
        'total_stages': MONEY_STAGES,
        'progress_pct': int(
            (STATUS_STAGE.get(contract.status, 1) / MONEY_STAGES) * 100
        ),
        'is_buyer': is_buyer,
        'is_seller': is_seller,
        'role_label': 'Buyer' if is_buyer else ('Seller' if is_seller else 'Observer'),
        'is_live': not contract.is_terminal and contract.status != 'disputed',
        'inspection_ends_display': contract.inspection_ends.strftime('%d %b %Y, %H:%M')
        if contract.inspection_ends else None,
        'inspection_expired': contract.is_inspection_expired,
        'show_payment_button': (contract.status == 'awaiting_funding' and is_buyer),
        'show_seller_dispatch_button': (contract.status == 'awaiting_dispatch' and is_seller),
        'show_buyer_confirm_receipt': (contract.status == 'in_transit' and is_buyer),
        'show_buyer_release_funds': (contract.status == 'in_inspection' and is_buyer),
        'show_buyer_raise_dispute': (contract.status == 'in_inspection' and is_buyer),
        'show_seller_raise_dispute': (
            contract.status in ('awaiting_dispatch', 'in_transit', 'in_inspection')
            and is_seller
        ),
        'show_cancel_button': (
            contract.status in EscrowContract.PRE_FUNDING_STATUSES
            and (is_buyer or is_seller)
        ),
        'show_seller_vault_form': (
            contract.status in ('awaiting_dispatch', 'in_transit', 'in_inspection')
            and is_seller and not vault
        ),
        'show_seller_waiting': (
            contract.status in ('in_transit', 'in_inspection') and is_seller and bool(vault)
        ),
        'show_seller_funds_released': (contract.status == 'pending_payout' and is_seller),
        'show_seller_completed': (contract.status == 'completed' and is_seller),
        'show_buyer_completed': (contract.status == 'completed' and is_buyer),
        'show_settlement_vault': bool(vault),
        'show_disputed_notice': (contract.status == 'disputed'),
        # One device holds one role per contract, so both sides are never
        # shown together. Kept as False for template compatibility.
        'show_role_overlap_notice': False,
        'formatted_amount': money(contract.amount),
        'formatted_fee': money(contract.service_fee),
        'formatted_seller_receives': money(contract.seller_receives),
    }

    if vault:
        # The seller entered this PayPal email, so only the seller sees it in
        # full. The buyer gets a masked reference.
        context['vault_bank_name'] = 'PayPal'
        context['vault_account_number'] = (
            vault.paypal_email if is_seller else vault.masked_account_number
        )
        context['vault_account_name'] = (
            vault.paypal_email if is_seller else vault.masked_account_number
        )
        context['vault_is_masked'] = not is_seller

    # HTMX polls for the panel only, so a status change swaps the card in place
    # instead of replacing the whole page.
    template = (
        'escrow/_contract_panel.html'
        if request.headers.get('HX-Request') == 'true'
        else 'escrow/contract_detail.html'
    )
    return render(request, template, context)


@require_POST
def initiate_payment(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    if get_contract_verified_email(request, contract) != contract.buyer_email:
        return HttpResponseForbidden("Only the verified buyer can fund this escrow.")
    if contract.status != 'awaiting_funding':
        return redirect('contract_detail', code=contract.code)

    gateway = get_payment_gateway()
    return_url = request.build_absolute_uri(
        reverse('payment_callback', kwargs={'code': contract.code})
    )
    result = gateway.initialize_vault_payment(contract, return_url)
    if result.get('status') and result.get('auth_url'):
        # PayPal returns an order id; persist it so the callback + webhook
        # can verify/capture against the same reference.
        order_id = result.get('order_id')
        if order_id and order_id != contract.gateway_reference:
            EscrowContract.objects.filter(pk=contract.pk).update(
                gateway_reference=order_id)
        return redirect(result['auth_url'])

    logger.error("PayPal init failed for %s: %r", contract.code, result)
    messages.error(request, "We could not start the payment. Please try again in a moment.")
    return redirect('contract_detail', code=contract.code)


def payment_callback(request, code):
    """
    Browser return leg from PayPal Checkout (return_url with ?token=ORDERID).

    The token is the PayPal order id. We capture it server-side, then confirm
    against OUR stored reference. The browser query string is never trusted
    beyond identifying which order to capture, and the move is a conditional
    UPDATE, so replays can't double-advance.
    """
    contract = get_object_or_404(EscrowContract, code=code)
    if contract.status == 'awaiting_funding':
        token = (request.GET.get('token') or '').strip()
        gateway = get_payment_gateway()
        # If PayPal redirected with an order id, capture it first.
        if token and hasattr(gateway, 'capture_order'):
            # Adopt the token as canonical if we don't have an order yet
            # (e.g. init happened in another session).
            if not contract.gateway_reference or contract.gateway_reference.startswith(contract.code + '_'):
                EscrowContract.objects.filter(pk=contract.pk).update(
                    gateway_reference=token)
                contract.refresh_from_db()
            reference = contract.gateway_reference or token
            try:
                capture = gateway.capture_order(reference if reference != token else token)
                _ = capture
            except Exception:
                logger.exception(
                    "PayPal capture raised for contract %s", contract.code
                )
        if contract.gateway_reference:
            try:
                result = gateway.verify_transaction(
                    contract.gateway_reference
                )
            except Exception:
                logger.exception(
                    "Payment verification raised for contract %s", contract.code
                )
                result = None
            data = (
                result.get('data', {}) if isinstance(result, dict) else {}
            )
            if (
                isinstance(result, dict) and result.get('status')
                and isinstance(data, dict) and data.get('status') == 'success'
                and (not data.get('reference') or data.get('reference') == contract.gateway_reference)
                and data.get('amount') is not None
                and confirm_funding_payment(
                    contract, data.get('amount'), currency=data.get('currency'))
            ):
                messages.success(
                    request,
                    "Payment confirmed. The seller has been notified to dispatch.",
                )
                return redirect('contract_detail', code=contract.code)
            contract.refresh_from_db()
    if contract.status == 'awaiting_funding':
        messages.info(
            request,
            "Thanks. We're confirming your payment. This usually takes a few seconds.",
        )
    return redirect('contract_detail', code=contract.code)


def _paypal_amount_to_kobo(resource):
    amt = (resource.get('amount') or {})
    try:
        return int(float(amt.get('value', '0')) * 100), (amt.get('currency_code') or 'USD')
    except Exception:
        return None, (amt.get('currency_code') or 'USD')


@csrf_exempt
def paypal_webhook(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Method not allowed")
    event = get_payment_gateway().verify_incoming_webhook(request.headers, request.body)
    if not event:
        WebhookEvent.objects.create(
            provider='paypal', event='unknown',
            signature_valid=False, payload={},
        )
        return HttpResponseBadRequest("Invalid signature")

    event_type = event.get('event_type')
    resource = event.get('resource') or {}
    event_id = event.get('id')
    if event_id:
        recorded, created = WebhookEvent.objects.get_or_create(
            provider='paypal', event=event_type or 'unknown', event_id=event_id,
            defaults={'signature_valid': True, 'payload': event},
        )
        if not created and recorded.processed:
            logger.info("Ignoring duplicate PayPal event %s (%s)", event_type, event_id)
            return HttpResponse("Duplicate event", status=200)
    else:
        recorded = WebhookEvent.objects.create(
            provider='paypal', event=event_type or 'unknown',
            signature_valid=True, payload=event,
        )

    def mark_processed():
        recorded.processed = True
        recorded.processed_at = now()
        recorded.save(update_fields=['processed', 'processed_at'])

    # Payout item completed -> complete escrow
    if event_type in ('PAYMENT.PAYOUTS-ITEM.SUCCEEDED', 'PAYMENT.PAYOUTSBATCH.SUCCESS'):
        batch_id = resource.get('payout_batch_id') or (resource.get('batch_header') or {}).get('payout_batch_id')
        sender_batch = resource.get('sender_batch_id') or (resource.get('batch_header') or {}).get('sender_batch_id')
        from django.db.models import Q
        from .services import audit
        payout = None
        if batch_id:
            payout = PayoutRecord.objects.filter(
                Q(transfer_reference=batch_id) | Q(provider_transfer_code=batch_id)
            ).order_by('-id').first()
        if payout is None and sender_batch:
            payout = PayoutRecord.objects.filter(
                Q(transfer_reference=sender_batch) | Q(provider_response__sender_batch_id=sender_batch)
            ).order_by('-id').first()
        if payout:
            payout.status = 'success'
            payout.save(update_fields=['status'])
            moved = EscrowContract.objects.filter(
                pk=payout.escrow.pk, status='pending_payout').update(status='completed')
            if moved:
                payout.escrow.refresh_from_db()
                audit('payout.completed', escrow=payout.escrow,
                      metadata={'payout_id': payout.id, 'via': 'paypal_webhook'})
                notify_event('payout_completed', payout.escrow)
        mark_processed()
        return HttpResponse("Webhook received", status=200)

    if event_type in ('PAYMENT.CAPTURE.COMPLETED', 'CHECKOUT.ORDER.APPROVED'):
        # Resolve order -> capture to get canonical amount/currency.
        order_id = resource.get('supplementary_data', {}).get('related_ids', {}).get('order_id')
        capture_id = resource.get('id') if event_type == 'PAYMENT.CAPTURE.COMPLETED' else None
        # Try to find contract by gateway_reference == order_id, else by custom invoice/reference
        contract = None
        if order_id:
            contract = EscrowContract.objects.filter(gateway_reference=order_id).first()
        # PayPal custom_id / invoice_id fallbacks
        custom = resource.get('custom_id') or resource.get('invoice_id')
        if contract is None and custom:
            contract = EscrowContract.objects.filter(gateway_reference=custom).first()
            if contract is None:
                contract = EscrowContract.objects.filter(code=custom).first()
        if contract is None and capture_id:
            # Last resort: verify via gateway to map capture -> order
            contract = None
        if contract is None:
            logger.warning("PayPal %s for unknown reference", event_type)
            mark_processed()
            return HttpResponse("Unknown reference", status=404)
        if contract.status != 'awaiting_funding':
            logger.info("Ignoring %s for %s in status %s", event_type, contract.code, contract.status)
            mark_processed()
            return HttpResponse("Already processed", status=200)
        paid_kobo, currency = _paypal_amount_to_kobo(resource)
        expected = contract.total_charge_kobo
        if paid_kobo is not None and paid_kobo != expected:
            logger.error("Amount mismatch on %s: got %s expected %s", contract.code, paid_kobo, expected)
            return HttpResponse("Amount mismatch", status=400)
        if currency and str(currency).upper() != contract.currency.upper():
            logger.error("Currency mismatch on %s: got %s expected %s", contract.code, currency, contract.currency)
            return HttpResponse("Currency mismatch", status=400)
        moved = confirm_funding_payment(
            contract, paid_kobo if paid_kobo is not None else expected, currency=currency)
        if moved:
            logger.info("Payment confirmed for contract %s", contract.code)
        mark_processed()
        return HttpResponse("Webhook received", status=200)

    logger.info("Ignoring PayPal event %s", event_type)
    mark_processed()
    return HttpResponse("Webhook received", status=200)


def _require_role(request, contract, role):
    email = contract.buyer_email if role == 'buyer' else contract.seller_email
    if get_contract_verified_email(request, contract) != email:
        return HttpResponseForbidden(
            f"Only the verified {role} can perform this action."
        )
    return None


@require_POST
def seller_confirm_dispatch(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    try:
        contract.transition_to('in_transit', seller_confirmed=True)
    except TransitionError:
        pass
    else:
        notify_event('dispatch_confirmed', contract,
                     actor=get_contract_verified_email(request, contract))
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_confirm_receipt(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied
    inspection_ends = now() + datetime.timedelta(days=contract.inspection_days)
    try:
        contract.transition_to('in_inspection', inspection_ends=inspection_ends)
    except TransitionError:
        pass
    else:
        notify_event('receipt_confirmed', contract,
                     actor=get_contract_verified_email(request, contract))
        if not SettlementVault.objects.filter(contract=contract).exists():
            notify_event('bank_details_needed', contract)
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_release_funds(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied

    result = release_escrow_funds(contract)
    if result.success:
        messages.success(request, "Funds released. The seller has been paid.")
    else:
        messages.error(request, result.message)
    return redirect('contract_detail', code=contract.code)


@require_POST
def buyer_raise_dispute(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'buyer')
    if denied:
        return denied
    try:
        contract.transition_to('disputed')
    except TransitionError:
        pass
    else:
        notify_event('disputed', contract,
                     actor=get_contract_verified_email(request, contract))
    messages.info(
        request,
        "The dispute has been raised. Funds are held until our team reviews the escrow.",
    )
    return redirect('contract_detail', code=contract.code)


@require_POST
def seller_raise_dispute(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    try:
        contract.transition_to('disputed')
    except TransitionError:
        pass
    else:
        notify_event('disputed', contract,
                     actor=get_contract_verified_email(request, contract))
    messages.info(
        request,
        "The dispute has been raised. Funds are held until our team reviews the escrow.",
    )
    return redirect('contract_detail', code=contract.code)


@require_POST
def cancel_escrow(request, code):
    """Party-initiated cancel, allowed only before money has moved.

    Once funded, cancellation needs an ops refund via the payment provider
    dashboard/API, so funded contracts redirect with an explanatory message
    instead of transitioning here.
    """
    contract = get_object_or_404(EscrowContract, code=code)
    if not is_party(contract, request):
        return HttpResponseForbidden("Only a verified party can cancel this escrow.")
    if contract.status not in EscrowContract.PRE_FUNDING_STATUSES:
        messages.error(
            request,
            "This escrow is already funded. Cancellation now requires a refund, "
            "please contact support so our team can review it.",
        )
        return redirect('contract_detail', code=contract.code)
    try:
        contract.transition_to('cancelled')
    except TransitionError:
        pass
    messages.info(request, "The escrow has been cancelled. No payment was taken.")
    return redirect('contract_detail', code=contract.code)


def seller_add_vault(request, code):
    contract = get_object_or_404(EscrowContract, code=code)
    denied = _require_role(request, contract, 'seller')
    if denied:
        return denied
    if contract.status not in ('awaiting_dispatch', 'in_transit', 'in_inspection'):
        return redirect('contract_detail', code=contract.code)
    if hasattr(contract, 'settlement_vault'):
        return redirect('contract_detail', code=contract.code)

    seller_email = get_contract_verified_email(request, contract)

    if request.method == 'POST':
        form = SettlementVaultForm(request.POST, verified_email=seller_email)
        if form.is_valid():
            vault = form.save(commit=False)
            vault.paypal_email = form.get_paypal_email()
            vault.contract = contract
            vault.account_name = vault.paypal_email[:150]
            vault.bank_name = 'PayPal'
            vault.save()
            messages.success(
                request,
                f"PayPal payout email saved ({vault.paypal_email}).",
            )
            return redirect('contract_detail', code=contract.code)
    else:
        form = SettlementVaultForm(verified_email=seller_email)

    return render(
        request, 'escrow/add_vault.html',
        {'form': form, 'contract': contract,
         'verified_email': seller_email},
    )


def my_escrow(request):
    email = request.session.get('my_escrow_pending_email')
    if email and request.session.get('my_escrow_verified') == email:
        return redirect('my_escrow_list')

    error_message = None
    if request.method == 'POST':
        email = normalize_email(request.POST.get('email', ''))
        if not email:
            error_message = "Please enter a valid email address."
        elif not contracts_for_email(email).exists():
            error_message = "No active escrows found for this email."
        elif issue_otp(email, 'reauth') is None:
            error_message = (
                "We could not send a verification code right now. "
                "Please wait a moment and try again."
            )
        else:
            request.session['my_escrow_pending_email'] = email
            return redirect('my_escrow_verify_otp')

    return render(request, 'escrow/my_escrow_email.html', {'error_message': error_message})


def my_escrow_verify_otp(request):
    email = request.session.get('my_escrow_pending_email')
    if not email:
        return redirect('my_escrow')

    error_message = None
    if request.method == 'POST':
        result = consume_otp(email, request.POST.get('otp'), 'reauth')
        if result.ok:
            request.session['my_escrow_verified'] = email
            del request.session['my_escrow_pending_email']
            return redirect('my_escrow_list')
        error_message = result.message

    return render(
        request, 'escrow/my_escrow_otp.html',
        {'email': email, 'error_message': error_message},
    )


def my_escrow_list(request):
    email = request.session.get('my_escrow_verified')
    if not email:
        return redirect('my_escrow')

    escrow_data = []
    for contract in contracts_for_email(email).order_by('-id'):
        escrow_data.append({
            'code': contract.code,
            'title': contract.title,
            'description': contract.description,
            'status': contract.status,
            'status_label': STATUS_LABELS.get(contract.status, contract.status),
            'badge_class': STATUS_BADGES.get(contract.status, 'bg-secondary'),
            'user_role': (
                contract.creator_role if contract.creator_email == email
                else ('seller' if contract.creator_role == 'buyer' else 'buyer')
            ),
            'formatted_amount': money(contract.amount),
        })

    return render(
        request, 'escrow/my_escrow_list.html',
        {'escrows': escrow_data, 'email': email},
    )


__all__ = [
    'home_view', 'join_escrow_page', 'create_contract', 'verify_otp_view',
    'contract_detail', 'counterparty_verify_otp', 'initiate_payment',
    'payment_callback', 'paypal_webhook', 'seller_confirm_dispatch',
    'buyer_confirm_receipt', 'buyer_release_funds', 'buyer_raise_dispute',
    'seller_raise_dispute', 'cancel_escrow',
    'seller_add_vault', 'my_escrow', 'my_escrow_verify_otp', 'my_escrow_list',
    'add_verified_email', 'get_verified_emails', 'is_party',
    'get_contract_verified_email', 'set_contract_verified_email',
]


def demo_pay_page(request, code):
    """Simulated checkout, demo mode only. Confirming here runs the exact
    same backend funding confirmation a verified PayPal webhook would."""
    if not getattr(settings, 'DEMO_MODE', False) or not getattr(
            settings, 'DEMO_SIMULATE_PAYMENTS', True):
        raise Http404()
    contract = get_object_or_404(EscrowContract, code=code)
    confirmed = False
    if request.method == 'POST' and contract.status == 'awaiting_funding':
        confirmed = confirm_funding_payment(
            contract, contract.total_charge_kobo, currency=contract.currency)
        contract.refresh_from_db()
    return render(request, 'escrow/demo_pay.html', {
        'contract': contract, 'confirmed': confirmed,
    })
