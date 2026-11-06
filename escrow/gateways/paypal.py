"""PayPal gateway: Orders v2 + Payouts, sandbox/live via env.

Env:
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET,
  PAYPAL_MODE=sandbox|live (default sandbox),
  PAYPAL_BASE_URL override (default derived from mode),
  PAYPAL_WEBHOOK_ID (for verify-webhook-signature),
  PAYPAL_MERCHANT_EMAIL (optional, payee for orders),
  GATEWAY_HTTP_TIMEOUT (seconds).
"""
import base64
import logging

import requests
from django.conf import settings

from .base import PaymentGatewayBase

logger = logging.getLogger(__name__)


def _timeout():
    return int(getattr(settings, 'GATEWAY_HTTP_TIMEOUT', 15))


def _base_url():
    override = getattr(settings, 'PAYPAL_BASE_URL', '')
    if override:
        return override.rstrip('/')
    mode = str(getattr(settings, 'PAYPAL_MODE', 'sandbox')).lower()
    if mode == 'live':
        return 'https://api-m.paypal.com'
    return 'https://api-m.sandbox.paypal.com'


def _creds():
    return (
        getattr(settings, 'PAYPAL_CLIENT_ID', ''),
        getattr(settings, 'PAYPAL_CLIENT_SECRET', ''),
    )


