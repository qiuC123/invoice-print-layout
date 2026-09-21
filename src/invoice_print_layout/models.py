from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path


class DocumentKind(StrEnum):
    TRIP = "trip"
    INVOICE = "invoice"


class RideProvider(StrEnum):
    DIDI = "滴滴"
    CAOCAO = "曹操"
    XIANGDAO = "享道"
    UNKNOWN = "网约车"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class PdfDocument:
    path: Path
    kind: DocumentKind
    provider: RideProvider
    amount: Decimal
    sha256: str
    page_count: int
    invoice_date: date | None = None
    invoice_number: str | None = None


@dataclass(frozen=True)
class TicketGroup:
    trip: PdfDocument
    invoice: PdfDocument

    @property
    def provider(self) -> RideProvider:
        if self.trip.provider is not RideProvider.UNKNOWN:
            return self.trip.provider
        return self.invoice.provider


@dataclass(frozen=True)
class PairingIssue:
    paths: tuple[Path, ...]
    reason: str


@dataclass(frozen=True)
class ProcessingOutcome:
    status: OutcomeStatus
    label: str
    message: str


@dataclass
class BatchSummary:
    outcomes: list[ProcessingOutcome] = field(default_factory=list)

    def add(self, status: OutcomeStatus, label: str, message: str) -> None:
        self.outcomes.append(ProcessingOutcome(status, label, message))

    def count(self, status: OutcomeStatus) -> int:
        return sum(item.status is status for item in self.outcomes)

    @property
    def success_count(self) -> int:
        return self.count(OutcomeStatus.SUCCESS)

    @property
    def review_count(self) -> int:
        return self.count(OutcomeStatus.NEEDS_REVIEW)

    @property
    def duplicate_count(self) -> int:
        return self.count(OutcomeStatus.DUPLICATE)
