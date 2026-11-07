"""Full-circle E2E through the chat UI (HTTP), roles/fee-payer swapped.

Both parties use only /assistant/send/: create, verify, join, fund (via
PayPal webhook), ship, receive, bank details, release, completed.
The model is scripted deterministically; gateways/OTP are mocked.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase

from agent.models import AIConversation
from escrow.models import AuditEvent, EscrowContract, EscrowDraft, SettlementVault


def tool_call(name, arguments, cid='c1'):
    return {'text': '', 'tool_calls': [
        {'id': cid, 'name': name, 'arguments': arguments}], 'usage': {}}


def text_reply(text='OK'):
    return {'text': text, 'tool_calls': [], 'usage': {}}


class ChatE2ETest(TestCase):
    def setUp(self):
        self.queue = []

        def fake_chat(messages, tools=None, **kwargs):
            if self.queue:
                return self.queue.pop(0)
            return text_reply('Done.')

        self.chat_patcher = patch('agent.loop.chat', side_effect=fake_chat)
        self.chat_patcher.start()
        self.addCleanup(self.chat_patcher.stop)

        otp = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_otp = otp.start()
        self.addCleanup(otp.stop)

        pay_link = patch('agent.registry.get_payment_gateway')
        mock_gw = pay_link.start()
        self.addCleanup(pay_link.stop)
        mock_gw.return_value.initialize_vault_payment.return_value = {
            'status': True, 'auth_url': 'https://pay.test/checkout'}

        payout = patch('escrow.services.get_payment_gateway')
        mock_payout = payout.start()
        self.addCleanup(payout.stop)
        mock_payout.return_value.execute_seller_payout.return_value = {
            'status': True, 'data': {'reference': 'TRF_E2E', 'transfer_code': 'T1'}}

    # -- helpers ---------------------------------------------------------
    def send(self, client, message, script=()):
        self.queue.extend(script)
        response = client.post('/assistant/send/', {'message': message})
        self.assertEqual(response.status_code, 200)
        return response

    def last_otp(self):
        return self.mock_otp.call_args[0][1]

    def tool_outcome(self, client_convo, name):
        row = (client_convo.messages.filter(role='tool', tool_name=name)
               .order_by('-id').first())
        self.assertIsNotNone(row, f'no tool row for {name}')
        return json.loads(row.content)

    def convo_of(self, client):
        # The web conversation identity is the client's session key.
        key = client.session.session_key
        return AIConversation.objects.get(
            channel='web', channel_identity=f'session:{key}')

    def confirm_tool(self, client, name, arguments, cid):
        """Gate + confirm: first call proposes; a bare 'yes' then executes
        deterministically in the backend (no second model call scripted)."""
        self.send(client, 'go ahead', [tool_call(name, arguments, cid),
                                       text_reply('Please confirm.')])
        self.send(client, 'yes, I confirm')

    # -- one full circle ---------------------------------------------------
    def run_circle(self, tag, creator_role, fee_payer):
        amount = '250000'
        fee = Decimal('10.00')
        creator_email = f'creator-{tag}@x.com'
        counterparty_email = f'counter-{tag}@x.com'
        if creator_role == 'buyer':
            buyer_email, seller_email = creator_email, counterparty_email
        else:
            seller_email, buyer_email = creator_email, counterparty_email

        creator = Client()
        counterparty = Client()

        # 1. Creator describes the deal in chat.
        self.send(creator, 'create my escrow', [
            tool_call('create_escrow_draft', {
                'title': 'Laptop', 'description': 'Laptop deal',
                'amount': amount, 'fee_payer': fee_payer,
                'creator_email': creator_email, 'creator_role': creator_role,
                'counterparty_email': counterparty_email, 'inspection_days': 3,
            }, 'd1'), text_reply('Draft saved. Confirm?')])
        draft = EscrowDraft.objects.latest('id')

        # 2. Creator confirms; the complete draft was auto-proposed at the
        # end of the last turn, so a bare 'yes' executes it directly and
        # creation parks on the emailed code.
        self.send(creator, 'yes, I confirm')
        outcome = self.tool_outcome(self.convo_of(creator), 'request_escrow_creation')
        self.assertTrue(outcome['ok'], outcome)
        self.assertTrue(outcome['data'].get('verification_required'), outcome)

        # 3. Creator pastes the code; escrow exists, code is LKTR-prefixed.
        self.send(creator, self.last_otp())
        contract = EscrowContract.objects.get(creator_email=creator_email)
        self.assertTrue(contract.code.startswith('LKTR'), contract.code)
        self.assertEqual(contract.status, 'awaiting_counterparty')
        self.assertEqual(contract.fee_payer, fee_payer)

        # 4. Counterparty joins from a fresh chat: code -> email -> OTP.
        self.send(counterparty, f'I want to join {contract.code}', [
            text_reply("What's your email?")])
        self.send(counterparty, f'my email is {counterparty_email}', [
            tool_call('request_verification_code', {
                'email': counterparty_email, 'escrow_reference': contract.code,
            }, 'v1'), text_reply('Code sent, paste it here.')])
        response = self.send(counterparty, self.last_otp())
        self.assertContains(response, 'joined')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')
        if counterparty_email == buyer_email:
            self.assertTrue(contract.buyer_confirmed)
        else:
            self.assertTrue(contract.seller_confirmed)
        self.assertTrue(AuditEvent.objects.filter(
            escrow=contract, action='counterparty.joined').exists())

        buyer = creator if buyer_email == creator_email else counterparty
        seller = creator if seller_email == creator_email else counterparty

        # 5. Buyer funds: chat fetches the PayPal link after confirmation.
        self.confirm_tool(buyer, 'request_payment_link',
                          {'escrow_reference': contract.code}, 'pay')
        outcome = self.tool_outcome(self.convo_of(buyer), 'request_payment_link')
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['data']['payment_url'], 'https://pay.test/checkout')

        # 6. PayPal webhook verifies the payment server-side.
        expected_total = Decimal(amount) + (fee if fee_payer == 'buyer' else Decimal('0'))
        self.assertEqual(contract.total_charge, expected_total)
        payload = {'event_type': 'PAYMENT.CAPTURE.COMPLETED', 'id': f'evt-e2e-{tag}',
                   'resource': {
                       'id': f'cap-e2e-{tag}',
                       'amount': {'value': f"{expected_total:.2f}", 'currency_code': 'USD'},
                       'supplementary_data': {
                           'related_ids': {'order_id': contract.gateway_reference},
                       },
                   }}
        with patch('escrow.views.get_payment_gateway') as mock_wh:
            mock_wh.return_value.verify_incoming_webhook.return_value = payload
            response = self.client.post(
                '/webhooks/paypal/', data=json.dumps(payload),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_dispatch')
        self.assertTrue(AuditEvent.objects.filter(
            escrow=contract, action='funding.confirmed').exists())

        # 7. Seller ships via chat.
        self.confirm_tool(seller, 'request_delivery_confirmation',
                          {'escrow_reference': contract.code, 'stage': 'dispatch'}, 'ship')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_transit')

        # 8. Buyer confirms receipt; inspection deadline is set.
        self.confirm_tool(buyer, 'request_delivery_confirmation',
                          {'escrow_reference': contract.code, 'stage': 'receipt'}, 'recv')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')
        self.assertIsNotNone(contract.inspection_ends)

        # 9. Buyer tries to release before bank details: blocked, seller told.
        self.confirm_tool(buyer, 'request_release_approval',
                          {'escrow_reference': contract.code}, 'rel1')
        outcome = self.tool_outcome(self.convo_of(buyer), 'request_release_approval')
        self.assertFalse(outcome['ok'])
        self.assertIn('paypal', outcome['error'].lower())
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')

        # 10. Seller enters the PayPal payout email in chat; backend saves it.
        self.confirm_tool(seller, 'request_seller_bank_details', {
            'escrow_reference': contract.code,
            'paypal_email': 'seller@example.com'}, 'bank')
        outcome = self.tool_outcome(
            self.convo_of(seller), 'request_seller_bank_details')
        self.assertTrue(outcome['ok'], outcome)
        vault = SettlementVault.objects.get(contract=contract)
        self.assertEqual(vault.paypal_email, 'seller@example.com')
        self.assertEqual(vault.bank_name, 'PayPal')

        # 11. Buyer releases; seller is paid; escrow completes.
        self.confirm_tool(buyer, 'request_release_approval',
                          {'escrow_reference': contract.code}, 'rel2')
        outcome = self.tool_outcome(self.convo_of(buyer), 'request_release_approval')
        self.assertTrue(outcome['ok'], outcome)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'completed')
        self.assertTrue(AuditEvent.objects.filter(
            escrow=contract, action='payout.completed').exists())
        expected_seller = Decimal(amount) - (fee if fee_payer == 'seller' else Decimal('0'))
        self.assertEqual(contract.seller_receives, expected_seller)
        return contract

    # -- matrix ------------------------------------------------------------
    def test_creator_buyer_pays_fee(self):
        contract = self.run_circle('a1', creator_role='buyer', fee_payer='buyer')
        self.assertEqual(contract.total_charge, Decimal('250010'))
        self.assertEqual(contract.seller_receives, Decimal('250000'))

    def test_creator_seller_pays_fee(self):
        contract = self.run_circle('b2', creator_role='seller', fee_payer='seller')
        self.assertEqual(contract.total_charge, Decimal('250000'))
        self.assertEqual(contract.seller_receives, Decimal('249990'))

    def test_creator_seller_buyer_pays_fee(self):
        contract = self.run_circle('c3', creator_role='seller', fee_payer='buyer')
        self.assertEqual(contract.total_charge, Decimal('250010'))
        self.assertEqual(contract.seller_receives, Decimal('250000'))
