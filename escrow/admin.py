from django.contrib import admin, messages
from django.db.models import Q
from django.utils import timezone

from .gateways import get_payment_gateway
from .models import (
    AuditEvent,
    AuthOTP,
    EscrowContract,
    Identity,
    PaymentTransaction,
    PayoutRecord,
    RefundRecord,
    SettlementVault,
    VerifiedContact,
    WebhookEvent,
)
from .services import confirm_funding_payment, initiate_refund, settle_pending_payout


@admin.register(EscrowContract)
class EscrowContractAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'title', 'amount', 'status', 'creator_email',
        'counterparty_email', 'inspection_ends', 'created_at',
    )
    list_filter = ('status', 'creator_role', 'fee_payer')
    search_fields = ('code', 'creator_email', 'counterparty_email', 'title')
    readonly_fields = (
        'code', 'gateway_reference', 'service_fee', 'created_at',
        'inspection_ends', 'buyer_confirmed', 'seller_confirmed',
    )
    date_hierarchy = 'created_at'
    actions = (
        'verify_pending_payment',
        'resolve_dispute_to_payout',
        'resolve_dispute_cancelled',
        'cancel_contract',
        'extend_inspection_window',
    )

    @admin.action(description="Verify pending payment with PayPal")
    def verify_pending_payment(self, request, queryset):
        """Recover escrows stuck in awaiting_funding after a real payment.

        Asks PayPal for each contract's stored order reference and advances the
        ones that are actually paid. Safe to re-run: confirmation is a
        conditional UPDATE that only fires once.
        """
        verified = 0
        for contract in queryset:
            if contract.status != 'awaiting_funding' or not contract.gateway_reference:
                self.message_user(
                    request,
                    f"{contract.code} is not awaiting funding; skipped.",
                    level=messages.WARNING,
                )
                continue
            try:
                result = get_payment_gateway().verify_transaction(
                    contract.gateway_reference
                )
            except Exception:
                result = None
            data = result.get('data', {}) if isinstance(result, dict) else {}
            if (
                isinstance(result, dict) and result.get('status')
                and isinstance(data, dict) and data.get('status') == 'success'
                and (not data.get('reference') or data.get('reference') == contract.gateway_reference)
                and data.get('amount') is not None
                and confirm_funding_payment(
                    contract, data.get('amount'), currency=data.get('currency'))
            ):
                verified += 1
            else:
                self.message_user(
                    request,
                    f"{contract.code}: no successful payment found "
                    f"(check PayPal order {contract.gateway_reference}).",
                    level=messages.WARNING,
                )
        self.message_user(
            request, f"Verified {verified} payment(s).", level=messages.INFO
        )

    def _cancel(self, request, queryset, verb):
        changed = 0
        for contract in queryset:
            if not contract.can_transition_to('cancelled'):
                self.message_user(
                    request,
                    f"{contract.code} cannot be cancelled from {contract.status}.",
                    level=messages.WARNING,
                )
                continue
            was_funded = contract.status not in EscrowContract.PRE_FUNDING_STATUSES
            contract.transition_to('cancelled')
            changed += 1
            if was_funded:
                refund = initiate_refund(contract)
                self.message_user(
                    request,
                    f"{contract.code} was funded: refund {refund.status} "
                    f"(record #{refund.id}). The escrow is not refunded "
                    "until the provider confirms.",
                    level=(messages.INFO if refund.status in ('initiated', 'success')
                           else messages.ERROR),
                )
        self.message_user(
            request, f"{verb} {changed} escrow(s).", level=messages.INFO
        )

    @admin.action(description="Resolve dispute: release funds to seller")
    def resolve_dispute_to_payout(self, request, queryset):
        for contract in queryset:
            if not contract.can_transition_to('pending_payout'):
                self.message_user(
                    request,
                    f"{contract.code} cannot be released from {contract.status}.",
                    level=messages.WARNING,
                )
                continue
            contract.transition_to('pending_payout')
            result = settle_pending_payout(contract)
            self.message_user(
                request,
                f"{contract.code}: "
                + ('paid' if result.success else f'NOT paid ({result.reason})'),
                level=messages.INFO if result.success else messages.ERROR,
            )

    @admin.action(description="Resolve dispute: cancel escrow")
    def resolve_dispute_cancelled(self, request, queryset):
        self._cancel(request, queryset, 'Resolved and cancelled')

    @admin.action(description="Cancel escrow")
    def cancel_contract(self, request, queryset):
        self._cancel(request, queryset, 'Cancelled')

    @admin.action(description="Extend inspection window by 3 days")
    def extend_inspection_window(self, request, queryset):
        for contract in queryset:
            if not contract.inspection_ends:
                self.message_user(
                    request,
                    f"{contract.code} has no inspection deadline to extend.",
                    level=messages.WARNING,
                )
                continue
            contract.inspection_ends += timezone.timedelta(days=3)
            contract.save(update_fields=['inspection_ends'])
        self.message_user(request, f"Extended {queryset.count()} escrow(s).",
                          level=messages.INFO)


@admin.register(AuthOTP)
class AuthOTPAdmin(admin.ModelAdmin):
    list_display = ('email', 'intent', 'attempts_left', 'is_used', 'expires_at', 'created_at')
    list_filter = ('intent', 'is_used')
    search_fields = ('email',)
    readonly_fields = ('code_hash', 'created_at')
    actions = ('purge_stale',)

    @admin.action(description="Delete expired or already-used codes")
    def purge_stale(self, request, queryset):
        stale = queryset.filter(Q(is_used=True) | Q(expires_at__lt=timezone.now()))
        count, _ = stale.delete()
        self.message_user(request, f"Deleted {count} stale code(s).", level=messages.INFO)


@admin.register(SettlementVault)
class SettlementVaultAdmin(admin.ModelAdmin):
    list_display = ('contract', 'paypal_email', 'masked_account')
    search_fields = ('contract__code', 'paypal_email')

    @admin.display(description='Account number')
    def masked_account(self, obj):
        return obj.masked_account_number


@admin.register(Identity)
class IdentityAdmin(admin.ModelAdmin):
    list_display = ('email', 'first_verified_at', 'last_seen_at', 'created_at')
    search_fields = ('email',)
    readonly_fields = ('created_at',)


@admin.register(VerifiedContact)
class VerifiedContactAdmin(admin.ModelAdmin):
    list_display = ('value', 'channel', 'identity', 'verified_at', 'created_at')
    list_filter = ('channel',)
    search_fields = ('value', 'identity__email')


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'provider_reference', 'escrow', 'amount_kobo', 'currency',
        'status', 'created_at',
    )
    list_filter = ('status', 'provider')
    search_fields = ('provider_reference', 'escrow__code')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(PayoutRecord)
class PayoutRecordAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'escrow', 'amount', 'currency', 'status',
        'transfer_reference', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('escrow__code', 'transfer_reference', 'provider_transfer_code')
    readonly_fields = ('created_at',)


@admin.register(RefundRecord)
class RefundRecordAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'escrow', 'amount_kobo', 'currency', 'status', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('escrow__code', 'provider_reference')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        'provider', 'event', 'event_id', 'signature_valid',
        'processed', 'received_at',
    )
    list_filter = ('provider', 'signature_valid', 'processed')
    search_fields = ('event', 'event_id')
    readonly_fields = ('received_at',)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('action', 'actor', 'escrow', 'created_at')
    list_filter = ('action',)
    search_fields = ('action', 'actor', 'escrow__code')
    readonly_fields = ('created_at',)

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = 'Locktrans.AI Operations'
admin.site.site_title = 'Locktrans.AI'
admin.site.index_title = 'Escrow operations'
