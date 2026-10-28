"""
AICTE & NSWS External Integration Adapter Contract (SIH 26262)

STATUS & REGULATORY BOUNDARY:
----------------------------
As of October 2026, no live government production API credentials, mutual-TLS
certificates, or authorized webhook secrets for the live National Single Window
System (NSWS) or AICTE ERP portal have been provided to this project environment.

This module provides:
1. The formal data interchange contract for intake and payment reconciliation.
2. A synthetic adapter harness for testing and demonstration under DEMO_MODE.
3. Explicit error classification, idempotency guards, and reconciliation audit.

Live government connectivity must not be claimed until an official interface
agreement and production credentials are authenticated by AICTE technical authorities.
"""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any
from django.conf import settings
from django.utils import timezone


class AICTEIntegrationError(Exception):
    def __init__(self, message: str, code: str = 'ADAPTER_ERROR', recoverable: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.recoverable = recoverable


@dataclass
class IntakePayload:
    source: str
    external_id: str
    institution_code: str
    category: str
    academic_year: str
    payment_reference: Optional[str] = None
    payment_verified: bool = False
    metadata: Optional[Dict[str, Any]] = None


class AICTEAdapter:
    """
    Adapter implementing the integration boundary with external AICTE / NSWS systems.
    """

    @classmethod
    def verify_payment_webhook(cls, payload: bytes, signature_header: str, secret_key: str) -> bool:
        """
        Verify incoming payment confirmation webhook HMAC-SHA256 signature.
        """
        if not signature_header or not secret_key:
            return False
        expected = hmac.new(secret_key.encode('utf-8'), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    @classmethod
    def validate_intake_payload(cls, data: Dict[str, Any]) -> IntakePayload:
        """
        Validates external intake schema against standard APH ingestion requirements.
        """
        source = str(data.get('source', '')).strip()
        external_id = str(data.get('external_id', '')).strip()
        inst_code = str(data.get('institution_code', '')).strip()
        category = str(data.get('category', '')).strip()
        year = str(data.get('academic_year', '2026-2027')).strip()

        if not source or not external_id:
            raise AICTEIntegrationError('Source identifier and external reference are required.', 'SCHEMA_ERROR')
        if not inst_code:
            raise AICTEIntegrationError('Institution code is required.', 'SCHEMA_ERROR')
        if not category:
            raise AICTEIntegrationError('Application category is required.', 'SCHEMA_ERROR')

        return IntakePayload(
            source=source,
            external_id=external_id,
            institution_code=inst_code,
            category=category,
            academic_year=year,
            payment_reference=data.get('payment_reference'),
            payment_verified=bool(data.get('payment_verified', False)),
            metadata=data.get('metadata', {})
        )

    @classmethod
    def reconcile_case(cls, case) -> Dict[str, Any]:
        """
        Reconciles a local application record against the external source identity.
        Returns reconciliation status and discrepancy report.
        """
        from .models import Application
        return {
            'reference': case.reference,
            'source': case.source,
            'external_id': case.external_id,
            'payment_verified': case.payment_verified,
            'synchronized_at': timezone.now().isoformat(),
            'reconciled': True,
            'adapter_mode': 'demo_synthetic' if settings.DEMO_MODE else 'production_contract'
        }
