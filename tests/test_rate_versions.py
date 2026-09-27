from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
import pytest

from terminalweave.billing.demurrage import (
    BusinessCalendar,
    DemurrageTariff,
    TariffBand,
)
from terminalweave.billing.invoice import Invoice, InvoiceLine
from terminalweave.billing.rate_versions import RateVersion, RateVersionBook
from terminalweave.billing.settlement import (
    DemurrageCharge,
    HandlingCharge,
    SettlementRequest,
    SettlementService,
    SurchargeCharge,
    handling_key,
)
from terminalweave.billing.surcharges import SurchargeRule
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.errors import ConflictError, NotFoundError, ValidationError
from terminalweave.core.money import Money

UTC = timezone.utc
JAN1 = datetime(2026, 1, 1, tzinfo=UTC)
FEB1 = datetime(2026, 2, 1, tzinfo=UTC)
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)
MAY1 = datetime(2026, 5, 1, tzinfo=UTC)


def money(amount, currency='USD'):
    return Money(Decimal(str(amount)), currency)


def handling_book():
    book = RateVersionBook[HandlingTariff]('handling')
    book.add_version(RateVersion('HV1', handling_key('LOAD', 20),
                                 HandlingTariff('LOAD', 20, money(100)), 'USD', JAN1, FEB1))
    book.add_version(RateVersion('HV2', handling_key('LOAD', 20),
                                 HandlingTariff('LOAD', 20, money(120), hazardous_surcharge=money(20)),
                                 'USD', FEB1, None))
    return book


def demurrage_tariff(rate, currency='USD'):
    return DemurrageTariff(0, (TariffBand(1, None, money(rate, currency)),))


def demurrage_book():
    book = RateVersionBook[DemurrageTariff]('demurrage')
    book.add_version(RateVersion('DV1', 'default', demurrage_tariff(10), 'USD', JAN1, MAR1))
    book.add_version(RateVersion('DV2', 'default', demurrage_tariff(15), 'USD', MAR1, None))
    return book


def surcharge_book():
    book = RateVersionBook[SurchargeRule]('surcharge')
    book.add_version(RateVersion('SV1', 'BAF', SurchargeRule('BAF', money(8)), 'USD', JAN1, APR1))
    book.add_version(RateVersion('SV2', 'BAF', SurchargeRule('BAF', money(12)), 'USD', APR1, None))
    return book


def service(handling=None, demurrage=None, surcharges=None, calendar=None):
    return SettlementService(
        handling or handling_book(),
        demurrage or demurrage_book(),
        surcharges or surcharge_book(),
        calendar or BusinessCalendar(),
    )


def test_handling_uses_rule_effective_when_business_occurred():
    svc = service()
    old = svc.settle(SettlementRequest(
        'INV-1', 'CUST', JAN1, 'USD',
        handling=[HandlingCharge('LOAD', 20, datetime(2026, 1, 15, tzinfo=UTC))],
    ))
    new = svc.settle(SettlementRequest(
        'INV-2', 'CUST', FEB1, 'USD',
        handling=[HandlingCharge('LOAD', 20, datetime(2026, 2, 15, tzinfo=UTC))],
    ))
    assert old.lines[0].unit_price.amount == Decimal('100.00')
    assert old.lines[0].rate_version_id == 'HV1'
    assert new.lines[0].unit_price.amount == Decimal('120.00')
    assert new.lines[0].rate_version_id == 'HV2'


def test_effective_boundary_is_inclusive_from_exclusive_to():
    svc = service()
    boundary = svc.settle(SettlementRequest(
        'INV-B', 'CUST', FEB1, 'USD',
        handling=[HandlingCharge('LOAD', 20, FEB1)],
    ))
    assert boundary.lines[0].rate_version_id == 'HV2'


def test_overlapping_versions_are_rejected_with_reason():
    book = handling_book()
    overlap = RateVersion('HV3', handling_key('LOAD', 20),
                          HandlingTariff('LOAD', 20, money(110)), 'USD',
                          datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 1, 20, tzinfo=UTC))
    with pytest.raises(ConflictError) as exc:
        book.add_version(overlap)
    message = str(exc.value)
    assert 'overlap' in message and 'HV1' in message and 'HV3' in message

    # Two open-ended versions for the same key also overlap.
    with pytest.raises(ConflictError):
        book.add_version(RateVersion('HV4', handling_key('LOAD', 20),
                                     HandlingTariff('LOAD', 20, money(999)), 'USD',
                                     datetime(2027, 1, 1, tzinfo=UTC), None))


def test_adjacent_versions_are_allowed():
    book = handling_book()
    book.add_version(RateVersion('HV5', handling_key('LOAD', 20),
                                 HandlingTariff('LOAD', 20, money(90)), 'USD',
                                 datetime(2025, 12, 1, tzinfo=UTC), JAN1))
    assert book.resolve(handling_key('LOAD', 20), datetime(2025, 12, 15, tzinfo=UTC)).version_id == 'HV5'


