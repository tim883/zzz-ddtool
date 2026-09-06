from __future__ import annotations


class DiscBudget:
    """Stop after N discs have been looked at (including skip)."""

    def __init__(self, max_discs: int) -> None:
        if max_discs < 1:
            raise ValueError("max_discs 至少是 1")
        self.max_discs = max_discs
        self.processed = 0

    def can_continue(self) -> bool:
        return self.processed < self.max_discs

    def consume(self) -> bool:
        if not self.can_continue():
            return False
        self.processed += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.processed >= self.max_discs
