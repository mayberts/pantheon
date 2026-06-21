from abc import ABC, abstractmethod


class Platform(ABC):
    def __init__(self):
        self._progress: dict | None = None

    def _inc(self, key: str, amount: int = 1) -> None:
        if self._progress is not None:
            self._progress[key] = self._progress.get(key, 0) + amount

    @abstractmethod
    async def sync(self, account: dict, conn) -> None:
        """Pull data for `account` and upsert into DB via `conn`."""
