"""End-to-end lifecycle via AI: release tool, push notifications, updates.

Covers the buyer getting funded and released without leaving chat, the
seller hearing about moves on WhatsApp, and the agent surfacing backend
updates on the next turn.
"""
from decimal import Decimal
from unittest.mock import ANY, patch

from django.test import TestCase, override_settings

from agent.loop import get_or_create_conversation, run_turn
from agent.registry import CONSEQUENTIAL_TOOLS, execute
from escrow.lifecycle import notify_event, whatsapp_numbers_for
from escrow.models import AuditEvent, EscrowContract, SettlementVault


def make_contract(code='LC01', status='in_inspection', amount=Decimal('50000'),
                  fee=Decimal('1500')):
    return EscrowContract.objects.create(
        code=code, title='T', description='D', amount=amount,
        service_fee=fee, fee_payer='buyer', creator_email='buyer@x.com',
        creator_role='buyer', counterparty_email='seller@x.com',
        status=status, gateway_reference=f'REF_{code}')


def add_vault(contract):
    return SettlementVault.objects.create(
        contract=contract, paypal_email='seller@example.com',
        bank_name='PayPal', account_name='seller@example.com')


class ReleaseToolTest(TestCase):
    def test_release_is_consequential(self):
        self.assertIn('request_release_approval', CONSEQUENTIAL_TOOLS)

    def test_buyer_releases_successfully(self):
        contract = make_contract()
        add_vault(contract)
        with patch('escrow.services.get_payment_gateway') as mock_gw:
            mock_gw.return_value.execute_seller_payout.return_value = {
                'status': True, 'data': {'reference': 'T1'}}
            result = execute('request_release_approval',
                             {'escrow_reference': contract.code},
                             actor_email='buyer@x.com')
        self.assertTrue(result['ok'])
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'completed')

    def test_seller_cannot_release(self):
        contract = make_contract()
        add_vault(contract)
        result = execute('request_release_approval',
                         {'escrow_reference': contract.code},
                         actor_email='seller@x.com')
        self.assertFalse(result['ok'])

    def test_wrong_state_refused(self):
        contract = make_contract(status='awaiting_funding')
        result = execute('request_release_approval',
                         {'escrow_reference': contract.code},
                         actor_email='buyer@x.com')
        self.assertFalse(result['ok'])

    def test_unverified_refused(self):
        self.assertFalse(execute(
            'request_release_approval', {'escrow_reference': 'LC01'},
            actor_email=None)['ok'])

    def test_missing_vault_is_friendly(self):
        contract = make_contract()
        result = execute('request_release_approval',
                         {'escrow_reference': contract.code},
                         actor_email='buyer@x.com')
        self.assertFalse(result['ok'])
        self.assertIn('paypal', result['error'].lower())

    def test_double_release_pays_once(self):
        from escrow.models import PayoutRecord
        contract = make_contract()
        add_vault(contract)
        with patch('escrow.services.get_payment_gateway') as mock_gw:
            mock_gw.return_value.execute_seller_payout.return_value = {
                'status': True, 'data': {'reference': 'T1'}}
            first = execute('request_release_approval',
                            {'escrow_reference': contract.code},
                            actor_email='buyer@x.com')
            second = execute('request_release_approval',
                             {'escrow_reference': contract.code},
                             actor_email='buyer@x.com')
        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertEqual(
            PayoutRecord.objects.filter(escrow=contract, status='success').count(), 1)

    def test_release_flows_through_confirmation_gate(self):
        import json
        contract = make_contract(code='LC99')
        add_vault(contract)
        convo = get_or_create_conversation('web', 'release-gate')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        call = {'id': 'r1', 'name': 'request_release_approval',
                'arguments': {'escrow_reference': 'LC99'}}

        def script(rounds):
            calls = list(rounds)

            def fake_chat(messages, tools=None, **kwargs):
                if calls:
                    return calls.pop(0)
                return {'text': 'Done.', 'tool_calls': [], 'usage': {}}
            return patch('agent.loop.chat', side_effect=fake_chat)

        with script([{'text': '', 'tool_calls': [call], 'usage': {}}]):
            run_turn(convo, 'Release it.')
        outcome = json.loads(
            convo.messages.filter(role='tool').order_by('-id').first().content)
        self.assertIn('CONFIRMATION_REQUIRED', outcome['error'])
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')
        with patch('escrow.services.get_payment_gateway') as mock_gw:
            mock_gw.return_value.execute_seller_payout.return_value = {
                'status': True, 'data': {'reference': 'T1'}}
            with script([{'text': '', 'tool_calls': [call], 'usage': {}}]):
                run_turn(convo, 'Yes, release.')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'completed')


