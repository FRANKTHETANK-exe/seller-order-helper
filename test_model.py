import unittest
from classifier import classify_order_reply

class TestClassifier(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(classify_order_reply("CREATED"), "payment_pending")
        self.assertEqual(classify_order_reply("APPROVED"), "payment_pending")
        self.assertEqual(classify_order_reply("COMPLETED"), "payment_pending")
        self.assertEqual(classify_order_reply("VOIDED"), "order_voided")
    def test_invalid(self):
        with self.assertRaises(ValueError):
            classify_order_reply("unknown")
        with self.assertRaises(ValueError):
            classify_order_reply("completed")
        with self.assertRaises(ValueError):
            classify_order_reply(None)
        with self.assertRaises(ValueError):
            classify_order_reply(True)
        with self.assertRaises(ValueError):
            classify_order_reply(1)
        with self.assertRaises(ValueError):
            classify_order_reply("")
        with self.assertRaises(ValueError):
            classify_order_reply(["a"])

if __name__ == "__main__":
    unittest.main()
