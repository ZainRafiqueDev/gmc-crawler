"""Tracks which (product_id, rule) violations have already been alerted on,
so re-running the same catalog doesn't re-notify for issues nobody has
fixed or newly introduced. Swap for a DB-backed table keyed the same way
for cross-restart persistence."""
from __future__ import annotations

from abc import ABC, abstractmethod


class AlertDeduper(ABC):
    @abstractmethod
    def new_keys(self, keys: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return the subset of `keys` that haven't been alerted on yet."""
        raise NotImplementedError

    @abstractmethod
    def mark_alerted(self, keys: set[tuple[str, str]]) -> None:
        raise NotImplementedError


class InMemoryAlertDeduper(AlertDeduper):
    def __init__(self) -> None:
        self._alerted: set[tuple[str, str]] = set()

    def new_keys(self, keys: set[tuple[str, str]]) -> set[tuple[str, str]]:
        return keys - self._alerted

    def mark_alerted(self, keys: set[tuple[str, str]]) -> None:
        self._alerted |= keys