def test_missing_rule_is_rejected_with_reason():
    svc = service()
    with pytest.raises(NotFoundError) as exc:
        svc.settle(SettlementRequest(
            'INV-3', 'CUST', JAN1, 'USD',
            handling=[HandlingCharge('DISCHARGE', 40, JAN1)],
        ))
    assert "no handling rule registered for key 'DISCHARGE|40'" in str(exc.value)


def test_gap_in_timeline_is_rejected_and_explained():
    book = RateVersionBook[SurchargeRule]('surcharge')
    book.add_version(RateVersion('P1', 'PSS', SurchargeRule('PSS', money(5)), 'USD',
                                 JAN1, FEB1))
    book.add_version(RateVersion('P2', 'PSS', SurchargeRule('PSS', money(7)), 'USD',
                                 MAR1, None))
    svc = service(surcharges=book)
    with pytest.raises(NotFoundError) as exc:
        svc.settle(SettlementRequest(
            'INV-4', 'CUST', FEB1, 'USD',
            surcharges=[SurchargeCharge('PSS', datetime(2026, 2, 15, tzinfo=UTC))],
        ))
    message = str(exc.value)
    assert 'gap' in message and 'P1' in message and 'P2' in message


def test_currency_mismatch_rejects_settlement():
    book = RateVersionBook[DemurrageTariff]('demurrage')
    book.add_version(RateVersion('EV1', 'default', demurrage_tariff(10, 'EUR'), 'EUR', JAN1, None))
    svc = service(demurrage=book)
    with pytest.raises(ValidationError) as exc:
        svc.settle(SettlementRequest(
            'INV-5', 'CUST', JAN1, 'USD',
            demurrage=[DemurrageCharge(JAN1, JAN1 + timedelta(days=2))],
        ))
    message = str(exc.value)
    assert 'EUR' in message and 'USD' in message and 'EV1' in message


def test_demurrage_governed_by_tariff_at_discharge_across_version_change():
    svc = service(calendar=BusinessCalendar(weekends_free=True))
    # Stay crosses the Mar 1 rate change; discharge on Feb 25 keeps DV1.
    stay = svc.settle(SettlementRequest(
        'INV-6', 'CUST', datetime(2026, 2, 25, tzinfo=UTC), 'USD',
        demurrage=[DemurrageCharge(datetime(2026, 2, 25, tzinfo=UTC),
                                   datetime(2026, 3, 2, tzinfo=UTC))],
    ))
    # Chargeable days: Wed Feb25, Thu Feb26, Fri Feb27, Mon Mar2 (weekend skipped).
    assert len(stay.lines) == 4
    assert {line.rate_version_id for line in stay.lines} == {'DV1'}
    assert stay.subtotal().amount == Decimal('40.00')

    after = svc.settle(SettlementRequest(
        'INV-7', 'CUST', MAR1, 'USD',
        demurrage=[DemurrageCharge(datetime(2026, 3, 2, tzinfo=UTC),
                                   datetime(2026, 3, 3, tzinfo=UTC))],
    ))
    assert {line.rate_version_id for line in after.lines} == {'DV2'}
    assert after.subtotal().amount == Decimal('30.00')


def test_surcharge_versions_apply_by_occurrence():
    svc = service()
    before = svc.settle(SettlementRequest(
        'INV-8', 'CUST', MAR1, 'USD',
        surcharges=[SurchargeCharge('BAF', datetime(2026, 3, 15, tzinfo=UTC), count=2)],
    ))
    after = svc.settle(SettlementRequest(
        'INV-9', 'CUST', APR1, 'USD',
        surcharges=[SurchargeCharge('BAF', APR1, count=2)],
    ))
    assert before.lines[0].unit_price.amount == Decimal('8.00')
    assert before.lines[0].rate_version_id == 'SV1'
    assert before.subtotal().amount == Decimal('16.00')
    assert after.lines[0].unit_price.amount == Decimal('12.00')
    assert after.lines[0].rate_version_id == 'SV2'


def test_historical_invoice_replays_to_identical_result_after_new_version():
    handling = RateVersionBook[HandlingTariff]('handling')
    handling.add_version(RateVersion('HV1', handling_key('LOAD', 20),
                                     HandlingTariff('LOAD', 20, money(100)), 'USD', JAN1, None))
    svc = service(handling=handling)
    request = SettlementRequest(
        'INV-OLD', 'CUST', JAN1, 'USD',
        handling=[HandlingCharge('LOAD', 20, datetime(2026, 1, 15, tzinfo=UTC))],
        demurrage=[DemurrageCharge(datetime(2026, 2, 25, tzinfo=UTC),
                                   datetime(2026, 3, 2, tzinfo=UTC))],
        surcharges=[SurchargeCharge('BAF', datetime(2026, 1, 20, tzinfo=UTC))],
    )
    original = svc.settle(request)
    original_amount = original.subtotal().amount
    original_versions = [line.rate_version_id for line in original.lines]

    # A new tariff cycle begins in July; January business must be untouched.
    handling.retire_open_version(handling_key('LOAD', 20), datetime(2026, 7, 1, tzinfo=UTC))
    handling.add_version(RateVersion('HV9', handling_key('LOAD', 20),
                                     HandlingTariff('LOAD', 20, money(200)), 'USD',
                                     datetime(2026, 7, 1, tzinfo=UTC), None))
    svc.replay(original, request)
    assert original.subtotal().amount == original_amount
    assert [line.rate_version_id for line in original.lines] == original_versions

    # And July business really does pick up the new version.
    july = svc.settle(SettlementRequest(
        'INV-JUL', 'CUST', datetime(2026, 7, 1, tzinfo=UTC), 'USD',
        handling=[HandlingCharge('LOAD', 20, datetime(2026, 7, 1, tzinfo=UTC))],
    ))
    assert july.lines[0].rate_version_id == 'HV9'


