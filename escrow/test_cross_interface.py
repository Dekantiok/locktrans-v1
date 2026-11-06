"""Cross-interface consistency (spec sections 9, 15.6).

The web form, website AI assistant, and WhatsApp AI assistant must produce
consistent records through the same validation, authorization, and
financial rules, never separate versions of the business logic.
"""
import inspect
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from agent.loop import confirmed, get_or_create_conversation
from agent.registry import CONSEQUENTIAL_TOOLS, execute
from escrow.forms import ContractCreationForm
from escrow.models import EscrowContract, EscrowDraft
from escrow.services import contracts_for_email, create_escrow_from_validated


def valid_data(**overrides):
    data = {
        'title': 'Laptop',
        'description': 'Laptop deal',
        'amount': Decimal('500000'),
        'fee_payer': 'buyer',
        'creator_email': 'buyer@x.com',
        'creator_role': 'buyer',
        'counterparty_email': 'seller@x.com',
        'inspection_days': 3,
    }
    data.update(overrides)
    return data


class CrossInterfaceTest(TestCase):
    def test_all_paths_share_one_creation_function(self):
        from agent import registry
        from whatsapp import flow
        from agent import views as agent_views
        # The tool validates and parks on email proof; both resume paths
        # (web chat, WhatsApp) create through the same shared function and
        # advance to awaiting_counterparty so the counterparty can join.
        tool_src = inspect.getsource(registry.tool_request_escrow_creation)
        self.assertIn('verification_required', tool_src)
        self.assertNotIn('create_escrow_from_validated', tool_src)
        for resume_src in (inspect.getsource(agent_views._try_resume_creation_with_code),
                           inspect.getsource(flow._try_resume_creation)):
            self.assertIn('create_escrow_from_validated', resume_src)
            self.assertIn('awaiting_counterparty', resume_src)

    def test_web_form_and_agent_records_are_consistent(self):
        form = ContractCreationForm(valid_data())
        self.assertTrue(form.is_valid(), form.errors)
        contract = create_escrow_from_validated(form.cleaned_data)
        self.assertEqual(contract.service_fee, Decimal('10.00'))
        self.assertEqual(contract.status, 'pending_verification')
        # Both participants see it; strangers do not.
        self.assertTrue(contracts_for_email('buyer@x.com').filter(code=contract.code).exists())
        self.assertTrue(contracts_for_email('seller@x.com').filter(code=contract.code).exists())
        self.assertFalse(contracts_for_email('stranger@x.com').filter(code=contract.code).exists())

    def test_draft_is_never_a_live_escrow(self):
        before = EscrowContract.objects.count()
        EscrowDraft.objects.create(
            owner_email='a@x.com', title='T', amount=Decimal('10000'),
            fee_payer='buyer', creator_role='buyer', counterparty_email='b@x.com')
        self.assertEqual(EscrowContract.objects.count(), before)

    def test_consequential_tools_require_confirmation(self):
        convo = get_or_create_conversation('web', 'cross-check')
        for tool in CONSEQUENTIAL_TOOLS:
            self.assertFalse(confirmed(convo, tool, {'x': 1}))

    def test_agent_status_respects_participation(self):
        contract = EscrowContract.objects.create(
            code='CROSS001', title='T', description='D', amount=Decimal('50000'),
            service_fee=Decimal('1500'), fee_payer='buyer',
            creator_email='buyer@x.com', creator_role='buyer',
            counterparty_email='seller@x.com', status='awaiting_funding',
            gateway_reference='REF_CROSS001')
        self.assertFalse(execute(
            'get_authorized_escrow_status', {'escrow_reference': contract.code},
            actor_email='stranger@x.com')['ok'])
        ok = execute(
            'get_authorized_escrow_status', {'escrow_reference': contract.code},
            actor_email='seller@x.com')
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['data']['you_are'], 'seller')

    def test_whatsapp_identity_never_leaks_into_escrow(self):
        from whatsapp.flow import handle_text_message
        with patch('whatsapp.adapter.send_text_message', return_value=True), \
             patch('whatsapp.flow.run_turn') as mock_turn:
            mock_turn.side_effect = lambda c, t, base_url='', session_key='': (
                f"actor={c.owner_email!r} session={session_key}")
            reply = handle_text_message('2348000000999', 'What is Locktrans.AI?')
        self.assertIn('actor=None', reply)
        self.assertIn('whatsapp:2348000000999', reply)
