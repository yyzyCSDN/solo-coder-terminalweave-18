from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Generic, TypeVar
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ConflictError, NotFoundError, ValidationError

T = TypeVar('T')


@dataclass(frozen=True)
class RateVersion(Generic[T]):
    """An immutable tariff rule and the half-open interval in which it is valid.

    ``effective_from`` is inclusive, ``effective_to`` is exclusive, so a
    boundary instant can be shared by two versions without overlapping.
    """
    version_id: str
    key: str
    rule: T
    currency: str
    effective_from: datetime
    effective_to: datetime | None = None

    def __post_init__(self) -> None:
        if not self.version_id or not self.key:
            raise ValidationError('rate version requires version_id and key')
        start = ensure_utc(self.effective_from)
        end = ensure_utc(self.effective_to) if self.effective_to is not None else None
        if end is not None and end <= start:
            raise ValidationError('rate version effective_to must be after effective_from')
        object.__setattr__(self, 'effective_from', start)
        object.__setattr__(self, 'effective_to', end)
        object.__setattr__(self, 'currency', self.currency.strip().upper())

    def effective_at(self, moment: datetime) -> bool:
        moment = ensure_utc(moment)
        return self.effective_from <= moment and (self.effective_to is None or moment < self.effective_to)


def _format_interval(version: RateVersion) -> str:
    end = 'open-ended' if version.effective_to is None else version.effective_to.isoformat()
    return f'{version.effective_from.isoformat()}..{end}'


def _overlaps(left: RateVersion, right: RateVersion) -> bool:
    left_end = left.effective_to or datetime.max.replace(tzinfo=left.effective_from.tzinfo)
    right_end = right.effective_to or datetime.max.replace(tzinfo=right.effective_from.tzinfo)
    return left.effective_from < right_end and right.effective_from < left_end


class RateVersionBook(Generic[T]):
    """Append-only registry of tariff rules keyed by business scope.

    Versions for one key form a timeline. Overlapping intervals are rejected
    up front; gaps are allowed but make settlement fail for business that
    falls inside them.
    """

    def __init__(self, kind: str):
        self.kind = kind
        self._versions: dict[str, list[RateVersion[T]]] = {}

    def add_version(self, version: RateVersion[T]) -> RateVersion[T]:
        for existing_key, entries in self._versions.items():
            for existing in entries:
                if existing.version_id == version.version_id:
                    raise ConflictError(
                        f"duplicate {self.kind} version id '{version.version_id}' "
                        f"(already registered for key '{existing_key}')"
                    )
        entries = self._versions.setdefault(version.key, [])
        for existing in entries:
            if _overlaps(existing, version):
                raise ConflictError(
                    f"{self.kind} versions for key '{version.key}' overlap: "
                    f"'{existing.version_id}' {_format_interval(existing)} "
                    f"vs '{version.version_id}' {_format_interval(version)}"
                )
        inserted = entries + [version]
        inserted.sort(key=lambda v: v.effective_from)
        self._versions[version.key] = inserted
        return version

    def retire_open_version(self, key: str, effective_to: datetime) -> RateVersion[T]:
        """Close the currently open-ended version for ``key`` at ``effective_to``.

        This is the append-only way to end a rate cycle: the retired interval
        is preserved, so business that already occurred under it still resolves
        to it. Backdated retirement is rejected; configure the succession
        before the new version takes effect.
        """
        effective_to = ensure_utc(effective_to)
        entries = self._versions.get(key, [])
        for index, version in enumerate(entries):
            if version.effective_to is None:
                if effective_to <= version.effective_from:
                    raise ValidationError(
                        f"cannot retire {self.kind} version '{version.version_id}': "
                        f'end {effective_to.isoformat()} is not after start '
                        f'{version.effective_from.isoformat()}'
                    )
                retired = replace(version, effective_to=effective_to)
                updated = list(entries)
                updated[index] = retired
                self._versions[key] = updated
                return retired
        raise NotFoundError(f"no open-ended {self.kind} version for key '{key}' to retire")

    def keys(self) -> set[str]:
        return set(self._versions)

    def versions(self, key: str) -> tuple[RateVersion[T], ...]:
        return tuple(self._versions.get(key, ()))

    def effective(self, key: str, moment: datetime) -> RateVersion[T] | None:
        moment = ensure_utc(moment)
        for version in self._versions.get(key, ()):
            if version.effective_at(moment):
                return version
        return None

    def resolve(self, key: str, moment: datetime) -> RateVersion[T]:
        moment = ensure_utc(moment)
        if key not in self._versions:
            raise NotFoundError(
                f"no {self.kind} rule registered for key '{key}' at {moment.isoformat()}"
            )
        version = self.effective(key, moment)
        if version is None:
            return self._missing_in_gap(key, moment)
        return version

    def _missing_in_gap(self, key: str, moment: datetime) -> RateVersion[T]:
        timeline = self._versions[key]
        previous = [v for v in timeline if v.effective_from <= moment and v.effective_to is not None and v.effective_to <= moment]
        nxt = [v for v in timeline if v.effective_from > moment]
        if previous and nxt:
            prev = max(previous, key=lambda v: v.effective_to)
            gap = f"gap between '{prev.version_id}' (until {prev.effective_to.isoformat()}) and '{min(nxt, key=lambda v: v.effective_from).version_id}' (from {min(nxt, key=lambda v: v.effective_from).effective_from.isoformat()})"
        elif previous:
            prev = max(previous, key=lambda v: v.effective_to)
            gap = f"gap after '{prev.version_id}' (until {prev.effective_to.isoformat()})"
        else:
            first = timeline[0]
            gap = f"gap before '{first.version_id}' (from {first.effective_from.isoformat()})"
        raise NotFoundError(
            f"no {self.kind} rule in effect for key '{key}' at {moment.isoformat()}; {gap}"
        )

    def require_currency(self, key: str, moment: datetime, currency: str) -> RateVersion[T]:
        version = self.resolve(key, moment)
        if version.currency != currency:
            raise ValidationError(
                f"{self.kind} version '{version.version_id}' for key '{key}' is denominated in "
                f"{version.currency}, settlement requires {currency}"
            )
        return version
