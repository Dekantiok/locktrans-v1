"""Tests for the WhatsApp adapter + agent wiring (all network calls mocked)."""
import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse


def inbound_payload(wamid='wamid.1', sender='2348000000001', body='Hello'):
    return {
        'object': 'whatsapp_business_account',
        'entry': [{
            'id': 'entry-1',
            'changes': [{
                'value': {
                    'messaging_product': 'whatsapp',
                    'messages': [{
                        'from': sender, 'id': wamid,
                        'timestamp': '1234567890',
                        'type': 'text', 'text': {'body': body},
                    }],
                },
            }],
        }],
    }


def signed(body_bytes, secret='test-secret'):
    digest = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


@override_settings(
    WHATSAPP_APP_SECRET='test-secret',
    WHATSAPP_WEBHOOK_VERIFY_TOKEN='verify-me',
    WHATSAPP_ACCESS_TOKEN='tok',
    WHATSAPP_PHONE_NUMBER_ID='123',
)
class WebhookTest(TestCase):
    def setUp(self):
        self.url = reverse('whatsapp:webhook')
        self.send_patcher = patch('whatsapp.adapter.send_text_message', return_value=True)
        self.mock_flow_send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)
        # send_otp_via_termii is reached through escrow.services.issue_otp
        self.otp_patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.otp_patcher.start()
        self.addCleanup(self.otp_patcher.stop)

    def post_signed(self, payload, secret='test-secret'):
        body = json.dumps(payload).encode()
        return self.client.post(
            self.url, data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=signed(body, secret),
        )

    def test_verification_challenge(self):
        response = self.client.get(self.url, {
            'hub.mode': 'subscribe',
            'hub.verify_token': 'verify-me',
            'hub.challenge': 'CHALLENGE_OK',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), 'CHALLENGE_OK')

    def test_verification_rejects_bad_token(self):
        response = self.client.get(self.url, {
            'hub.mode': 'subscribe',
            'hub.verify_token': 'wrong',
            'hub.challenge': 'X',
        })
        self.assertEqual(response.status_code, 403)

    def test_bad_signature_is_rejected(self):
        body = json.dumps(inbound_payload()).encode()
        response = self.client.post(
            self.url, data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256='sha256=wrong',
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_json_is_acked(self):
        body = b'{not json'
        response = self.client.post(
            self.url, data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=signed(body),
        )
        self.assertEqual(response.status_code, 200)

    def test_greeting_asks_for_email_when_unverified(self):
        response = self.post_signed(inbound_payload(body='hi'))
        self.assertEqual(response.status_code, 200)
        from whatsapp.models import WhatsAppLink
        link = WhatsAppLink.objects.get(wa_id='2348000000001')
        self.assertEqual(link.verified_email, '')
        self.assertTrue(self.mock_flow_send.called)

    def test_duplicate_events_are_ignored(self):
        from escrow.models import WebhookEvent
        self.post_signed(inbound_payload(wamid='wamid.dup', body='hi'))
        first_count = WebhookEvent.objects.filter(
            provider='whatsapp', event_id='wamid.dup').count()
        self.post_signed(inbound_payload(wamid='wamid.dup', body='hi'))
        second_count = WebhookEvent.objects.filter(
            provider='whatsapp', event_id='wamid.dup').count()
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)
        record = WebhookEvent.objects.get(provider='whatsapp', event_id='wamid.dup')
        self.assertTrue(record.processed)

    def test_status_events_are_recorded_without_reply(self):
        payload = {
            'object': 'whatsapp_business_account',
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'statuses': [{'id': 'wamid.1', 'status': 'delivered',
                              'recipient_id': '2348000000001'}],
            }}]}],
        }
        with patch('whatsapp.adapter.send_text_message') as mock_send:
            response = self.post_signed(payload)
        self.assertEqual(response.status_code, 200)
        mock_send.assert_not_called()

    def test_conversation_isolation_between_senders(self):
        with patch('whatsapp.flow.run_turn', return_value='ok') as mock_turn:
            self.post_signed(inbound_payload(wamid='wamid.A', sender='234800000000A', body='fee?'))
            self.post_signed(inbound_payload(wamid='wamid.B', sender='234800000000B', body='fee?'))
        from agent.models import AIConversation
        self.assertEqual(
            AIConversation.objects.filter(channel='whatsapp').count(), 2)
        self.assertEqual(mock_turn.call_count, 2)
        sessions = {call.kwargs.get('session_key') for call in mock_turn.call_args_list}
        self.assertEqual(len(sessions), 2)


