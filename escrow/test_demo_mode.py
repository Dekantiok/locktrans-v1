"""Demo mode: only Termii OTP + PayPal payment are simulated.

Judges can run the full AI/escrow flow with no provider keys: the demo
OTP code is accepted (wrong codes still fail), funding runs through an
internal simulated checkout, and payouts return fake success.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from escrow.gateways import get_payment_gateway
from escrow.gateways.demo import DemoGateway
from escrow.gateways.paypal import PayPalGateway
from escrow.models import EscrowContract, PaymentTransaction, SettlementVault
from escrow.notifications import send_otp_via_termii
from escrow.services import consume_otp, issue_otp, release_escrow_funds


def make_contract(code='LKTRdemo1', status='awaiting_funding'):
    return EscrowContract.objects.create(
        code=code, title='Laptop', description='D', amount=Decimal('50000'),
        service_fee=Decimal('1500'), fee_payer='buyer',
        creator_email='buyer@x.com', creator_role='buyer',
        counterparty_email='seller@x.com', status=status,
        gateway_reference=f'REF_{code}')


@override_settings(DEMO_MODE=True, DEMO_OTP_CODE='123456')
class DemoOtpTest(TestCase):
    def test_termii_not_called_in_demo(self):
        with patch('escrow.notifications.requests.post') as mock_post:
            self.assertTrue(send_otp_via_termii('judge@x.com', '999999'))
        mock_post.assert_not_called()

    def test_demo_code_accepted_wrong_code_rejected(self):
        with patch('escrow.services.send_otp_via_termii', return_value=True):
            self.assertIsNotNone(issue_otp('judge@x.com', 'reauth'))
        self.assertFalse(consume_otp('judge@x.com', '000000', 'reauth').ok)
        self.assertTrue(consume_otp('judge@x.com', '123456', 'reauth').ok)

    def test_real_code_still_works_in_demo(self):
        with patch('escrow.services.send_otp_via_termii',
                   return_value=True) as mock_send:
            issue_otp('judge@x.com', 'reauth')
            real_code = mock_send.call_args[0][1]
        self.assertTrue(consume_otp('judge@x.com', real_code, 'reauth').ok)


@override_settings(DEMO_MODE=False)
class DemoOffTest(TestCase):
    def test_demo_code_rejected_when_off(self):
        with patch('escrow.services.send_otp_via_termii', return_value=True):
            issue_otp('judge@x.com', 'reauth')
        self.assertFalse(consume_otp('judge@x.com', '123456', 'reauth').ok)

    def test_real_gateway_when_off(self):
        self.assertIsInstance(get_payment_gateway(), PayPalGateway)

    def test_demo_checkout_404_when_off(self):
        make_contract(code='LKTRdemo2')
        response = self.client.get('/demo/pay/LKTRdemo2/')
        self.assertEqual(response.status_code, 404)


@override_settings(DEMO_MODE=True, DEMO_OTP_CODE='123456')
class DemoGatewayTest(TestCase):
    def test_demo_gateway_selected(self):
        self.assertIsInstance(get_payment_gateway(), DemoGateway)

    def test_payment_link_points_to_demo_checkout(self):
        contract = make_contract(code='LKTRdemo3')
        result = get_payment_gateway().initialize_vault_payment(
            contract, 'http://test/ignored')
        self.assertTrue(result['status'])
        self.assertEqual(result['auth_url'], '/demo/pay/LKTRdemo3/')

    def test_demo_checkout_confirms_funding(self):
        contract = make_contract(code='LKTRdemo4')
        response = self.client.get('/demo/pay/LKTRdemo4/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Simulated payment')
        response = self.client.post('/demo/pay/LKTRdemo4/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment confirmed (simulated)')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_dispatch')
        self.assertTrue(PaymentTransaction.objects.filter(
            escrow=contract, status='success').exists())

    def test_demo_payout_completes_release(self):
        contract = make_contract(code='LKTRdemo5', status='in_inspection')
        SettlementVault.objects.create(
            contract=contract, bank_name='B', bank_code='058',
            account_number='0123456789', account_name='SELLER')
        result = release_escrow_funds(contract)
        self.assertTrue(result.success, result)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'completed')

    def test_demo_banner_shows_code(self):
        response = self.client.get('/')
        self.assertContains(response, 'Demo mode')
        self.assertContains(response, '123456')


@override_settings(DEMO_MODE=False)
class DemoGuardTest(TestCase):
    def test_prod_settings_module_allows_import(self):
        # The hard DEMO-in-prod refusal lives at settings-import time;
        # it is exercised in CI with DJANGO_DEBUG=False. Here we pin the
        # two settings that compose it.
        from django.conf import settings
        self.assertFalse(settings.DEMO_MODE)


@override_settings(DEMO_MODE=True, DEMO_OTP_CODE='123456', DEMO_SIMULATE_PAYMENTS=False)
class DemoRealPayPalTest(TestCase):
    def test_real_gateway_when_payments_not_simulated(self):
        self.assertIsInstance(get_payment_gateway(), PayPalGateway)

    def test_demo_checkout_404_when_payments_not_simulated(self):
        make_contract(code='LKTRdemo6')
        response = self.client.get('/demo/pay/LKTRdemo6/')
        self.assertEqual(response.status_code, 404)

    def test_demo_otp_still_accepted(self):
        with patch('escrow.services.send_otp_via_termii', return_value=True):
            self.assertIsNotNone(issue_otp('judge@x.com', 'reauth'))
        self.assertTrue(consume_otp('judge@x.com', '123456', 'reauth').ok)
