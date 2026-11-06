from django.conf import settings

from .demo import DemoGateway
from .paypal import PayPalGateway


def get_payment_gateway():
    if getattr(settings, 'DEMO_MODE', False) and getattr(
            settings, 'DEMO_SIMULATE_PAYMENTS', True):
        return DemoGateway()
    return PayPalGateway()
