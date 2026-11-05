"""Allowlisted synthetic facts for one seller/product and three reply categories."""
from dataclasses import dataclass
import re

from classifier import classify_order_reply

SELLER_ID = 'DEMO_SELLER'
PRODUCT_ID = 'demo-notebook'
PRODUCT_NAME = 'Demo notebook'
CURRENCY = 'USD'
AMOUNT = '10.00'
CATEGORIES = ('payment_pending', 'payment_confirmed', 'order_voided')


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str
    capture_status: str | None = None
    seller_id: str = SELLER_ID
    product_id: str = PRODUCT_ID
    product_name: str = PRODUCT_NAME
    currency: str = CURRENCY
    amount: str = AMOUNT
    source: str = 'SIMULATED_SANDBOX'

    def __post_init__(self):
        if type(self.order_id) is not str or not re.fullmatch(r'[A-Z0-9_-]{1,64}', self.order_id):
            raise ValueError('Invalid synthetic order identifier')
        classify_order_reply(self.status)
        fixed = {'seller_id': SELLER_ID, 'product_id': PRODUCT_ID,
                 'product_name': PRODUCT_NAME, 'currency': CURRENCY,
                 'amount': AMOUNT, 'source': 'SIMULATED_SANDBOX'}
        if any(type(getattr(self, key)) is not str or getattr(self, key) != value for key, value in fixed.items()):
            raise ValueError('Facts must match the fixed synthetic seller/product scope')
        if self.capture_status is not None and (type(self.capture_status) is not str or self.capture_status not in ('PENDING', 'COMPLETED')):
            raise ValueError('Unsupported mock capture status')
        if self.status != 'COMPLETED' and self.capture_status is not None:
            raise ValueError('Capture facts inconsistent with mock order status')

    @property
    def category(self):
        return classify_order_reply(self.status, capture_status=self.capture_status)

    def model_context(self):
        return {'source': self.source, 'order_id': self.order_id,
                'status': self.status, 'capture_status': self.capture_status,
                'category': self.category, 'seller_id': self.seller_id,
                'product': self.product_name, 'currency': self.currency,
                'amount': self.amount, 'quantity': 1}
