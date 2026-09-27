from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from terminalweave.billing.demurrage import (
    BusinessCalendar,
    DemurrageCalculator,
    DemurrageTariff,
)
from terminalweave.billing.invoice import Invoice, InvoiceLine
from terminalweave.billing.rate_versions import RateVersionBook
from terminalweave.billing.surcharges import SurchargeRule
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ConflictError, ValidationError

DEMURRAGE_KEY = 'default'


def handling_key(operation: str, container_size: int) -> str:
    return f'{operation}|{container_size}'


@dataclass(frozen=True)
class HandlingCharge:
    operation: str
    container_size: int
    occurred_at: datetime
    count: int = 1
    hazardous: bool = False
    reefer: bool = False


@dataclass(frozen=True)
class DemurrageCharge:
    discharge_at: datetime
    gate_out_at: datetime


@dataclass(frozen=True)
class SurchargeCharge:
    code: str
    occurred_at: datetime
    count: int = 1


@dataclass
class SettlementRequest:
    invoice_id: str
    customer_id: str
    occurred_at: datetime
    currency: str = 'USD'
    handling: list[HandlingCharge] = field(default_factory=list)
    demurrage: list[DemurrageCharge] = field(default_factory=list)
    surcharges: list[SurchargeCharge] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'occurred_at', ensure_utc(self.occurred_at))
        object.__setattr__(self, 'currency', self.currency.strip().upper())


