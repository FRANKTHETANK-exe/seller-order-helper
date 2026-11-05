"""Independent Codex checks for input boundaries and payment-state semantics."""
import unittest
from classifier import classify_order_reply


class EqualsAnyStatus:
    def __eq__(self, other):
        return True


class TestContract(unittest.TestCase):
    def test_only_completed_confirms_payment(self):
        expected = {
            'CREATED': 'payment_pending', 'APPROVED': 'payment_pending',
            'COMPLETED': 'payment_pending', 'VOIDED': 'order_voided',
        }
        for status, classification in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_order_reply(status), classification)

    def test_noncanonical_strings_are_rejected(self):
        for status in ('UNKNOWN', 'created', 'approved', 'completed', 'voided',
                       ' COMPLETED', 'COMPLETED ', 'COMPLETED\n', '', 'REFUNDED'):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    classify_order_reply(status)

    def test_nonstrings_are_rejected(self):
        for status in (None, True, False, 0, 1, 1.0, b'COMPLETED', [],
                       ['COMPLETED'], {}, {'status': 'COMPLETED'}, set()):
            with self.subTest(status=repr(status)):
                with self.assertRaises(ValueError):
                    classify_order_reply(status)

    def test_foreign_equality_cannot_impersonate_a_status(self):
        with self.assertRaises(ValueError):
            classify_order_reply(EqualsAnyStatus())

    def test_rejected_mutable_input_is_unchanged(self):
        status = {'status': 'COMPLETED'}
        with self.assertRaises(ValueError):
            classify_order_reply(status)
        self.assertEqual(status, {'status': 'COMPLETED'})


if __name__ == '__main__':
    unittest.main()
