"""Versioned rate schedules for billing.

Each charge family (handling, demurrage, surcharges) keeps a timeline of rate
versions. A version is effective over the half-open interval
[effective_from, effective_to), and settlement always resolves the version
that was effective when the business event occurred. As a consequence:

- a new version only affects business occurring at or after its effective
  start;
- replaying a historical invoice resolves the same versions and therefore
  reproduces the original amounts;
- overlapping version intervals, currency mismatches and missing rules are
  rejected with an explanatory error.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Generic, Iterable, Protocol, TypeVar
from terminalweave.core.clock import UTC, ensure_utc, iso_z
from terminalweave.core.errors import ConflictError, NotFoundError, StateTransitionError, ValidationError
from terminalweave.core.money import Money
from terminalweave.billing.demurrage import BusinessCalendar, DemurrageCalculator, DemurrageLine, DemurrageTariff
from terminalweave.billing.invoice import InvoiceLine
from terminalweave.billing.tariffs import TariffBook

class RatePayload(Protocol):
    @property
    def currency(self) -> str: ...

T = TypeVar('T', bound=RatePayload)
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)

def _normalize_currency(currency: str) -> str:
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValidationError('currency must be a 3-letter code')
    return code

def _span(version: 'RateVersion') -> str:
    end = iso_z(version.effective_to) if version.effective_to is not None else 'open'
    return f'[{iso_z(version.effective_from)}, {end})'

@dataclass(frozen=True)
class RateVersion(Generic[T]):
    version_id: str
    effective_from: datetime
    effective_to: datetime | None
    payload: T

    def __post_init__(self) -> None:
        if not self.version_id.strip():
            raise ValidationError('version id cannot be empty')
        start = ensure_utc(self.effective_from)
        object.__setattr__(self, 'effective_from', start)
        if self.effective_to is not None:
            end = ensure_utc(self.effective_to)
            if end <= start:
                raise ValidationError('version effective_to must be after effective_from')
            object.__setattr__(self, 'effective_to', end)

    def covers(self, at: datetime) -> bool:
        point = ensure_utc(at)
        return self.effective_from <= point and (self.effective_to is None or point < self.effective_to)

    def overlaps(self, other: 'RateVersion') -> bool:
        self_end = self.effective_to or _FAR_FUTURE
        other_end = other.effective_to or _FAR_FUTURE
        return self.effective_from < other_end and other.effective_from < self_end

class RateSchedule(Generic[T]):
    """Timeline of non-overlapping rate versions for one charge family.

    At most one version may be open-ended, so any point in time resolves to
    zero or one version. Resolution depends only on the business time, which
    keeps replays of historical invoices deterministic.
    """

    def __init__(self, name: str, currency: str):
        if not name.strip():
            raise ValidationError('schedule name cannot be empty')
        self.name = name
        self.currency = _normalize_currency(currency)
        self._versions: dict[str, RateVersion[T]] = {}

    def add(self, version: RateVersion[T]) -> None:
        self._check_addable(version)
        self._versions[version.version_id] = version

    def supersede(self, version: RateVersion[T]) -> None:
        """Close the open-ended version at the new version's start, then add it."""
        open_version = self._open_version()
        if open_version is not None and version.effective_from <= open_version.effective_from:
            raise ValidationError(
                f"new version '{version.version_id}' must start after open version "
                f"'{open_version.version_id}' in schedule '{self.name}'")
        self._check_addable(version, ignore_id=open_version.version_id if open_version else None)
        if open_version is not None:
            self.close(open_version.version_id, version.effective_from)
        self.add(version)

    def close(self, version_id: str, effective_to: datetime) -> None:
        version = self._versions.get(version_id)
        if version is None:
            raise NotFoundError(f"no rate version '{version_id}' in schedule '{self.name}'")
        if version.effective_to is not None:
            raise StateTransitionError(f"rate version '{version_id}' is already closed")
        # closing only shrinks coverage, so it cannot create an overlap
        self._versions[version_id] = RateVersion(version_id, version.effective_from, effective_to, version.payload)

    def resolve(self, at: datetime) -> RateVersion[T]:
        point = ensure_utc(at)
        for version in self._versions.values():
            if version.covers(point):
                return version
        raise NotFoundError(f"no applicable rate version in schedule '{self.name}' at {iso_z(point)}")

    def versions(self) -> list[RateVersion[T]]:
        return sorted(self._versions.values(), key=lambda v: (v.effective_from, v.version_id))

    def _open_version(self) -> RateVersion[T] | None:
        for version in self._versions.values():
            if version.effective_to is None:
                return version
        return None

    def _check_addable(self, version: RateVersion[T], ignore_id: str | None=None) -> None:
        if version.version_id in self._versions:
            raise ConflictError(f"duplicate rate version id '{version.version_id}' in schedule '{self.name}'")
        payload_currency = version.payload.currency
        if payload_currency != self.currency:
            raise ValidationError(
                f"currency mismatch: schedule '{self.name}' is {self.currency} "
                f"but version '{version.version_id}' is {payload_currency}")
        for existing in self._versions.values():
            if existing.version_id == ignore_id:
                continue
            if existing.overlaps(version):
                raise ConflictError(
                    f"version interval overlap: '{version.version_id}' {_span(version)} overlaps "
                    f"'{existing.version_id}' {_span(existing)} in schedule '{self.name}'")

