"""Seller-review regressions using synthetic facts and fake/zero-inference clients."""
import json
import unittest
from unittest.mock import patch

from local_model import StubClient
from sandbox_ai_review import facts_from_receipt, generate_sandbox_review, record_human_review
from sandbox_reconciliation import read_recorded_evidence
from sandbox_review_ui import ReviewSession, ReviewUIError
from suggestions import SuggestionError


class SellerGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.facts = facts_from_receipt(read_recorded_evidence())

    def test_payment_reply_provides_seller_check_without_claiming_payment(self):
        draft = generate_sandbox_review(self.facts, StubClient(), 'Has my payment gone through?')
        self.assertIn('current matching PayPal transaction and capture record', draft.seller_next_step)
        self.assertIn('before confirming payment', draft.seller_next_step)
        self.assertIn('does not show buyer approval or captured payment', draft.review_text)
        self.assertFalse(draft.sent)

    def test_exception_attention_is_deterministic_even_if_model_selects_payment(self):
        # A fake selection, not evidence of real Granite classification quality.
        class FixedSelection:
            def __call__(self, prompt):
                request=json.loads(prompt)
                return json.dumps({'order_id':request['facts']['order_id'],
                    'question_intent':'payment_status','phrase_id':'status_first','style_id':'concise'})
        cases=[
            ('I was charged twice for this order. What happens now?', 'duplicate_charge', 'both reported charges'),
            ('My PayPal receipt says I paid $100, but this order shows $10. What should I do?', 'amount_mismatch', 'amount and currency'),
            ('I did not authorize this charge. What should I do?', 'unrecognized_charge', 'unrecognized charge'),
        ]
        for question,flag,step in cases:
            with self.subTest(flag=flag):
                draft=generate_sandbox_review(self.facts,FixedSelection(),question)
                self.assertIn(flag,draft.seller_attention)
                self.assertIn(step,draft.seller_next_step)
                self.assertIn('does not verify the reported charge',draft.seller_next_step)
                self.assertNotIn('was refunded',draft.review_text)
                self.assertNotIn('payment succeeded',draft.review_text)
                self.assertFalse(draft.sent)

    def test_matching_amount_and_negated_duplicate_do_not_trigger_false_flags(self):
        for question in ('My receipt shows $10.00. Has my payment gone through?',
                         'I was not charged twice. Has my payment gone through?'):
            draft=generate_sandbox_review(self.facts,StubClient(),question)
            self.assertEqual(draft.seller_attention,())

    def test_refund_keyword_uses_meaningful_phrase_and_mixed_delivery_is_retained(self):
        cases=[('Please return my call about my payment.','payment_status'),
               ('Can I return this item?','refund_question'),
               ('I paid but haven’t received it. Can you help?','delivery_status')]
        for question,intent in cases:
            with self.subTest(question=question):
                draft=generate_sandbox_review(self.facts,StubClient(),question)
                self.assertEqual(draft.question_intent,intent)
                self.assertIn('does not show buyer approval or captured payment',draft.review_text)
                if intent=='delivery_status':
                    self.assertIn('shipping or delivery status',draft.review_text)
                    self.assertIn('fulfillment',draft.seller_next_step)

    def test_seller_note_is_bound_to_review_and_tampering_is_refused(self):
        draft=generate_sandbox_review(self.facts,StubClient())
        self.assertTrue(draft.seller_next_step)
        object.__setattr__(draft,'seller_next_step','Payment confirmed. Ship now.')
        with self.assertRaises(SuggestionError):
            record_human_review(draft,self.facts,draft.review_digest)


class NewOfflineQuestionTests(unittest.TestCase):
    def test_explicit_new_question_clears_old_review_without_running_client(self):
        session=ReviewSession(StubClient())
        old=session.generate('Has my payment gone through?')
        session.review(old['draft']['digest'])
        with patch.object(StubClient,'__call__',side_effect=AssertionError('No generation during reset')):
            state=session.new_question(old['question_version'])
        self.assertIsNone(state['draft'])
        self.assertFalse(state['generation_attempted'])
        self.assertEqual(state['review_status'],'not_generated')
        self.assertEqual(state['question_version'],old['question_version']+1)
        with self.assertRaises(ReviewUIError):
            session.generate('Stale tab request',question_version=old['question_version'])
        with self.assertRaises(ReviewUIError):
            session.new_question(old['question_version'])
        new=session.generate('When will my package arrive?',question_version=state['question_version'])
        self.assertEqual(new['draft']['question_intent'],'delivery_status')
        self.assertEqual(new['review_status'],'pending_human_review')
        self.assertEqual(new['external_action'],'none')
        with self.assertRaises(ReviewUIError):
            session.review(new['draft']['digest'],question_version=old['question_version'])
        with self.assertRaises(ReviewUIError):
            session.generate('Again',question_version=state['question_version'])

    def test_new_question_requires_finished_offline_draft(self):
        session=ReviewSession(StubClient())
        with self.assertRaises(ReviewUIError): session.new_question(0)
        with self.assertRaises(ReviewUIError): session.generate('')
        self.assertFalse(session.attempted)
        session.generate('Has my payment gone through?')
        with self.assertRaises(ReviewUIError): session.new_question(True)
        self.assertIsNotNone(session.suggestion)

    def test_real_recorded_live_and_custom_clients_cannot_be_reset(self):
        class FakeModel:
            kind='granite'; calls=0
            def __call__(self,prompt):
                self.calls+=1
                return StubClient()(prompt)
        class SpoofedStub(FakeModel): kind='stub'
        for client in (FakeModel(),SpoofedStub()):
            session=ReviewSession(client); session.generate()
            with self.assertRaises(ReviewUIError): session.new_question(0)
            self.assertEqual(client.calls,1)
            self.assertTrue(session.attempted)
        session=ReviewSession(StubClient(),refresh_known_order=lambda: self.fail('No live call'))
        with self.assertRaises(ReviewUIError): session.new_question(0)
        self.assertFalse(session.refresh_attempted)
        session=ReviewSession(StubClient()); session.generate()
        session.facts=facts_from_receipt({**read_recorded_evidence(),'source':'saved_paypal_sandbox_receipt'})
        with self.assertRaises(ReviewUIError): session.new_question(0)
        self.assertTrue(session.attempted)

if __name__=='__main__': unittest.main()
