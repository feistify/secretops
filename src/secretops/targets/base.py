from abc import ABC, abstractmethod
from typing import Any


class Target(ABC):
    @abstractmethod
    def describe_write_plan(self) -> dict[str, Any]:
        """Return safe metadata for a planned target write without secret values."""