@dataclass(frozen=True)
class SurchargeItem:
    code: str
    description: str
    amount: Money

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValidationError('surcharge code cannot be empty')

class SurchargeTable:
    """Versioned payload holding the additional-fee rules for one period."""

    def __init__(self, items: list[SurchargeItem]):
        if not items:
            raise ValidationError('surcharge table cannot be empty')
        self._map = {item.code: item for item in items}
        if len(self._map) != len(items):
            raise ValidationError('duplicate surcharge code')
        currencies = {item.amount.currency for item in items}
        if len(currencies) != 1:
            raise ValidationError('surcharge table has mixed currencies')
        self._currency = currencies.pop()

    @property
    def currency(self) -> str:
        return self._currency

    def item(self, code: str) -> SurchargeItem:
        try:
            return self._map[code]
        except KeyError:
            raise NotFoundError(f"no surcharge rule for code '{code}'") from None

    def quote(self, code: str) -> Money:
        return self.item(code).amount

@dataclass(frozen=True)
class HandlingEvent:
    operation: str
    container_size: int
    occurred_at: datetime
    count: int = 1
    hazardous: bool = False
    reefer: bool = False

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValidationError('count cannot be negative')
        object.__setattr__(self, 'occurred_at', ensure_utc(self.occurred_at))

@dataclass(frozen=True)
class DemurrageEvent:
    discharge_at: datetime
    gate_out_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'discharge_at', ensure_utc(self.discharge_at))
        object.__setattr__(self, 'gate_out_at', ensure_utc(self.gate_out_at))

@dataclass(frozen=True)
class SurchargeEvent:
    code: str
    occurred_at: datetime
    quantity: Decimal = Decimal('1')

    def __post_init__(self) -> None:
        if Decimal(str(self.quantity)) < 0:
            raise ValidationError('quantity cannot be negative')
        object.__setattr__(self, 'occurred_at', ensure_utc(self.occurred_at))

ChargeEvent = HandlingEvent | DemurrageEvent | SurchargeEvent