@override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='123')
class NotifyTest(TestCase):
    def setUp(self):
        from whatsapp.models import WhatsAppLink
        WhatsAppLink.objects.create(wa_id='234B', verified_email='buyer@x.com')
        WhatsAppLink.objects.create(wa_id='234S', verified_email='seller@x.com')

    def test_join_notifies_buyer_only(self):
        contract = make_contract(status='awaiting_funding')
        with patch('whatsapp.adapter.send_text_message',
                   return_value=True) as mock_send:
            self.assertEqual(notify_event('counterparty_joined', contract), 1)
        mock_send.assert_called_once_with('234B', ANY)

    def test_funding_notifies_seller_only(self):
        contract = make_contract(status='awaiting_dispatch')
        with patch('whatsapp.adapter.send_text_message',
                   return_value=True) as mock_send:
            self.assertEqual(notify_event('funding_confirmed', contract), 1)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], '234S')

    def test_unlinked_email_skipped_silently(self):
        # funding_confirmed notifies the seller (counterparty here).
        contract = make_contract()
        contract.counterparty_email = 'ghost@x.com'
        contract.save()
        with patch('whatsapp.adapter.send_text_message',
                   return_value=True) as mock_send:
            notify_event('funding_confirmed', contract)
        mock_send.assert_not_called()

    def test_send_failure_never_raises(self):
        contract = make_contract()
        with patch('whatsapp.adapter.send_text_message',
                   side_effect=Exception('down')):
            notify_event('disputed', contract)

    def test_unknown_event_sends_nothing(self):
        contract = make_contract()
        with patch('whatsapp.adapter.send_text_message') as mock_send:
            self.assertEqual(notify_event('nope', contract), 0)
        mock_send.assert_not_called()

    def test_lookup_empty_without_token(self):
        with override_settings(WHATSAPP_ACCESS_TOKEN=''):
            self.assertEqual(whatsapp_numbers_for('buyer@x.com'), [])



class RecentUpdatesTest(TestCase):
    def test_agent_lists_escrows_when_asked(self):
        contract = make_contract(code='UP01', status='awaiting_dispatch')
        convo = get_or_create_conversation('web', 'updates-1')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        call = {'id': 'u1', 'name': 'list_my_escrows', 'arguments': {}}
        with patch('agent.loop.chat', return_value={
                'text': 'X', 'tool_calls': [call], 'usage': {}}):
            run_turn(convo, 'anything new with my escrows?')
        import json
        tool_row = convo.messages.filter(role='tool').order_by('-id').first()
        outcome = json.loads(tool_row.content)
        self.assertTrue(outcome['ok'])
        self.assertIn('UP01', json.dumps(outcome['data']))

    def test_no_proactive_brief_in_context(self):
        contract = make_contract(code='UP02', status='awaiting_dispatch')
        AuditEvent.objects.create(action='funding.confirmed', actor='system',
                                  escrow=contract)
        convo = get_or_create_conversation('web', 'updates-2')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        seen = []

        def fake_chat(messages, tools=None, **kwargs):
            seen.extend(messages)
            return {'text': 'Nothing.', 'tool_calls': [], 'usage': {}}

        with patch('agent.loop.chat', side_effect=fake_chat):
            run_turn(convo, 'hi')
        systems = [m['content'] for m in seen if m['role'] == 'system']
        self.assertFalse(any('Since your last visit' in s for s in systems))

class VerificationFlagsTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_otp = patcher.start()
        self.addCleanup(patcher.stop)

    def _join(self, code, email, intent, post_url):
        from escrow.services import issue_otp
        issue_otp(email, intent)
        otp = self.mock_otp.call_args[0][1]
        return self.client.post(post_url, {'otp': otp})

    def test_join_sets_counterparty_flag(self):
        contract = make_contract(code='FL01', status='awaiting_counterparty')
        response = self._join('FL01', 'seller@x.com', 'join', f'/escrow/FL01/verify/')
        self.assertEqual(response.status_code, 302)
        contract.refresh_from_db()
        self.assertTrue(contract.seller_confirmed)
        result = execute('get_authorized_escrow_status',
                         {'escrow_reference': 'FL01'}, actor_email='buyer@x.com')
        self.assertTrue(result['ok'])
        self.assertTrue(result['data']['counterparty_verified'])

    def test_creator_verify_sets_creator_flag(self):
        contract = make_contract(code='FL02', status='pending_verification')
        response = self._join('FL02', 'buyer@x.com', 'create', '/verify/FL02/')
        self.assertEqual(response.status_code, 302)
        contract.refresh_from_db()
        self.assertTrue(contract.buyer_confirmed)
