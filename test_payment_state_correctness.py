"""Official order/capture semantics; fixtures only, never a payment action."""
from pathlib import Path
import json,unittest
from classifier import classify_order_reply
from sandbox_adapter import parse_order,parse_known_sandbox_order,AdapterError
from facts import OrderFacts
from local_model import StubClient
from suggestions import generate_suggestion

ROOT=Path(__file__).parent/'fixtures/payment-state-audit'
def fixture(name):return json.loads((ROOT/(name+'.json')).read_text())

class PaymentStateCorrectnessTests(unittest.TestCase):
 def test_classifier_needs_explicit_matching_completed_capture(self):
  self.assertEqual(classify_order_reply('COMPLETED'),'payment_pending')
  self.assertEqual(classify_order_reply('COMPLETED',capture_status='PENDING'),'payment_pending')
  self.assertEqual(classify_order_reply('COMPLETED',capture_status='COMPLETED'),'payment_confirmed')
  for status in ('CREATED','APPROVED','VOIDED'):
   with self.assertRaises(ValueError):classify_order_reply(status,capture_status='COMPLETED')
  for capture in ('DECLINED','PARTIALLY_REFUNDED','REFUNDED','FAILED',True):
   with self.assertRaises(ValueError):classify_order_reply('COMPLETED',capture_status=capture)
 def test_completed_absent_pending_and_authorization_only_never_confirm_payment_in_prompt(self):
  for name in ('completed_absent','completed_pending','authorization_only','approved_absent'):
   with self.subTest(case=name):
    body=fixture(name);facts=parse_order(body)
    self.assertEqual(facts.category,'payment_pending')
    self.assertEqual(facts.model_context()['category'],'payment_pending')
    draft=generate_suggestion(facts,StubClient(style_id='concise'))
    self.assertNotEqual(draft.category,'payment_confirmed')
    self.assertIn('not confirmed',draft.reply_text)
 def test_successful_capture_is_positive_evidence_not_order_status_alone(self):
  body=fixture('captured');facts=parse_order(body)
  self.assertEqual(facts.capture_status,'COMPLETED');self.assertEqual(facts.category,'payment_confirmed')
  self.assertEqual(parse_known_sandbox_order(body,expected_id=body['id']).payment_truth,'CAPTURED')
 def test_declined_and_partially_refunded_captures_are_rejected_not_called_paid(self):
  for name in ('completed_declined','partially_refunded'):
   with self.subTest(case=name):
    body=fixture(name)
    with self.assertRaises(AdapterError):parse_order(body)
    with self.assertRaises(AdapterError) as error:parse_known_sandbox_order(body,expected_id=body['id'])
    self.assertEqual(error.exception.failure_code,'CAPTURE_STATUS_UNSUPPORTED')
 def test_partial_refund_collection_cannot_be_silently_discarded(self):
  body=fixture('capture_with_partial_refund_evidence')
  for parse in (lambda:parse_order(body),lambda:parse_known_sandbox_order(body,expected_id=body['id'])):
   with self.assertRaises(AdapterError) as error:parse()
   self.assertEqual(error.exception.failure_code,'REFUND_FACTS_UNSUPPORTED')
 def test_explicit_capture_order_binding_and_amount_currency_match(self):
  import copy
  body=fixture('captured');capture=body['purchase_units'][0]['payments']['captures'][0]
  capture['supplementary_data']={'related_ids':{'order_id':body['id']}}
  self.assertEqual(parse_known_sandbox_order(body,expected_id=body['id']).payment_truth,'CAPTURED')
  variants=[]
  wrong=copy.deepcopy(body);wrong['purchase_units'][0]['payments']['captures'][0]['supplementary_data']['related_ids']['order_id']='DIFFERENTORDER123';variants.append(wrong)
  wrong=copy.deepcopy(body);wrong['purchase_units'][0]['payments']['captures'][0]['amount']['value']='9.00';variants.append(wrong)
  wrong=copy.deepcopy(body);wrong['purchase_units'][0]['payments']['captures'][0]['amount']['currency_code']='EUR';variants.append(wrong)
  wrong=copy.deepcopy(body);wrong['id']='DIFFERENTORDER123';variants.append(wrong)
  for wrong in variants:
   with self.assertRaises(AdapterError):parse_known_sandbox_order(wrong,expected_id=body['id'])
  for wrong in variants[:3]:
   with self.assertRaises(AdapterError):parse_order(wrong)
 def test_completed_capture_with_explicit_refund_or_chargeback_reason_is_rejected(self):
  for reason in ('REFUNDED','CHARGEBACK'):
   body=fixture('captured');body['purchase_units'][0]['payments']['captures'][0]['status_details']={'reason':reason}
   for parse in (lambda:parse_order(body),lambda:parse_known_sandbox_order(body,expected_id=body['id'])):
    with self.assertRaises(AdapterError) as error:parse()
    self.assertEqual(error.exception.failure_code,'CAPTURE_ADJUSTMENT_CONFLICT')
 def test_read_view_preserves_unknowns_and_does_not_invent_approval(self):
  expected={'completed_absent':'COMPLETED_WITHOUT_CAPTURE_EVIDENCE','authorization_only':'COMPLETED_WITHOUT_CAPTURE_EVIDENCE','completed_pending':'CAPTURE_PENDING','approved_absent':'APPROVED_WITHOUT_CAPTURE_EVIDENCE','payer_action_required':'PAYER_ACTION_REQUIRED'}
  for name,truth in expected.items():
   body=fixture(name);self.assertEqual(parse_known_sandbox_order(body,expected_id=body['id']).payment_truth,truth)

if __name__=='__main__':unittest.main()
