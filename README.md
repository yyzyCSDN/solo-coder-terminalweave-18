# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, and `api`.

## Versioned billing rates

`billing.rate_versions` holds append-only `RateVersion` timelines (half-open
`effective_from`..`effective_to` intervals) for handling tariffs
(`billing.tariffs`), demurrage tariffs (`billing.demurrage`) and flat
surcharges (`billing.surcharges`). `billing.settlement.SettlementService`
prices every charge against the rule in effect when the business actually
occurred, records that `rate_version_id` on each invoice line, and rejects
settlement with an explanatory error when intervals overlap, the version
currency does not match the invoice currency, or no rule is in effect
(including timeline gaps). Adding a later version never changes historical
lines; `replay(invoice, request)` reproduces a historical invoice and reports
the first differing line if anything has drifted. Demurrage for a stay is
governed by the tariff in effect at discharge.