def test_replay_detects_tampered_historical_invoice():
    svc = service()
    request = SettlementRequest(
        'INV-10', 'CUST', JAN1, 'USD',
        handling=[HandlingCharge('LOAD', 20, datetime(2026, 1, 15, tzinfo=UTC))],
    )
    tampered = Invoice('INV-10', 'CUST', 'USD')
    tampered.add(InvoiceLine('HANDLING:LOAD', 'LOAD 20ft', Decimal('1'),
                             money(999), rate_version_id='HV1'))
    with pytest.raises(ConflictError) as exc:
        svc.replay(tampered, request)
    assert 'changed on replay' in str(exc.value)


def test_replay_rejects_wrong_request_identity():
    svc = service()
    request = SettlementRequest(
        'INV-11', 'CUST', JAN1, 'USD',
        handling=[HandlingCharge('LOAD', 20, JAN1)],
    )
    invoice = svc.settle(request)
    with pytest.raises(ConflictError):
        svc.replay(invoice, SettlementRequest(
            'INV-OTHER', 'CUST', JAN1, 'USD',
            handling=[HandlingCharge('LOAD', 20, JAN1)],
        ))


def test_mixed_currency_lines_in_one_settlement_are_rejected():
    book = RateVersionBook[SurchargeRule]('surcharge')
    book.add_version(RateVersion('XV1', 'BAF', SurchargeRule('BAF', money(8, 'EUR')), 'EUR', JAN1, None))
    svc = service(surcharges=book)
    with pytest.raises(ValidationError):
        svc.settle(SettlementRequest(
            'INV-12', 'CUST', JAN1, 'USD',
            handling=[HandlingCharge('LOAD', 20, JAN1)],
            surcharges=[SurchargeCharge('BAF', JAN1)],
        ))


def test_hazmat_flag_priced_through_versioned_tariff():
    svc = service()
    invoice = svc.settle(SettlementRequest(
        'INV-13', 'CUST', FEB1, 'USD',
        handling=[HandlingCharge('LOAD', 20, FEB1, hazardous=True)],
    ))
    assert invoice.subtotal().amount == Decimal('140.00')
    assert invoice.lines[0].rate_version_id == 'HV2'


def test_demurrage_tariff_rejects_mixed_currency_bands():
    with pytest.raises(ValidationError):
        DemurrageTariff(0, (
            TariffBand(1, 3, money(10)),
            TariffBand(4, None, money(15, 'EUR')),
        ))


def test_rate_version_rejects_invalid_interval_and_naive_datetime():
    with pytest.raises(ValidationError):
        RateVersion('X', 'k', SurchargeRule('BAF', money(1)), 'USD', FEB1, JAN1)
    with pytest.raises(ValidationError):
        RateVersion('X', 'k', SurchargeRule('BAF', money(1)), 'USD',
                    datetime(2026, 1, 1), None)


def test_retire_open_version_preserves_history():
    book = handling_book()
    july = datetime(2026, 7, 1, tzinfo=UTC)
    retired = book.retire_open_version(handling_key('LOAD', 20), july)
    assert retired.effective_to == july
    # Historical business still resolves to the retired version.
    assert book.resolve(handling_key('LOAD', 20), datetime(2026, 6, 30, tzinfo=UTC)).version_id == 'HV2'
    with pytest.raises(NotFoundError):
        book.resolve(handling_key('LOAD', 20), datetime(2026, 7, 2, tzinfo=UTC))
    # Retiring twice or backdating the succession is refused.
    with pytest.raises(NotFoundError):
        book.retire_open_version(handling_key('LOAD', 20), datetime(2026, 8, 1, tzinfo=UTC))
    book.add_version(RateVersion('HV9', handling_key('LOAD', 20),
                                 HandlingTariff('LOAD', 20, money(200)), 'USD', july, None))
    with pytest.raises(ValidationError):
        book.retire_open_version(handling_key('LOAD', 20), FEB1)


def test_duplicate_version_id_for_key_is_rejected():
    book = handling_book()
    with pytest.raises(ConflictError):
        book.add_version(RateVersion('HV2', handling_key('LOAD', 40),
                                     HandlingTariff('LOAD', 40, money(300)), 'USD', JAN1, None))
