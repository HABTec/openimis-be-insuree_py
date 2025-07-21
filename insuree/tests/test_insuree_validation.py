from django.test import TestCase

from insuree.apps import InsureeConfig
from insuree.services import validate_insuree_number


def fail1(x):
    if x == "fail1":
        return ["fail1"]
    else:
        return []


class InsureeValidationTest(TestCase):
    pass