class SettlementService:
    """Settles handling, demurrage and surcharge charges against versioned tariffs.

    Every line picks the rule whose version interval contains the moment the
    business actually happened. Versions are immutable once registered, so the
    same request replayed later (with the same books) reproduces the original
    invoice exactly.

    Demurrage for one stay is governed by the tariff in effect at discharge;
    that single version prices every billable day of the stay.
    """

    def __init__(
        self,
        handling_book: RateVersionBook[HandlingTariff],
        demurrage_book: RateVersionBook[DemurrageTariff],
        surcharge_book: RateVersionBook[SurchargeRule],
        calendar: BusinessCalendar,
    ):
        self.handling_book = handling_book
        self.demurrage_book = demurrage_book
        self.surcharge_book = surcharge_book
        self.calendar = calendar

    def settle(self, request: SettlementRequest) -> Invoice:
        invoice = Invoice(request.invoice_id, request.customer_id, request.currency)
        for line in self._lines_for(request):
            invoice.add(line)
        return invoice

    def replay(self, invoice: Invoice, request: SettlementRequest) -> None:
        """Re-run a historical settlement and assert the invoice is unchanged.

        Raises ConflictError when the reproduced lines differ from the ones
        stored on the invoice; returns silently on an exact replay.
        """
        if invoice.invoice_id != request.invoice_id:
            raise ConflictError(
                f"replay request '{request.invoice_id}' does not match invoice '{invoice.invoice_id}'"
            )
        if invoice.customer_id != request.customer_id:
            raise ConflictError('replay request customer does not match invoice')
        if invoice.currency != request.currency:
            raise ConflictError(
                f'replay request currency {request.currency} does not match invoice currency {invoice.currency}'
            )
        expected = self._lines_for(request)
        if len(expected) != len(invoice.lines):
            raise ConflictError(
                f"replay of invoice '{invoice.invoice_id}' produced {len(expected)} lines, "
                f'invoice holds {len(invoice.lines)}'
            )
        for index, (wanted, stored) in enumerate(zip(expected, invoice.lines)):
            mismatch = self._line_diff(wanted, stored)
            if mismatch:
                raise ConflictError(
                    f"line {index + 1} of invoice '{invoice.invoice_id}' changed on replay: {mismatch}"
                )

    @staticmethod
    def _line_diff(wanted: InvoiceLine, stored: InvoiceLine) -> str | None:
        for attribute in ('code', 'quantity', 'rate_version_id'):
            if getattr(wanted, attribute) != getattr(stored, attribute):
                return (
                    f"{attribute} was {getattr(stored, attribute)!r}, "
                    f'rule-in-effect now yields {getattr(wanted, attribute)!r}'
                )
        if wanted.unit_price.currency != stored.unit_price.currency:
            return (
                f"currency was {stored.unit_price.currency}, "
                f'rule-in-effect now yields {wanted.unit_price.currency}'
            )
        if wanted.unit_price.amount != stored.unit_price.amount:
            return (
                f"unit price was {stored.unit_price.amount}, "
                f'rule-in-effect now yields {wanted.unit_price.amount}'
            )
        return None

    def _lines_for(self, request: SettlementRequest) -> list[InvoiceLine]:
        lines: list[InvoiceLine] = []
        for charge in request.handling:
            lines.extend(self._handling_lines(charge, request.currency))
        for charge in request.demurrage:
            lines.extend(self._demurrage_lines(charge, request.currency))
        for charge in request.surcharges:
            lines.extend(self._surcharge_lines(charge, request.currency))
        return lines

    def _handling_lines(self, charge: HandlingCharge, currency: str) -> list[InvoiceLine]:
        if charge.count < 1:
            raise ValidationError('handling charge count must be at least 1')
        key = handling_key(charge.operation, charge.container_size)
        version = self.handling_book.require_currency(key, charge.occurred_at, currency)
        tariff = version.rule
        if not isinstance(tariff, HandlingTariff):
            raise ValidationError(
                f"handling version '{version.version_id}' for key '{key}' is not a handling tariff"
            )
        unit_price = tariff.price(hazardous=charge.hazardous, reefer=charge.reefer)
        flags = []
        if charge.hazardous:
            flags.append('hazmat')
        if charge.reefer:
            flags.append('reefer')
        suffix = f' ({", ".join(flags)})' if flags else ''
        return [
            InvoiceLine(
                code=f'HANDLING:{charge.operation}',
                description=f'{charge.operation} {charge.container_size}ft{suffix}',
                quantity=Decimal(charge.count),
                unit_price=unit_price,
                rate_version_id=version.version_id,
            )
        ]

    def _demurrage_lines(self, charge: DemurrageCharge, currency: str) -> list[InvoiceLine]:
        discharge_at = ensure_utc(charge.discharge_at)
        gate_out_at = ensure_utc(charge.gate_out_at)
        version = self.demurrage_book.require_currency(DEMURRAGE_KEY, discharge_at, currency)
        tariff = version.rule
        if not isinstance(tariff, DemurrageTariff):
            raise ValidationError(
                f"demurrage version '{version.version_id}' is not a demurrage tariff"
            )
        calculator = DemurrageCalculator(self.calendar, tariff)
        lines = []
        for day_line in calculator.calculate(discharge_at, gate_out_at):
            lines.append(
                InvoiceLine(
                    code='DEMURRAGE',
                    description=f'detention day {day_line.charge_day_number} ({day_line.day.isoformat()})',
                    quantity=Decimal('1'),
                    unit_price=day_line.amount,
                    rate_version_id=version.version_id,
                )
            )
        return lines

    def _surcharge_lines(self, charge: SurchargeCharge, currency: str) -> list[InvoiceLine]:
        if charge.count < 1:
            raise ValidationError('surcharge count must be at least 1')
        version = self.surcharge_book.require_currency(charge.code, charge.occurred_at, currency)
        rule = version.rule
        if not isinstance(rule, SurchargeRule):
            raise ValidationError(
                f"surcharge version '{version.version_id}' for code '{charge.code}' is not a surcharge rule"
            )
        return [
            InvoiceLine(
                code=f'SURCHARGE:{charge.code}',
                description=f'surcharge {charge.code}',
                quantity=Decimal(charge.count),
                unit_price=rule.amount,
                rate_version_id=version.version_id,
            )
        ]
