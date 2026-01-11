from abc import ABC, abstractmethod

class BasePlugin(ABC):
    @abstractmethod
    def fetch_items(self, since: Optional[datetime] = None) -> List[TaskCIR]:
        """Fetch all tasks/comments from the API modified since 'since'."""
        pass

    @abstractmethod
    def push_item(self, item: TaskCIR) -> str:
        """
        Send a TaskCIR object to the external API.
        Returns the new 'ext_id' if created.
        """
        pass

    @abstractmethod
    def update_item(self, item: TaskCIR) -> bool:
        """Update an existing item on the external API."""
        pass

    @abstractmethod
    def map_to_internal(self, raw_data: Any) -> TaskCIR:
        """Convert API-specific JSON to our TaskCIR dataclass."""
        pass

    @abstractmethod
    def map_from_internal(self, item: TaskCIR) -> Any:
        """Convert TaskCIR dataclass to API-specific JSON."""
        pass
    class BasePlugin(ABC):
    @abstractmethod
    def authenticate(self):
        """Handle credential collection/loading. Raise error if it fails."""
        pass
