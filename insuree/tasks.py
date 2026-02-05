import logging

from celery import shared_task
from django.apps import apps
from django_opensearch_dsl.registries import registry

logger = logging.getLogger(__name__)


def Enrollment_etl():
    from insuree.ETL import ETLTotalCBHIMemberAndBeneficiary , ETLTotalMoneyCollected , ETLEnrollmentRate

    enrollmentETL = ETLEnrollmentRate.EnrollmentRateETL()
    enrollmentETL.process()
    totalMoneyCollectedETL = ETLTotalMoneyCollected.TotalMoneyCollectedETL()
    totalMoneyCollectedETL.process()
    totalCBHIMemberAndBeneficiaryETL = ETLTotalCBHIMemberAndBeneficiary.TotalCBHIMemberAndBeneficiaryETL()
    totalCBHIMemberAndBeneficiaryETL.process()
    print("EnrollmentRate ETL process executed.")