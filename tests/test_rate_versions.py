from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from terminalweave.billing.demurrage import BusinessCalendar, DemurrageTariff, TariffBand
from terminalweave.billing.invoice import Invoice
from terminalweave.billing.rate_versions import (
    DemurrageEvent,
    HandlingEvent,
    RateEngine,
    RateSchedule,
    RateVersion,
    SurchargeEvent,
    SurchargeItem,
    SurchargeTable,
)
from terminalweave.billing.tariffs import HandlingTariff, TariffBook
from terminalweave.core.errors import ConflictError, NotFoundError, ValidationError
from terminalweave.core.money import Money

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 3, 1, tzinfo=UTC)


def handling_book(price, currency='USD'):
    return TariffBook([
        HandlingTariff('DISCHARGE', 20, Money(Decimal(price), currency)),
        HandlingTariff('LOAD', 40, Money(Decimal(price), currency)),
    ])


def demurrage_tariff(rate, currency='USD'):
    return DemurrageTariff(0, (TariffBand(1, None, Money(Decimal(rate), currency)),))


def surcharge_table(fee, currency='USD'):
    return SurchargeTable([SurchargeItem('DOC', 'documentation fee', Money(Decimal(fee), currency))])


def build_engine():
    handling = RateSchedule('handling', 'USD')
    handling.add(RateVersion('h-2026a', T0, None, handling_book('100')))
    demurrage = RateSchedule('demurrage', 'USD')
    demurrage.add(RateVersion('d-2026a', T0, None, demurrage_tariff('10')))
    surcharges = RateSchedule('surcharges', 'USD')
    surcharges.add(RateVersion('s-2026a', T0, None, surcharge_table('25')))
    engine = RateEngine(handling, demurrage, surcharges, BusinessCalendar(weekends_free=False))
    return engine, handling, demurrage, surcharges


def test_quote_uses_version_effective_at_business_time():
    engine, handling, _, _ = build_engine()
    handling.supersede(RateVersion('h-2026b', T1, None, handling_book('150')))
    before = engine.handling_quote('DISCHARGE', 20, T1 - timedelta(days=1))
    after = engine.handling_quote('DISCHARGE', 20, T1)
    assert before.amount == Decimal('100.00')
    assert after.amount == Decimal('150.00')


def test_replay_reproduces_original_invoice_after_new_versions():
    engine, handling, demurrage, surcharges = build_engine()
    occurred = datetime(2026, 2, 2, tzinfo=UTC)
    events = [
        HandlingEvent('DISCHARGE', 20, occurred, count=2),
        DemurrageEvent(datetime(2026, 2, 2, tzinfo=UTC), datetime(2026, 2, 5, tzinfo=UTC)),
        SurchargeEvent('DOC', occurred, quantity=2),
    ]
    first = engine.settle(events, 'USD')
    invoice = Invoice('INV-1', 'CUST', currency='USD')
    for line in first:
        invoice.add(line)
    original_total = invoice.total()
    assert original_total.amount == Decimal('290.00')

    # new versions take effect after the settled business occurred
    handling.supersede(RateVersion('h-2026b', T1, None, handling_book('150')))
    demurrage.supersede(RateVersion('d-2026b', T1, None, demurrage_tariff('40')))
    surcharges.supersede(RateVersion('s-2026b', T1, None, surcharge_table('30')))

    replay = engine.settle(events, 'USD')
    assert replay == first
    replay_invoice = Invoice('INV-1', 'CUST', currency='USD')
    for line in replay:
        replay_invoice.add(line)
    assert replay_invoice.total() == original_total

    # business occurring after the new versions took effect uses the new rates
    late = engine.settle([HandlingEvent('DISCHARGE', 20, datetime(2026, 3, 2, tzinfo=UTC))], 'USD')
    assert late[0].unit_price.amount == Decimal('150.00')


def test_demurrage_uses_tariff_effective_at_discharge():
    engine, _, demurrage, _ = build_engine()
    demurrage.supersede(RateVersion('d-2026b', T1, None, demurrage_tariff('99')))
    discharge = T1 - timedelta(days=2)
    gate_out = T1 + timedelta(days=2)
    total = engine.demurrage_total(discharge, gate_out)
    # 5 chargeable days, all priced by the version effective at discharge
    assert total.amount == Decimal('50.00')


def test_overlapping_versions_rejected():
    schedule = RateSchedule('handling', 'USD')
    schedule.add(RateVersion('v1', T0, T1, handling_book('100')))
    with pytest.raises(ConflictError, match='overlap'):
        schedule.add(RateVersion('v2', T1 - timedelta(days=1), None, handling_book('150')))


def test_adjacent_versions_accepted():
    schedule = RateSchedule('handling', 'USD')
    schedule.add(RateVersion('v1', T0, T1, handling_book('100')))
    schedule.add(RateVersion('v2', T1, None, handling_book('150')))
    assert schedule.resolve(T0).version_id == 'v1'
    assert schedule.resolve(T1).version_id == 'v2'


def test_currency_mismatch_rejected_at_registration():
    schedule = RateSchedule('handling', 'USD')
    with pytest.raises(ValidationError, match='currency mismatch'):
        schedule.add(RateVersion('v1', T0, None, handling_book('100', currency='EUR')))


def test_currency_mismatch_rejected_at_settlement():
    engine, *_ = build_engine()
    with pytest.raises(ValidationError, match='currency mismatch'):
        engine.settle([HandlingEvent('DISCHARGE', 20, T0)], 'EUR')


def test_mixed_currency_tariff_book_rejected():
    book = TariffBook([
        HandlingTariff('DISCHARGE', 20, Money(Decimal('100'), 'USD')),
        HandlingTariff('LOAD', 40, Money(Decimal('90'), 'EUR')),
    ])
    schedule = RateSchedule('handling', 'USD')
    with pytest.raises(ValidationError, match='mixed currencies'):
        schedule.add(RateVersion('v1', T0, None, book))


def test_missing_version_rejected():
    engine, *_ = build_engine()
    with pytest.raises(NotFoundError, match='no applicable rate version'):
        engine.handling_quote('DISCHARGE', 20, datetime(2025, 1, 1, tzinfo=UTC))


def test_missing_operation_rejected():
    engine, *_ = build_engine()
    with pytest.raises(NotFoundError, match='no handling tariff'):
        engine.handling_quote('SHIFT', 20, T0)


def test_missing_surcharge_code_rejected():
    engine, *_ = build_engine()
    with pytest.raises(NotFoundError, match='no surcharge rule'):
        engine.surcharge_quote('SEAL', T0)


def test_close_open_version_leaves_gap():
    schedule = RateSchedule('handling', 'USD')
    schedule.add(RateVersion('v1', T0, None, handling_book('100')))
    schedule.close('v1', T1)
    assert schedule.resolve(T0).version_id == 'v1'
    with pytest.raises(NotFoundError, match='no applicable rate version'):
        schedule.resolve(T1)


def test_supersede_requires_later_start():
    _, handling, _, _ = build_engine()
    with pytest.raises(ValidationError, match='must start after'):
        handling.supersede(RateVersion('h-2026b', T0, None, handling_book('150')))


def test_naive_business_time_rejected():
    engine, *_ = build_engine()
    with pytest.raises(ValidationError, match='timezone-aware'):
        engine.handling_quote('DISCHARGE', 20, datetime(2026, 2, 1))
