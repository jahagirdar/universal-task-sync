from abc import ABC, abstractmethod
from typing import Any, List

from .models import TaskCIR


class BasePlugin(ABC):
    @abstractmethod
    def set_filter(self, filter: str) -> None:
        pass

    @abstractmethod
    def update_task(self, id: str, task: TaskCIR, filter: str) -> str:
        pass

    @abstractmethod
    def delete_task(self, id: str, filter: str) -> bool:
        pass

    @abstractmethod
    def update_relationships(self, id: str, task: TaskCIR, mgr: Any) -> None:
        pass

    @abstractmethod
    def fetch_one(self, id: str) -> dict:
        pass

    @abstractmethod
    def validate_permissions(self, filter: str) -> bool:
        pass

    @abstractmethod
    def name(self) -> str:
        "Plugin name"
        pass

    @abstractmethod
    def authenticate(self) -> bool:
        """Handle credential collection/loading. Raise error if it fails."""
        pass

    @abstractmethod
    def fetch_raw(self, target: str) -> List[Any]:
        """Fetch all raw data from the API."""
        pass

    @abstractmethod
    def to_cif(self, raw_data: Any) -> TaskCIR:
        """Convert API-specific JSON to our TaskCIR dataclass."""
        pass

    @abstractmethod
    def from_cif(self, item: TaskCIR) -> Any:
        """Convert TaskCIR dataclass to API-specific JSON."""
        pass

    @abstractmethod
    def send_raw(self, raw_item: Any, target: str) -> str:
        """Send raw data to the external API and return the new 'tool_id'."""
        pass

    @abstractmethod
    def patch_raw(self, tool_id: str, raw_item: Any, target: str) -> bool:
        """Update an existing item on the external API using raw data."""
        pass