def money_value(amount_decimal):
    from decimal import Decimal, ROUND_HALF_UP
    return str(Decimal(amount_decimal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


class PayPalGateway(PaymentGatewayBase):
    def _auth_header(self):
        client_id, secret = _creds()
        token = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _access_token(self):
        client_id, secret = _creds()
        if not client_id or not secret:
            raise RuntimeError("PayPal credentials are not configured.")
        resp = requests.post(
            f"{_base_url()}/v1/oauth2/token",
            headers={**self._auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=_timeout(),
        )
        data = resp.json()
        token = data.get('access_token')
        if not token:
            logger.error("PayPal OAuth failed: %r", data)
            raise RuntimeError("PayPal OAuth failed.")
        return token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

    def initialize_vault_payment(self, escrow_record, return_url):
        """Create a PayPal Order; buyer approves at auth_url, then we capture."""
        from decimal import Decimal
        total = Decimal(escrow_record.total_charge)
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": escrow_record.gateway_reference or escrow_record.code,
                "description": f"Locktrans escrow {escrow_record.code}",
                "amount": {
                    "currency_code": escrow_record.currency or 'USD',
                    "value": money_value(total),
                },
            }],
            "application_context": {
                "return_url": return_url,
                "cancel_url": return_url,
                "brand_name": "Locktrans",
                "user_action": "PAY_NOW",
            },
        }
        merchant = getattr(settings, 'PAYPAL_MERCHANT_EMAIL', '')
        if merchant:
            payload["purchase_units"][0]["payee"] = {"email_address": merchant}
        try:
            resp = requests.post(
                f"{_base_url()}/v2/checkout/orders",
                json=payload, headers=self._headers(), timeout=_timeout(),
            )
            data = resp.json()
            order_id = data.get('id')
            if not order_id:
                logger.error("PayPal order create rejected: %r", data)
                return {"status": False, "error": data}
            approve = None
            for link in data.get('links', []):
                if link.get('rel') == 'approve':
                    approve = link.get('href')
            if not approve:
                logger.error("PayPal order missing approve link: %r", data)
                return {"status": False, "error": "no approve link"}
            return {"status": True, "auth_url": approve, "order_id": order_id, "raw": data}
        except Exception as e:
            logger.exception("PayPal order create failed")
            return {"status": False, "error": str(e)}

    def capture_order(self, order_id):
        try:
            resp = requests.post(
                f"{_base_url()}/v2/checkout/orders/{order_id}/capture",
                json={}, headers=self._headers(), timeout=_timeout(),
            )
            return resp.json()
        except Exception as e:
            logger.exception("PayPal capture failed for %s", order_id)
            return {"status": False, "error": str(e)}

    def verify_transaction(self, gateway_reference):
        """Fetch order; normalize to {status, data:{status, reference, amount, currency}}.

        Works for order IDs (post-init) and falls back to a capture ID.
        """
        try:
            resp = requests.get(
                f"{_base_url()}/v2/checkout/orders/{gateway_reference}",
                headers=self._headers(), timeout=_timeout(),
            )
            data = resp.json()
            if data.get('id'):
                units = data.get('purchase_units') or [{}]
                payments = (units[0].get('payments') or {})
                captures = payments.get('captures') or []
                if captures:
                    cap = captures[0]
                    amt = (cap.get('amount') or {})
                    return {'status': True, 'data': {
                        'status': 'success' if cap.get('status') == 'COMPLETED' else cap.get('status', '').lower(),
                        'reference': gateway_reference,
                        'amount': int(float(amt.get('value', '0')) * 100),
                        'currency': amt.get('currency_code', 'USD'),
                        'capture_id': cap.get('id'),
                        'order_status': data.get('status'),
                    }}
                return {'status': True, 'data': {
                    'status': data.get('status', '').lower(),
                    'reference': gateway_reference,
                    'amount': None, 'currency': None,
                    'order_status': data.get('status'),
                }}
            # Fallback: treat as capture id
            resp2 = requests.get(
                f"{_base_url()}/v2/payments/captures/{gateway_reference}",
                headers=self._headers(), timeout=_timeout(),
            )
            cap = resp2.json()
            if cap.get('id'):
                amt = cap.get('amount') or {}
                return {'status': True, 'data': {
                    'status': 'success' if cap.get('status') == 'COMPLETED' else cap.get('status', '').lower(),
                    'reference': gateway_reference,
                    'amount': int(float(amt.get('value', '0')) * 100),
                    'currency': amt.get('currency_code', 'USD'),
                    'capture_id': cap.get('id'),
                }}
            return {'status': False, 'error': data}
        except Exception as e:
            logger.exception("PayPal verify failed for %s", gateway_reference)
            return {'status': False, 'error': str(e)}

    def verify_incoming_webhook(self, request_headers, request_body):
        """Validate via /v1/notifications/verify-webhook-signature, return event or False."""
        import json as _json
        get = lambda k: request_headers.get(k) or request_headers.get(k.lower())
        transmission_id = get('PAYPAL-TRANSMISSION-ID')
        transmission_time = get('PAYPAL-TRANSMISSION-TIME')
        cert_url = get('PAYPAL-CERT-URL')
        auth_algo = get('PAYPAL-AUTH-ALGO')
        transmission_sig = get('PAYPAL-TRANSMISSION-SIG')
        webhook_id = getattr(settings, 'PAYPAL_WEBHOOK_ID', '')
        if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig, webhook_id]):
            logger.warning("PayPal webhook rejected: missing headers or webhook id")
            return False
        try:
            event = _json.loads(request_body.decode('utf-8'))
        except Exception:
            logger.warning("PayPal webhook rejected: invalid JSON")
            return False
        try:
            resp = requests.post(
                f"{_base_url()}/v1/notifications/verify-webhook-signature",
                json={
                    "transmission_id": transmission_id,
                    "transmission_time": transmission_time,
                    "cert_url": cert_url,
                    "auth_algo": auth_algo,
                    "transmission_sig": transmission_sig,
                    "webhook_id": webhook_id,
                    "webhook_event": event,
                },
                headers=self._headers(), timeout=_timeout(),
            )
            result = resp.json()
            if result.get('verification_status') != 'SUCCESS':
                logger.warning("PayPal webhook signature invalid: %r", result)
                return False
            return event
        except Exception:
            logger.exception("PayPal webhook verify call failed")
            return False

    def execute_seller_payout(self, settlement_vault_record, total_disbursement):
        receiver = getattr(settlement_vault_record, 'paypal_email', '') or ''
        if not receiver:
            return {"status": False, "error": "seller PayPal email missing"}
        try:
            code = settlement_vault_record.contract.code
        except Exception:
            code = "unknown"
        from decimal import Decimal
        cents = int(Decimal(total_disbursement) * 100)
        payload = {
            "sender_batch_header": {
                "sender_batch_id": f"locktrans-{code}-{cents}",
                "email_subject": f"Locktrans escrow {code} payout",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": receiver,
                "amount": {
                    "currency": getattr(settlement_vault_record.contract, 'currency', 'USD') or 'USD',
                    "value": money_value(total_disbursement),
                },
                "note": f"Locktrans escrow {code} settlement",
            }],
        }
        try:
            resp = requests.post(
                f"{_base_url()}/v1/payments/payouts",
                json=payload, headers=self._headers(), timeout=_timeout(),
            )
            data = resp.json()
            batch = (data.get('batch_header') or {})
            if not batch.get('payout_batch_id'):
                logger.error("PayPal payout rejected: %r", data)
                return {"status": False, "error": data}
            return {"status": True, "data": {
                "reference": batch.get('payout_batch_id'),
                "batch_status": batch.get('batch_status'),
                "sender_batch_id": batch.get('sender_batch_id'),
                "raw": data,
            }, **data}
        except Exception as e:
            logger.exception("PayPal payout failed")
            return {"status": False, "error": str(e)}

    def refund_payment(self, gateway_reference, amount_kobo=None):
        """Refund a captured payment; resolves order -> capture id first."""
        capture_id = gateway_reference
        verified = self.verify_transaction(gateway_reference)
        if isinstance(verified, dict):
            data = verified.get('data') or {}
            if data.get('capture_id'):
                capture_id = data['capture_id']
        payload = {}
        if amount_kobo is not None:
            try:
                from decimal import Decimal
                payload = {"amount": {
                    "currency_code": "USD",
                    "value": money_value(Decimal(amount_kobo) / 100),
                }}
            except Exception:
                payload = {}
        try:
            resp = requests.post(
                f"{_base_url()}/v2/payments/captures/{capture_id}/refund",
                json=payload, headers=self._headers(), timeout=_timeout(),
            )
            return resp.json()
        except Exception as e:
            logger.exception("PayPal refund failed for %s", gateway_reference)
            return {"status": False, "error": str(e)}
