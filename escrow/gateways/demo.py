"""Demo payment gateway: simulates PayPal without keys or real money.

Only active when DEMO_MODE is on and DEMO_SIMULATE_PAYMENTS is on.
Funding points at an internal demo checkout page; payouts return a fake
successful transfer. Nothing here ever touches the PayPal API.
"""
import logging

from django.urls import reverse

from .base import PaymentGatewayBase

logger = logging.getLogger(__name__)


class DemoGateway(PaymentGatewayBase):
    def initialize_vault_payment(self, escrow_record, return_url):
        del return_url
        return {
            'status': True,
            'auth_url': reverse('demo_pay', kwargs={'code': escrow_record.code}),
        }

    def verify_incoming_webhook(self, request_headers, request_body):
        del request_headers, request_body
        return False

    def verify_transaction(self, gateway_reference):
        from escrow.models import EscrowContract
        contract = EscrowContract.objects.filter(
            gateway_reference=gateway_reference).first()
        if contract is None:
            return {'status': False, 'error': 'unknown reference'}
        return {'status': True, 'data': {
            'status': 'success',
            'reference': gateway_reference,
            'amount': contract.total_charge_kobo,
            'currency': contract.currency,
        }}

    def execute_seller_payout(self, settlement_vault_record, total_disbursement):
        code = settlement_vault_record.contract.code
        logger.warning("DEMO payout simulated for escrow %s", code)
        return {'status': True, 'data': {
            'reference': f'DEMO-TRF-{code}',
            'transfer_code': f'DEMO-{code}',
            'amount': int(total_disbursement * 100),
        }}

    def refund_payment(self, gateway_reference, amount_kobo=None):
        del gateway_reference, amount_kobo
        return {'status': True, 'demo': True}
