"""Tests for the Baseten inference client (all network calls mocked)."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from agent.client import BasetenAPIError, BasetenConfigError, chat


def tool_call(call_id='call_1', name='get_fee_quote', arguments='{"amount": "50000"}'):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def completion(text='Hello', calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=calls)
        )],
        usage=usage,
    )


class BasetenClientTest(TestCase):
    @override_settings(BASETEN_API_KEY='')
    def test_missing_key_is_a_clear_config_error(self):
        with self.assertRaises(BasetenConfigError):
            chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_plain_reply_is_returned_as_text(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='Hello there')
            )
            result = chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result['text'], 'Hello there')
        self.assertEqual(result['tool_calls'], [])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_tool_calls_are_parsed_to_plain_data(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='', calls=[tool_call()])
            )
            result = chat(
                [{'role': 'user', 'content': 'fee?'}],
                tools=[{'type': 'function', 'function': {'name': 'get_fee_quote'}}],
            )
        self.assertEqual(len(result['tool_calls']), 1)
        call = result['tool_calls'][0]
        self.assertEqual(call['name'], 'get_fee_quote')
        self.assertEqual(call['arguments'], {'amount': '50000'})

    @override_settings(BASETEN_API_KEY='test-key')
    def test_unparseable_arguments_are_an_api_error(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(calls=[tool_call(arguments='{not json')])
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_timeout_maps_to_api_error(self):
        from openai import APITimeoutError
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.side_effect = (
                APITimeoutError(request=None)
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_empty_choices_are_an_api_error(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                SimpleNamespace(choices=[])
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_usage_is_logged_not_returned_raw(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5
                ))
            )
            result = chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result['usage']['prompt_tokens'], 10)
        self.assertEqual(result['usage']['completion_tokens'], 5)

    @override_settings(BASETEN_API_KEY='test-key')
    def test_reasoning_budget_is_sent(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='Hi')
            )
            chat([{'role': 'user', 'content': 'hi'}])
        _, kwargs = mock_build.return_value.chat.completions.create.call_args
        self.assertGreaterEqual(kwargs.get('max_tokens'), 4096)

    @override_settings(BASETEN_API_KEY='test-key')
    def test_affinity_key_becomes_routing_header(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='Hi')
            )
            chat([{'role': 'user', 'content': 'hi'}],
                 affinity_key='convo-123')
        _, kwargs = mock_build.return_value.chat.completions.create.call_args
        self.assertEqual(kwargs.get('extra_headers'),
                         {'Modal-Routing-Affinity-Key': 'convo-123'})

    @override_settings(BASETEN_API_KEY='test-key')
    def test_no_affinity_header_by_default(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='Hi')
            )
            chat([{'role': 'user', 'content': 'hi'}])
        _, kwargs = mock_build.return_value.chat.completions.create.call_args
        self.assertNotIn('extra_headers', kwargs)


class RegistryTest(TestCase):
    def setUp(self):
        from escrow.models import EscrowContract
        self.contract = EscrowContract.objects.create(
            code='AGENT001',
            title='Agent Item',
            description='Terms agreed in chat',
            amount=__import__('decimal').Decimal('50000.00'),
            service_fee=__import__('decimal').Decimal('1500.00'),
            fee_payer='buyer',
            creator_email='buyer@example.com',
            creator_role='buyer',
            counterparty_email='seller@example.com',
            status='awaiting_funding',
            gateway_reference='REF_AGENT001',
        )

    def test_fee_quote_math(self):
        from agent.registry import execute
        result = execute('get_fee_quote', {'amount': '50000'},
                         actor_email='buyer@example.com')
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['service_fee'], '10.00')
        self.assertEqual(result['data']['currency'], 'USD')
        self.assertEqual(result['data']['buyer_pays'], '50010.00')

    def test_fee_quote_rejects_out_of_range(self):
        from agent.registry import execute
        result = execute('get_fee_quote', {'amount': '1'},
                         actor_email='buyer@example.com')
        self.assertFalse(result['ok'])

    def test_bank_tool_one_tap_uses_verified_email(self):
        from agent.registry import execute
        self.contract.transition_to('awaiting_dispatch')
        result = execute('request_seller_bank_details',
                         {'escrow_reference': 'AGENT001',
                          'use_verified_email': True},
                         actor_email='seller@example.com')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['paypal_email'], 'seller@example.com')
        from escrow.models import SettlementVault
        self.assertEqual(
            SettlementVault.objects.get().paypal_email, 'seller@example.com')

    def test_bank_tool_needs_email_or_verified_flag(self):
        from agent.registry import execute
        self.contract.transition_to('awaiting_dispatch')
        result = execute('request_seller_bank_details',
                         {'escrow_reference': 'AGENT001'},
                         actor_email='seller@example.com')
        self.assertFalse(result['ok'])

    def test_unknown_tool_is_an_error_not_an_exception(self):
        from agent.registry import execute
        result = execute('delete_everything', {}, actor_email='a@b.com')
        self.assertFalse(result['ok'])

    def test_status_hidden_from_non_participant(self):
        from agent.registry import execute
        result = execute('get_authorized_escrow_status',
                         {'escrow_reference': 'AGENT001'},
                         actor_email='stranger@example.com')
        self.assertFalse(result['ok'])

    def test_status_visible_to_participant(self):
        from agent.registry import execute
        result = execute('get_authorized_escrow_status',
                         {'escrow_reference': 'AGENT001'},
                         actor_email='seller@example.com')
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['you_are'], 'seller')

    def test_draft_isolation_between_owners(self):
        from agent.registry import execute
        created = execute('create_escrow_draft', {'title': 'Mine'},
                          actor_email='buyer@example.com')
        draft_id = created['data']['draft_id']
        blocked = execute('update_escrow_draft', {'draft_id': draft_id},
                          actor_email='seller@example.com')
        self.assertFalse(blocked['ok'])
        allowed = execute('get_escrow_draft', {'draft_id': draft_id},
                          actor_email='buyer@example.com')
        self.assertTrue(allowed['ok'])

    def test_consequential_tool_needs_a_verified_actor(self):
        from agent.registry import execute
        result = execute('request_delivery_confirmation',
                         {'escrow_reference': 'AGENT001', 'stage': 'receipt'},
                         actor_email=None)
        self.assertFalse(result['ok'])

    def test_wrong_role_cannot_confirm(self):
        from agent.registry import execute
        result = execute('request_delivery_confirmation',
                         {'escrow_reference': 'AGENT001', 'stage': 'dispatch'},
                         actor_email='buyer@example.com')
        self.assertFalse(result['ok'])


class LoopTest(TestCase):
    def make_conversation(self, owner=None):
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-1')
        convo.owner_email = owner
        convo.save(update_fields=['owner_email'])
        return convo

    def script(self, rounds):
        """Script agent.loop.chat to return `rounds` then a final reply."""
        calls = list(rounds)

        def fake_chat(messages, tools=None, **kwargs):
            if calls:
                return calls.pop(0)
            return {'text': 'Done.', 'tool_calls': [], 'usage': {}}

        return patch('agent.loop.chat', side_effect=fake_chat)

    def test_non_consequential_tool_executes_freely(self):
        from agent.loop import run_turn
        convo = self.make_conversation(owner='buyer@example.com')
        rounds = [{
            'text': '',
            'tool_calls': [{'id': 'c1', 'name': 'get_fee_quote',
                            'arguments': {'amount': '50000'}}],
            'usage': {},
        }]
        with self.script(rounds):
            reply = run_turn(convo, 'What is the fee on 50000?')
        self.assertTrue(reply)
        tool_rows = convo.messages.filter(role='tool')
        self.assertEqual(tool_rows.count(), 1)
        import json
        self.assertTrue(json.loads(tool_rows.first().content)['ok'])

    def test_consequential_tool_waits_for_confirmation(self):
        import json

        from agent.loop import confirmed, run_turn
        from escrow.models import EscrowContract
        EscrowContract.objects.create(
            code='AGENT002',
            title='Disputed item',
            description='Terms',
            amount=__import__('decimal').Decimal('20000.00'),
            service_fee=__import__('decimal').Decimal('1500.00'),
            fee_payer='buyer',
            creator_email='buyer@example.com',
            creator_role='buyer',
            counterparty_email='seller@example.com',
            status='in_transit',
            gateway_reference='REF_AGENT002',
        )
        convo = self.make_conversation(owner='buyer@example.com')
        call = {'id': 'c1', 'name': 'request_dispute_submission',
                'arguments': {'escrow_reference': 'AGENT002',
                              'reason': 'Never arrived'}}

        with self.script([{'text': '', 'tool_calls': [call], 'usage': {}}]):
            first_reply = run_turn(convo, 'Raise a dispute, it never arrived.')
        self.assertTrue(first_reply)
        pending = convo.messages.filter(role='tool').order_by('-id').first()
        outcome = json.loads(pending.content)
        self.assertFalse(outcome['ok'])
        self.assertIn('CONFIRMATION_REQUIRED', outcome['error'])
        self.assertEqual(
            EscrowContract.objects.get(code='AGENT002').status, 'in_transit')

        convo.refresh_from_db()
        with self.script([{'text': '', 'tool_calls': [call], 'usage': {}}]):
            run_turn(convo, 'Yes, I confirm, raise it.')
        self.assertEqual(
            EscrowContract.objects.get(code='AGENT002').status, 'disputed')
        convo.refresh_from_db()
        self.assertFalse(confirmed(convo, call['name'], call['arguments']))

    def test_creation_confirmation_carries_fee_breakdown(self):
        import json
        from decimal import Decimal

        from agent.loop import run_turn
        from escrow.models import EscrowDraft
        draft = EscrowDraft.objects.create(
            owner_email='buyer@example.com', owner_session='',
            title='Laptop', description='Used laptop',
            amount=Decimal('500.00'), currency='USD', fee_payer='buyer',
            creator_role='buyer', counterparty_email='seller@example.com',
            inspection_days=3, status='draft',
        )
        convo = self.make_conversation(owner='buyer@example.com')
        call = {'id': 'c1', 'name': 'request_escrow_creation',
                'arguments': {'draft_id': draft.id}}
        with self.script([{'text': '', 'tool_calls': [call], 'usage': {}}]):
            run_turn(convo, 'Create it, I confirm the terms.')
        outcome = json.loads(
            convo.messages.filter(role='tool').order_by('-id').first().content)
        self.assertFalse(outcome['ok'])
        self.assertIn('CONFIRMATION_REQUIRED', outcome['error'])
        # $500 -> $2.50 fee, buyer pays $502.50, seller receives $500.00
        self.assertIn('$2.50', outcome['error'])
        self.assertIn('$502.50', outcome['error'])
        self.assertIn('$500.00', outcome['error'])

    def test_creation_terms_empty_without_draft(self):
        from agent.loop import creation_terms_text
        self.assertEqual(creation_terms_text({}), '')
        self.assertEqual(creation_terms_text({'draft_id': 999999}), '')

    def test_draft_id_string_matches_int_proposal(self):
        from agent.loop import confirmed, propose_action
        convo = self.make_conversation(owner='buyer@example.com')
        propose_action(convo, 'request_escrow_creation', {'draft_id': 7})
        convo.refresh_from_db()
        # The model sometimes re-emits the id as a string on its retry -
        # that must still count as the same confirmed proposal.
        self.assertTrue(confirmed(
            convo, 'request_escrow_creation', {'draft_id': '7'}))

    def test_changed_terms_need_fresh_confirmation(self):
        from agent.loop import confirmed
        convo = self.make_conversation(owner='buyer@example.com')
        from agent.loop import propose_action
        propose_action(convo, 'request_dispute_submission',
                       {'escrow_reference': 'AGENT002', 'reason': 'Late'})
        convo.refresh_from_db()
        self.assertFalse(confirmed(
            convo, 'request_dispute_submission',
            {'escrow_reference': 'AGENT002', 'reason': 'Different reason'}))

    def test_otp_is_withheld_from_model_context(self):
        from agent.loop import run_turn
        convo = self.make_conversation()
        seen = []

        def fake_chat(messages, tools=None, **kwargs):
            seen.extend(messages)
            return {'text': 'Please verify in the app.', 'tool_calls': [],
                    'usage': {}}

        with patch('agent.loop.chat', side_effect=fake_chat):
            run_turn(convo, '123456')
        user_texts = [m['content'] for m in seen if m['role'] == 'user']
        self.assertTrue(user_texts)
        for text in user_texts:
            self.assertNotIn('123456', text)

    def test_model_outage_degrades_to_manual_path(self):
        from agent.client import BasetenAPIError
        from agent.loop import DEGRADED_REPLY, run_turn
        convo = self.make_conversation()
        with patch('agent.loop.chat', side_effect=BasetenAPIError('down')):
            self.assertEqual(run_turn(convo, 'hello'), DEGRADED_REPLY)


class ChatViewTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('escrow.services.send_otp_via_termii',
                                  return_value=True)
        self.mock_send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)

    def test_chat_page_creates_a_session_conversation(self):
        from agent.models import AIConversation
        response = self.client.get('/assistant/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AIConversation.objects.filter(channel='web').count(), 1)

    def test_send_runs_a_turn_and_renders_the_reply(self):
        from agent.models import AIConversation, ConversationMessage

        def fake_turn(conversation, text, base_url='', session_key='', **kwargs):
            ConversationMessage.objects.create(
                conversation_id=conversation.id,
                role='user', content=text)
            ConversationMessage.objects.create(
                conversation_id=conversation.id,
                role='assistant', content='Fee is $10.00.')
            return 'Fee is $10.00.'

        with patch('agent.views.run_turn', side_effect=fake_turn) as mock_turn:
            response = self.client.post('/assistant/send/',
                                        {'message': 'fee on 50000?'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fee is')
        convo = AIConversation.objects.get(channel='web')
        self.assertTrue(mock_turn.called)
        self.assertEqual(convo.messages.filter(role='user').count(), 1)

    def test_bare_code_goes_to_verification_not_the_model(self):
        from unittest.mock import patch as mock_patch

        from agent.models import AIConversation
        self.client.post('/assistant/verify/', {'email': 'chat@example.com'})
        session = self.client.session
        self.assertEqual(session.get('chat_pending_email'), 'chat@example.com')
        from escrow.models import AuthOTP
        record = AuthOTP.objects.filter(
            email='chat@example.com', intent='reauth', is_used=False).first()
        self.assertIsNotNone(record)

        with mock_patch('agent.views.run_turn') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': '000000'})
            self.assertEqual(response.status_code, 200)
            mock_turn.assert_not_called()
        self.assertContains(response, 'not correct')
        convo = AIConversation.objects.get(channel='web')
        self.assertIsNone(convo.owner_email)

    def test_correct_code_binds_the_conversation_owner(self):
        from agent.models import AIConversation
        self.client.post('/assistant/verify/', {'email': 'chat@example.com'})
        # Read the plaintext the same way the user does: from the sent payload.
        code = self.mock_send.call_args[0][1]
        with patch('agent.views.run_turn') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': code})
            mock_turn.assert_not_called()
        convo = AIConversation.objects.get(channel='web')
        self.assertEqual(convo.owner_email, 'chat@example.com')
        self.assertContains(response, 'Verified')

    def test_dismiss_clears_a_pending_proposal(self):
        from agent.loop import get_or_create_conversation, propose_action
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        propose_action(convo, 'request_dispute_submission', {'escrow_reference': 'X'})
        response = self.client.post('/assistant/dismiss/')
        self.assertEqual(response.status_code, 200)
        convo.refresh_from_db()
        self.assertEqual(convo.pending_action, {})

    def test_typed_discard_drops_proposal_without_model(self):
        from agent.loop import get_or_create_conversation, propose_action
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        propose_action(convo, 'request_dispute_submission', {'escrow_reference': 'X'})
        with patch('agent.views.run_turn') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': 'discard it'})
            mock_turn.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Discarded')
        convo.refresh_from_db()
        self.assertEqual(convo.pending_action, {})

    def test_discard_without_pending_goes_to_model(self):
        self.client.get('/assistant/')
        with patch('agent.views.run_turn', return_value='ok') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': 'discard it'})
            mock_turn.assert_called_once()
        self.assertEqual(response.status_code, 200)

    def test_stale_proposal_hidden_and_cleared_on_load(self):
        import datetime as _dt
        from django.utils import timezone
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.pending_action = {
            'tool': 'request_escrow_creation',
            'arguments': {'draft_id': 1},
            'proposed_at': (timezone.now() - _dt.timedelta(hours=2)).isoformat(),
        }
        convo.save(update_fields=['pending_action'])
        response = self.client.get('/assistant/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Your call:')
        convo.refresh_from_db()
        self.assertEqual(convo.pending_action, {})

    def test_new_chat_wipes_thread_and_proposal(self):
        from agent.loop import get_or_create_conversation, propose_action
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(role='user', content='hello')
        propose_action(convo, 'request_dispute_submission', {'escrow_reference': 'X'})
        response = self.client.post('/assistant/new/')
        self.assertEqual(response.status_code, 200)
        convo.refresh_from_db()
        self.assertEqual(convo.messages.count(), 0)
        self.assertEqual(convo.pending_action, {})
        self.assertContains(response, 'What are we escrowing today?')

    def test_page_load_wipes_idle_history(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(role='user', content='old hello')
        convo.messages.create(role='assistant', content='old reply')
        response = self.client.get('/assistant/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'old hello')
        self.assertContains(response, 'What are we escrowing today?')
        convo.refresh_from_db()
        self.assertEqual(convo.messages.count(), 0)

    def test_page_load_restarts_even_live_flow(self):
        from agent.loop import propose_action
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(role='user', content='keep me')
        convo.pending_email = 'mid@x.com'
        convo.pending_context = {'join_escrow_reference': 'LKTRabc12'}
        convo.save(update_fields=['pending_email', 'pending_context'])
        propose_action(convo, 'request_escrow_creation', {'draft_id': 3})
        session = self.client.session
        session['chat_verified_email'] = 'mid@x.com'
        session.save()
        response = self.client.get('/assistant/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'keep me')
        self.assertNotContains(response, 'Your call:')
        self.assertContains(response, 'What are we escrowing today?')
        convo.refresh_from_db()
        self.assertEqual(convo.messages.count(), 0)
        self.assertEqual(convo.pending_action, {})
        self.assertIsNone(convo.pending_email)
        self.assertEqual(convo.pending_context, {})
        self.assertIsNone(convo.owner_email)

    def test_home_load_wipes_idle_history(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(role='user', content='stale home hi')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'stale home hi')

    def test_creation_resume_advances_to_counterparty(self):
        """Resume-after-code must leave the escrow joinable, not stranded."""
        from escrow.models import EscrowDraft
        from agent.loop import get_or_create_conversation
        self.client.get('/assistant/')
        from agent.models import AIConversation
        from decimal import Decimal
        convo = AIConversation.objects.get(channel='web')
        session_key = self.client.session.session_key
        draft = EscrowDraft.objects.create(
            owner_email='', owner_session=session_key,
            title='T', description='D', amount=Decimal('50000'),
            fee_payer='buyer', creator_role='buyer',
            counterparty_email='cp@example.com', inspection_days=3)
        from agent.loop import propose_action
        propose_action(convo, 'request_escrow_creation', {'draft_id': draft.id})
        convo.pending_action['verification_email'] = 'creator@example.com'
        convo.pending_action['draft_id'] = draft.id
        convo.save(update_fields=['pending_action'])
        draft.owner_email = 'creator@example.com'
        draft.save(update_fields=['owner_email'])
        from escrow.services import issue_otp
        with patch('escrow.services.send_otp_via_termii', return_value=True) as mock_send:
            issue_otp('creator@example.com', 'create')
            code = mock_send.call_args[0][1]
        response = self.client.post('/assistant/send/', {'message': code})
        self.assertEqual(response.status_code, 200)
        from escrow.models import EscrowContract
        contract = EscrowContract.objects.get(creator_email='creator@example.com')
        self.assertEqual(contract.status, 'awaiting_counterparty')

    def test_complete_draft_auto_proposes_creation(self):
        from decimal import Decimal
        from escrow.models import EscrowDraft
        from agent.loop import run_turn
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-auto-convo')
        convo.owner_email = 'buyer@example.com'
        convo.save(update_fields=['owner_email'])
        draft = EscrowDraft.objects.create(
            owner_email='buyer@example.com', owner_session='sess-auto',
            title='Laptop', description='Laptop', amount=Decimal('50000'),
            fee_payer='buyer', creator_role='buyer',
            counterparty_email='seller@example.com', inspection_days=3)
        update_call = {
            'id': 'u1', 'name': 'update_escrow_draft',
            'arguments': {'draft_id': draft.id, 'title': 'Laptop'},
        }
        with patch('agent.loop.chat', side_effect=[
                {'text': '', 'tool_calls': [update_call], 'usage': {}},
                {'text': 'Here are the terms, confirm?', 'tool_calls': [],
                 'usage': {}},
        ]):
            run_turn(convo, 'looks good', session_key='sess-auto')
        convo.refresh_from_db()
        pending = convo.pending_action or {}
        self.assertEqual(pending.get('tool'), 'request_escrow_creation')
        self.assertEqual((pending.get('arguments') or {}).get('draft_id'), draft.id)

    def test_prose_only_turn_does_not_auto_propose(self):
        # A turn that merely talks about a deal must never park a
        # creation proposal: a later "yes" meant for another action
        # (e.g. confirming dispatch) would otherwise execute it and
        # create a stray escrow.
        from decimal import Decimal
        from escrow.models import EscrowDraft
        from agent.loop import run_turn
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-auto-convo2')
        convo.owner_email = 'buyer@example.com'
        convo.save(update_fields=['owner_email'])
        EscrowDraft.objects.create(
            owner_email='buyer@example.com', owner_session='sess-auto2',
            title='Laptop', description='Laptop', amount=Decimal('50000'),
            fee_payer='buyer', creator_role='buyer',
            counterparty_email='seller@example.com', inspection_days=3)
        with patch('agent.loop.chat', return_value={
                'text': 'Here are the terms, confirm?', 'tool_calls': [],
                'usage': {}}):
            run_turn(convo, 'looks good', session_key='sess-auto2')
        convo.refresh_from_db()
        self.assertEqual(convo.pending_action, {})

    def test_incomplete_draft_does_not_auto_propose(self):
        from escrow.models import EscrowDraft
        from agent.loop import run_turn
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-auto2-convo')
        convo.owner_email = 'buyer@example.com'
        convo.save(update_fields=['owner_email'])
        EscrowDraft.objects.create(
            owner_email='buyer@example.com', owner_session='sess-auto2',
            title='Partial only')
        with patch('agent.loop.chat', return_value={
                'text': 'What else?', 'tool_calls': [], 'usage': {}}):
            run_turn(convo, 'hi', session_key='sess-auto2')
        convo.refresh_from_db()
        self.assertEqual(convo.pending_action, {})

    def test_bare_yes_executes_pending_without_model(self):
        from agent.loop import propose_action, run_turn
        from escrow.models import EscrowContract
        from decimal import Decimal
        EscrowContract.objects.create(
            code='LKTRdy001', title='T', description='D',
            amount=Decimal('50000'), service_fee=Decimal('1500'),
            fee_payer='buyer', creator_email='buyer@x.com',
            creator_role='buyer', counterparty_email='seller@x.com',
            status='in_transit', gateway_reference='REF_DY1')
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-direct')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        propose_action(convo, 'request_dispute_submission',
                       {'escrow_reference': 'LKTRdy001', 'reason': 'Late'})
        with patch('agent.loop.chat') as mock_chat:
            reply = run_turn(convo, 'yes')
            mock_chat.assert_not_called()
        self.assertIn('Dispute raised', reply)
        self.assertEqual(
            EscrowContract.objects.get(code='LKTRdy001').status, 'disputed')

    def test_yes_but_changed_terms_goes_to_model(self):
        from agent.loop import propose_action, run_turn
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-direct')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        propose_action(convo, 'request_dispute_submission',
                       {'escrow_reference': 'X', 'reason': 'Late'})
        with patch('agent.loop.chat', return_value={
                'text': 'Tell me more.', 'tool_calls': [], 'usage': {}}) as mock_chat:
            run_turn(convo, 'yes but change it to 40000')
            mock_chat.assert_called()

    def test_natural_yes_executes_pending(self):
        from agent.loop import propose_action, run_turn
        from escrow.models import EscrowContract
        from decimal import Decimal
        EscrowContract.objects.create(
            code='LKTRdy002', title='T', description='D',
            amount=Decimal('50000'), service_fee=Decimal('1500'),
            fee_payer='buyer', creator_email='buyer@x.com',
            creator_role='buyer', counterparty_email='seller@x.com',
            status='in_transit', gateway_reference='REF_DY2')
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', 'sess-direct2')
        convo.owner_email = 'buyer@x.com'
        convo.save(update_fields=['owner_email'])
        propose_action(convo, 'request_dispute_submission',
                       {'escrow_reference': 'LKTRdy002', 'reason': 'Late'})
        with patch('agent.loop.chat') as mock_chat:
            reply = run_turn(convo, 'Yes, raise it now.')
            mock_chat.assert_not_called()
        self.assertIn('Dispute raised', reply)

    def test_paypal_save_reply_states_paypal_email(self):
        from agent.loop import _direct_reply
        reply = _direct_reply('request_seller_bank_details', {
            'ok': True, 'data': {
                'paypal_email': 'seller@example.com',
                'masked': 'se***@example.com'}})
        self.assertIn('seller@example.com', reply)


class PendingTermsTest(TestCase):
    def make_draft(self):
        from decimal import Decimal
        from escrow.models import EscrowDraft
        return EscrowDraft.objects.create(
            owner_email='buyer@example.com', owner_session='',
            title='Laptop', description='Used laptop',
            amount=Decimal('500.00'), currency='USD', fee_payer='buyer',
            creator_role='buyer', counterparty_email='seller@example.com',
            inspection_days=3, status='draft',
        )

    def test_pending_terms_breakdown(self):
        from agent.views import _pending_terms
        draft = self.make_draft()
        terms = _pending_terms({'tool': 'request_escrow_creation',
                                'arguments': {'draft_id': draft.id}})
        self.assertIsNotNone(terms)
        self.assertEqual(terms['service_fee'], '$2.50')
        self.assertEqual(terms['buyer_pays'], '$502.50')
        self.assertEqual(terms['seller_receives'], '$500.00')

    def test_pending_terms_none_without_draft(self):
        from agent.views import _pending_terms
        self.assertIsNone(_pending_terms({}))
        self.assertIsNone(_pending_terms(
            {'tool': 'request_escrow_creation', 'arguments': {}}))
        self.assertIsNone(_pending_terms(
            {'tool': 'request_dispute_submission', 'arguments': {}}))

    def test_thread_renders_fee_breakdown(self):
        from django.template.loader import render_to_string
        draft = self.make_draft()
        from agent.views import _pending_terms
        html = render_to_string('agent/_thread.html', {
            'messages': [],
            'pending': {'tool': 'request_escrow_creation',
                        'arguments': {'draft_id': draft.id}},
            'pending_terms': _pending_terms(
                {'tool': 'request_escrow_creation',
                 'arguments': {'draft_id': draft.id}}),
            'last_message_id': 0,
        })
        self.assertIn('$2.50', html)
        self.assertIn('$502.50', html)