class RateEngine:
    """Settles handling, demurrage and surcharge charges using the rate
    version effective at the time each business event occurred."""

    def __init__(self, handling: RateSchedule[TariffBook], demurrage: RateSchedule[DemurrageTariff],
                 surcharges: RateSchedule[SurchargeTable], calendar: BusinessCalendar | None=None):
        self.handling = handling
        self.demurrage = demurrage
        self.surcharges = surcharges
        self.calendar = calendar if calendar is not None else BusinessCalendar()

    def handling_quote(self, operation: str, size: int, occurred_at: datetime, count: int=1,
                       currency: str | None=None, **flags) -> Money:
        version = self.handling.resolve(occurred_at)
        self._require_currency(self.handling, currency)
        try:
            return version.payload.quote(operation, size, count, **flags)
        except KeyError:
            raise NotFoundError(
                f"no handling tariff for operation '{operation}' size {size} "
                f"in rate version '{version.version_id}'") from None

    def demurrage_lines(self, discharge_at: datetime, gate_out_at: datetime,
                        currency: str | None=None) -> list[DemurrageLine]:
        version = self.demurrage.resolve(discharge_at)
        self._require_currency(self.demurrage, currency)
        calculator = DemurrageCalculator(self.calendar, version.payload)
        return calculator.calculate(discharge_at, gate_out_at)

    def demurrage_total(self, discharge_at: datetime, gate_out_at: datetime,
                        currency: str | None=None) -> Money:
        lines = self.demurrage_lines(discharge_at, gate_out_at, currency)
        total = Money.zero(self.demurrage.currency)
        for line in lines:
            total = total + line.amount
        return total

    def surcharge_quote(self, code: str, occurred_at: datetime, quantity: Decimal | int=1,
                        currency: str | None=None) -> Money:
        version = self.surcharges.resolve(occurred_at)
        self._require_currency(self.surcharges, currency)
        amount = Decimal(str(quantity))
        if amount < 0:
            raise ValidationError('quantity cannot be negative')
        return version.payload.quote(code).scale(amount)

    def settle(self, events: Iterable[ChargeEvent], currency: str) -> list[InvoiceLine]:
        """Produce invoice lines for business events. Replaying the same events
        yields identical lines because resolution is pinned to each event's time."""
        requested = _normalize_currency(currency)
        lines: list[InvoiceLine] = []
        for event in events:
            if isinstance(event, HandlingEvent):
                lines.append(self._handling_line(event, requested))
            elif isinstance(event, DemurrageEvent):
                lines.extend(self._demurrage_lines(event, requested))
            elif isinstance(event, SurchargeEvent):
                lines.append(self._surcharge_line(event, requested))
            else:
                raise ValidationError(f'unsupported charge event: {type(event).__name__}')
        return lines

    def _handling_line(self, event: HandlingEvent, currency: str) -> InvoiceLine:
        version = self.handling.resolve(event.occurred_at)
        self._require_currency(self.handling, currency)
        try:
            unit = version.payload.quote(event.operation, event.container_size, 1,
                                         hazardous=event.hazardous, reefer=event.reefer)
        except KeyError:
            raise NotFoundError(
                f"no handling tariff for operation '{event.operation}' size {event.container_size} "
                f"in rate version '{version.version_id}'") from None
        return InvoiceLine(
            code=f'HANDLING-{event.operation}-{event.container_size}',
            description=f'handling {event.operation} {event.container_size}ft (rate version {version.version_id})',
            quantity=Decimal(event.count),
            unit_price=unit)

    def _demurrage_lines(self, event: DemurrageEvent, currency: str) -> list[InvoiceLine]:
        version = self.demurrage.resolve(event.discharge_at)
        self._require_currency(self.demurrage, currency)
        calculator = DemurrageCalculator(self.calendar, version.payload)
        return [
            InvoiceLine(
                code='DEMURRAGE',
                description=f'demurrage {line.day.isoformat()} charge day {line.charge_day_number} '
                            f'(rate version {version.version_id})',
                quantity=Decimal(1),
                unit_price=line.amount)
            for line in calculator.calculate(event.discharge_at, event.gate_out_at)
        ]

    def _surcharge_line(self, event: SurchargeEvent, currency: str) -> InvoiceLine:
        version = self.surcharges.resolve(event.occurred_at)
        self._require_currency(self.surcharges, currency)
        item = version.payload.item(event.code)
        return InvoiceLine(
            code=f'SURCHARGE-{item.code}',
            description=f'{item.description} (rate version {version.version_id})',
            quantity=Decimal(str(event.quantity)),
            unit_price=item.amount)

    @staticmethod
    def _require_currency(schedule: RateSchedule, currency: str | None) -> None:
        if currency is None:
            return
        requested = _normalize_currency(currency)
        if requested != schedule.currency:
            raise ValidationError(
                f"currency mismatch: settlement requested in {requested} but schedule "
                f"'{schedule.name}' rates are {schedule.currency}")
