from abc import ABC, abstractmethod


class Platform(ABC):
    @abstractmethod
    async def sync(self, account: dict, conn) -> None:
        """Pull data for `account` and upsert into DB via `conn`."""