@override_settings(
    WHATSAPP_APP_SECRET='test-secret',
    WHATSAPP_ACCESS_TOKEN='tok',
    WHATSAPP_PHONE_NUMBER_ID='123',
)
class OtpFlowTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('whatsapp.adapter.send_text_message', return_value=True)
        self.mock_send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)
        self.otp_patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_otp = self.otp_patcher.start()
        self.addCleanup(self.otp_patcher.stop)

    def test_email_captures_pending_and_code_verifies(self):
        from whatsapp.flow import handle_text_message
        from whatsapp.models import WhatsAppLink
        handle_text_message('2348000000009', 'user@example.com')
        link = WhatsAppLink.objects.get(wa_id='2348000000009')
        self.assertEqual(link.pending_email, 'user@example.com')
        self.assertTrue(self.mock_otp.called)
        code = self.mock_otp.call_args[0][1]
        reply = handle_text_message('2348000000009', code)
        link.refresh_from_db()
        self.assertEqual(link.verified_email, 'user@example.com')
        self.assertEqual(link.pending_email, '')
        self.assertIn('Verified', reply)

    def test_wrong_code_does_not_verify(self):
        from whatsapp.flow import handle_text_message
        from whatsapp.models import WhatsAppLink
        handle_text_message('2348000000008', 'other@example.com')
        reply = handle_text_message('2348000000008', '000000')
        link = WhatsAppLink.objects.get(wa_id='2348000000008')
        self.assertEqual(link.verified_email, '')
        self.assertIn('not correct', reply)

    def test_sender_number_is_never_treated_as_identity(self):
        from unittest.mock import patch as mock_patch
        from agent.models import AIConversation
        from whatsapp.flow import handle_text_message
        with mock_patch('whatsapp.flow.run_turn') as mock_turn:
            def fake_turn(conversation, text, base_url='', session_key=''):
                return f"actor={conversation.owner_email!r} session={session_key}"
            mock_turn.side_effect = fake_turn
            reply = handle_text_message('2348000000007', 'What is Locktrans?')
        convo = AIConversation.objects.get(
            channel='whatsapp', channel_identity='2348000000007')
        self.assertIsNone(convo.owner_email)
        self.assertIn("actor=None", reply)
        self.assertIn('whatsapp:2348000000007', reply)

    def test_outbound_errors_do_not_raise(self):
        from whatsapp import adapter
        self.send_patcher.stop()
        try:
            with patch('whatsapp.adapter.requests.post',
                       side_effect=Exception('network down')):
                self.assertFalse(adapter.send_text_message('2348', 'hi'))
        finally:
            self.send_patcher.start()


