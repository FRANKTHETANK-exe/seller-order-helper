"""Offline client-stub tests; no actual local model or network is called."""
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from facts import OrderFacts
from suggestions import (LABEL, MAX_MODEL_OUTPUT_BYTES, MAX_PROMPT_BYTES,
                         ApprovedReply, ReplySuggestion, StaleFactsError,
                         SuggestionError, fingerprint_order_facts,
                         generate_suggestion)


class StubClient:
    def __init__(self, phrase_id="status_first", style_id="concise", **overrides):
        self.phrase_id = phrase_id
        self.style_id = style_id
        self.overrides = overrides
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        facts = json.loads(prompt)["facts"]
        selection = {"order_id": facts["order_id"], "category": facts["category"],
                     "phrase_id": self.phrase_id, "style_id": self.style_id}
        selection.update(self.overrides)
        return json.dumps(selection)


class SuggestionsTests(unittest.TestCase):
    def test_same_category_different_client_choices_change_wording(self):
        facts = OrderFacts("ORDER_DEMO_1", "APPROVED")
        first = generate_suggestion(facts, StubClient("status_first", "concise"))
        second = generate_suggestion(facts, StubClient("item_first", "friendly"))
        self.assertEqual(first.category, second.category)
        self.assertNotEqual(first.reply_text, second.reply_text)
        self.assertIn("Thanks for checking in.", second.reply_text)
        for suggestion in (first, second):
            self.assertIsInstance(suggestion, ReplySuggestion)
            self.assertIn(LABEL, suggestion.reply_text)
            self.assertIn("Human review required", suggestion.reply_text)
            self.assertTrue(suggestion.human_review_required)
            self.assertFalse(suggestion.model_execution_verified)
            self.assertIn(facts.order_id, suggestion.reply_text)
            self.assertIn("Demo notebook", suggestion.reply_text)
            self.assertIn("USD 10.00", suggestion.reply_text)
            self.assertFalse(hasattr(suggestion, "send"))

    def test_prompt_contains_only_the_supplied_synthetic_context(self):
        facts = OrderFacts("ORDER_DEMO_2", "COMPLETED", "COMPLETED")
        client = StubClient()
        generate_suggestion(facts, client)
        self.assertEqual(len(client.prompts), 1)
        prompt = client.prompts[0]
        data = json.loads(prompt)
        self.assertEqual(data["facts"], facts.model_context())
        self.assertLessEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)
        self.assertEqual(data["selection_schema"]["properties"]["category"]["const"], "payment_confirmed")
        self.assertFalse(data["selection_schema"]["additionalProperties"])
        self.assertNotIn("customer", data["facts"])

    def test_completed_order_without_completed_capture_stays_pending(self):
        for capture in (None, "PENDING"):
            facts = OrderFacts("ORDER_PENDING", "COMPLETED", capture)
            draft = generate_suggestion(facts, StubClient())
            self.assertEqual(draft.category, "payment_pending")
            self.assertIn("Payment is not confirmed", draft.reply_text)

    def test_only_supported_grounded_category_composes(self):
        pairs = [(OrderFacts("P", "CREATED"), "payment_pending"),
                 (OrderFacts("C", "COMPLETED", "COMPLETED"), "payment_confirmed"),
                 (OrderFacts("V", "VOIDED"), "order_voided")]
        for facts, category in pairs:
            with self.subTest(category=category):
                draft = generate_suggestion(facts, StubClient())
                self.assertEqual(draft.category, category)
                self.assertNotIn("refund", draft.reply_text.lower())
                self.assertNotIn("shipped", draft.reply_text.lower())

    def test_false_confirmed_category_on_approved_is_rejected(self):
        facts = OrderFacts("ORDER_APPROVED", "APPROVED")
        with self.assertRaises(SuggestionError):
            generate_suggestion(facts, StubClient(category="payment_confirmed"))

    def test_false_confirmed_category_without_capture_is_rejected(self):
        facts = OrderFacts("ORDER_COMPLETED", "COMPLETED")
        with self.assertRaises(SuggestionError):
            generate_suggestion(facts, StubClient(category="payment_confirmed"))

    def test_wrong_order_unknown_ids_and_unsupported_category_rejected(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        for changes in ({"order_id": "ORDER_TWO"}, {"phrase_id": "refund_issued"},
                        {"style_id": "unreviewed"}, {"category": "shipped"}):
            with self.subTest(changes=changes), self.assertRaises(SuggestionError):
                generate_suggestion(facts, StubClient(**changes))

    def test_arbitrary_reply_claim_or_approval_field_is_rejected(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        for extra in ({"reply": "Payment captured and item shipped."},
                      {"human_approved": True}, {"shipping_status": "delivered"}):
            with self.subTest(extra=extra), self.assertRaises(SuggestionError):
                generate_suggestion(facts, StubClient(**extra))

    def test_malformed_missing_duplicate_non_string_and_nonfinite_json_rejected(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        malformed = ["", "not json", "```json\n{}\n```", "[]", "null", "{}",
                     '{"order_id":"ORDER_ONE","category":"payment_pending","phrase_id":"status_first","style_id":2}',
                     '{"order_id":"ORDER_ONE","order_id":"ORDER_TWO","category":"payment_pending","phrase_id":"status_first","style_id":"concise"}',
                     '{"order_id":"ORDER_ONE","category":"payment_pending","phrase_id":"status_first","style_id":NaN}',
                     "\ud800", b"{}", None]
        for response in malformed:
            with self.subTest(response=repr(response)), self.assertRaises(SuggestionError):
                generate_suggestion(facts, lambda prompt: response)

    def test_oversized_output_rejected_including_multibyte_text(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        for response in (" " * (MAX_MODEL_OUTPUT_BYTES + 1), "é" * MAX_MODEL_OUTPUT_BYTES):
            with self.assertRaises(SuggestionError):
                generate_suggestion(facts, lambda prompt: response)

    def test_client_exception_is_bounded_and_not_leaked(self):
        def failed_client(prompt):
            raise RuntimeError("PRIVATE CLIENT FAILURE DETAIL")
        with self.assertRaises(SuggestionError) as raised:
            generate_suggestion(OrderFacts("ORDER_ONE", "APPROVED"), failed_client)
        self.assertEqual(str(raised.exception), "Injected model client failed")

    def test_approval_requires_original_facts_and_retains_simulation_label(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        draft = generate_suggestion(facts, StubClient())
        approved = draft.approve(facts)
        self.assertIsInstance(approved, ApprovedReply)
        self.assertTrue(approved.human_review_completed)
        self.assertFalse(approved.human_review_required)
        self.assertIn(LABEL, approved.reply_text)
        self.assertIn("Human review completed", approved.reply_text)
        self.assertIn("no message has been sent", approved.reply_text)
        self.assertFalse(hasattr(approved, "send"))
        self.assertEqual(approved.facts_fingerprint, fingerprint_order_facts(facts))
        with self.assertRaises(FrozenInstanceError):
            draft.category = "payment_confirmed"

    def test_stale_facts_reject_even_when_category_is_unchanged(self):
        original = OrderFacts("ORDER_ONE", "CREATED")
        draft = generate_suggestion(original, StubClient())
        for changed in (replace(original, status="APPROVED"),
                        replace(original, order_id="ORDER_TWO"),
                        OrderFacts("ORDER_ONE", "COMPLETED", "COMPLETED")):
            with self.subTest(changed=changed), self.assertRaises(StaleFactsError):
                draft.approve(changed)

    def test_tampered_draft_text_or_unapproved_body_cannot_be_approved(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        draft = generate_suggestion(facts, StubClient())
        for altered in (replace(draft, reply_text="Payment captured and item shipped."),
                        replace(draft, _approved_body="Payment captured and item shipped."),
                        replace(draft, phrase_id="refund_issued")):
            with self.assertRaises(SuggestionError):
                altered.approve(facts)

    def test_capture_fact_change_rejects_stale_review(self):
        original = OrderFacts("ORDER_ONE", "COMPLETED")
        draft = generate_suggestion(original, StubClient())
        with self.assertRaises(StaleFactsError):
            draft.approve(replace(original, capture_status="PENDING"))

    def test_changed_facts_during_client_call_are_rejected(self):
        facts = OrderFacts("ORDER_ONE", "CREATED")
        client = StubClient()
        def changing_client(prompt):
            result = client(prompt)
            object.__setattr__(facts, "status", "APPROVED")
            return result
        with self.assertRaises(StaleFactsError):
            generate_suggestion(facts, changing_client)

    def test_invalid_client_or_facts_rejected_before_client_call(self):
        facts = OrderFacts("ORDER_ONE", "APPROVED")
        with self.assertRaises(SuggestionError):
            generate_suggestion(facts, None)
        client = StubClient()
        with self.assertRaises(SuggestionError):
            generate_suggestion({"status": "APPROVED"}, client)
        self.assertEqual(client.prompts, [])

    def test_oversized_synthetic_identifier_is_rejected_by_facts_contract(self):
        with self.assertRaises(ValueError):
            OrderFacts("A" * 65, "APPROVED")


if __name__ == "__main__":
    unittest.main()
