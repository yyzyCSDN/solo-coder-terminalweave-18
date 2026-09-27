from __future__ import annotations
from dataclasses import dataclass
from terminalweave.core.money import Money


@dataclass(frozen=True)
class SurchargeRule:
    """A flat add-on charge (bunker adjustment, port congestion, peak season...)."""
    code: str
    amount: Money

    def price(self, count: int = 1) -> Money:
        return self.amount.scale(count)