@override_settings(
    WHATSAPP_APP_SECRET='test-secret',
    WHATSAPP_ACCESS_TOKEN='tok',
    WHATSAPP_PHONE_NUMBER_ID='123',
)
class CreationResumeTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('whatsapp.adapter.send_text_message', return_value=True)
        self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)
        self.otp_patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_otp = self.otp_patcher.start()
        self.addCleanup(self.otp_patcher.stop)

    def test_creation_otp_completes_escrow_over_whatsapp(self):
        from agent.loop import get_or_create_conversation, propose_action
        from escrow.models import EscrowContract, EscrowDraft
        from whatsapp.flow import handle_text_message
        from whatsapp.models import WhatsAppLink
        wa_id = '2348000000042'
        draft = EscrowDraft.objects.create(
            owner_email='', owner_session=f'whatsapp:{wa_id}',
            title='Laptop', description='Laptop',
            amount='500000', fee_payer='buyer',
            creator_role='buyer', counterparty_email='seller@example.com',
            inspection_days=3, status='draft',
        )
        # Simulate the agent having proposed creation and sent the code.
        link = WhatsAppLink.objects.create(wa_id=wa_id, pending_email='')
        conversation = get_or_create_conversation('whatsapp', wa_id)
        propose_action(conversation, 'request_escrow_creation', {'draft_id': draft.id})
        conversation.pending_action['verification_email'] = 'buyer@example.com'
        conversation.pending_action['draft_id'] = draft.id
        conversation.save(update_fields=['pending_action'])
        draft.owner_email = 'buyer@example.com'
        draft.save(update_fields=['owner_email'])
        code = self.mock_otp.call_args[0][1] if self.mock_otp.called else None
        # Issue the creation OTP explicitly to mirror the registry path.
        from escrow.services import issue_otp
        with patch('escrow.services.send_otp_via_termii', return_value=True) as mock_send2:
            issue_otp('buyer@example.com', 'create')
            code = mock_send2.call_args[0][1]
        reply = handle_text_message(wa_id, code)
        self.assertIn('created', reply.lower())
        self.assertTrue(EscrowContract.objects.filter(
            creator_email='buyer@example.com').exists())
        link.refresh_from_db()
        self.assertEqual(link.verified_email, 'buyer@example.com')

    def test_escrow_code_triggers_join_request_for_stranger(self):
        from whatsapp.flow import handle_text_message
        with patch('whatsapp.flow.run_turn') as mock_turn:
            reply = handle_text_message('2348000000999', 'I want to join LKTRx1y2z')
        mock_turn.assert_not_called()
        self.assertIn('LKTRx1y2z', reply)
        self.assertIn('email', reply.lower())
        from agent.models import AIConversation
        convo = AIConversation.objects.get(
            channel='whatsapp', channel_identity='2348000000999')
        self.assertEqual(
            convo.pending_context.get('join_escrow_reference'), 'LKTRx1y2z')

    def test_escrow_code_resumes_for_verified_participant(self):
        from decimal import Decimal
        from escrow.models import EscrowContract
        EscrowContract.objects.create(
            code='LKTRV9R4T', title='T', description='D',
            amount=Decimal('50000'), service_fee=Decimal('1500'),
            fee_payer='buyer', creator_email='seller@x.com',
            creator_role='seller', counterparty_email='buyer@x.com',
            status='awaiting_counterparty', gateway_reference='REF_V9')
        from whatsapp.flow import handle_text_message
        from whatsapp.models import WhatsAppLink
        WhatsAppLink.objects.create(
            wa_id='2348000000998', verified_email='buyer@x.com')
        reply = handle_text_message('2348000000998', 'join LKTRV9R4T')
        self.assertIn('awaiting_funding', reply)
        self.assertEqual(
            EscrowContract.objects.get(code='LKTRV9R4T').status,
            'awaiting_funding')

    def test_media_download_rejects_offhost_url(self):
        from unittest.mock import MagicMock
        from whatsapp import adapter
        meta = MagicMock(status_code=200)
        meta.json.return_value = {'url': 'https://evil.example/x.ogg'}
        with patch.object(adapter.requests, 'get', return_value=meta) as mock_get:
            self.assertIsNone(adapter.download_media('m1'))
        mock_get.assert_called_once()

    def test_media_download_streams_under_cap(self):
        from unittest.mock import MagicMock
        from whatsapp import adapter
        meta = MagicMock(status_code=200)
        meta.json.return_value = {'url': 'https://graph.facebook.com/x'}
        blob = MagicMock(status_code=200)
        blob.iter_content.return_value = [b'a' * 100, b'b' * 100]
        with patch.object(adapter.requests, 'get', side_effect=[meta, blob]):
            with override_settings(WHATSAPP_MEDIA_MAX_BYTES=1000):
                self.assertEqual(adapter.download_media('m2'), b'a' * 100 + b'b' * 100)

    def test_media_download_stops_over_cap(self):
        from unittest.mock import MagicMock
        from whatsapp import adapter
        meta = MagicMock(status_code=200)
        meta.json.return_value = {'url': 'https://x.fbcdn.net/v/t/x.ogg'}
        blob = MagicMock(status_code=200)
        blob.iter_content.return_value = [b'a' * 600, b'b' * 600]
        with patch.object(adapter.requests, 'get', side_effect=[meta, blob]):
            with override_settings(WHATSAPP_MEDIA_MAX_BYTES=1000):
                self.assertIsNone(adapter.download_media('m3'))
