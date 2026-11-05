def classify_order_reply(status, *, capture_status=None):
    """Classify supported synthetic facts; order completion alone is not paid.

    Only an explicitly matching completed capture supports confirmation.
    Unsupported capture/refund states are rejected, never guessed.
    """
    if type(status) is not str or status not in ('CREATED','APPROVED','COMPLETED','VOIDED'):
        raise ValueError('unsupported order status')
    if capture_status is not None:
        if type(capture_status) is not str or capture_status not in ('PENDING','COMPLETED'):
            raise ValueError('unsupported capture status')
        if status != 'COMPLETED':
            raise ValueError('capture inconsistent with synthetic order status')
    if status == 'VOIDED':return 'order_voided'
    if status == 'COMPLETED' and capture_status == 'COMPLETED':return 'payment_confirmed'
    return 'payment_pending'
