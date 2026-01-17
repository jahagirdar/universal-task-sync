import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Union, get_type_hints

from .serialization import TaskJSONEncoder, parse_iso_datetime, parse_iso_duration


class TaskStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    DELETED = "deleted"
    WAITING = "waiting"


class Priority(Enum):
    HIGH = "H"
    MEDIUM = "M"
    LOW = "L"


@dataclass
class TaskCIR:
    # --- Identification & Type ---
    uuid: str = field(default="", metadata={"merge": False})
    tool_uid: str = field(default="", metadata={"merge": False})
    last_modified: datetime = field(default_factory=datetime.now, metadata={"merge": False})
    description: str = field(default="", metadata={"merge": True})
    # Allows the engine to distinguish between a 'task', 'comment', or 'milestone'
    type: str = field(default="task", metadata={"merge": True})

    # --- Content ---
    body: Optional[str] = field(default=None, metadata={"merge": True})

    # --- Context & Categorization ---
    project: Optional[str] = field(default="Inbox", metadata={"merge": False})
    status: TaskStatus = field(default=TaskStatus.PENDING, metadata={"merge": True})
    priority: Optional[Priority] = field(default=None, metadata={"merge": True})
    tags: List[str] = field(default_factory=list, metadata={"merge": True})

    # --- Timing & Duration ---
    start: Optional[datetime] = field(default=None, metadata={"merge": True})
    due: Optional[datetime] = field(default=None, metadata={"merge": True})
    scheduled: Optional[datetime] = field(default=None, metadata={"merge": True})
    effort: Optional[timedelta] = field(default=None, metadata={"merge": True})
    actual_effort: Optional[timedelta] = field(default=None, metadata={"merge": True})

    # --- Progress & Relationships ---
    progress: int = field(default=0, metadata={"merge": True})
    # Predecessors (Tasks this depends on, or Parent of a comment)
    depends: List[str] = field(default_factory=list, metadata={"merge": True})
    # Successors (Followers or replies to a comment)
    followers: List[str] = field(default_factory=list, metadata={"merge": True})

    # --- People ---
    owner: Optional[str] = field(default=None, metadata={"merge": True})
    delegate: Optional[str] = field(default=None, metadata={"merge": True})

    # --- Extra Metadata ---
    source_url: Optional[str] = field(default=None, metadata={"merge": False})
    custom_fields: Dict[str, Any] = field(default_factory=dict, metadata={"merge": False})

    def copy(self) -> "TaskCIR":
        """
        Creates a deep copy of the task.
        Uses dataclasses.replace to ensure all fields (including metadata) are preserved.
        """
        return replace(self)

    def update_from(self, merged_data: dict) -> None:
        """
        Updates the current object's content fields using a dictionary
        (the result of the 3-way merge).

        This method is 'identity-safe': it only updates fields that were
        part of the merge, leaving uuid, tool_id, and last_modified alone.
        """
        for f in fields(self):
            # Only update if the field is mergeable AND exists in the truth dict
            if f.metadata.get("merge", True) and f.name in merged_data:
                setattr(self, f.name, merged_data[f.name])

    def to_dict(self, only_mergeable: bool = False) -> dict:
        if not only_mergeable:
            return asdict(self)
        return {f.name: getattr(self, f.name) for f in fields(self) if f.metadata.get("merge", True)}

    def get_content_hash(self) -> str:
        """
        Hashes only mergeable fields.
        Uses to_json(only_mergeable=True) to ensure a stable, stringified representation.
        """
        content_json = self.to_json(only_mergeable=True)
        return hashlib.md5(content_json.encode()).hexdigest()

    def to_json(self, only_mergeable: bool = False) -> str:
        """Helper to dump this specific task using the standard encoder."""
        data = self.to_dict(only_mergeable=only_mergeable)
        return json.dumps(data, indent=4, sort_keys=True, cls=TaskJSONEncoder)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskCIR":
        """
        Dynamic Decoder: Inspects the dataclass type hints to decide
        which parser (datetime, duration, enum) to use.
        """
        hints = get_type_hints(cls)
        processed_data = {}

        for name, value in data.items():
            if value is None:
                processed_data[name] = None
                continue

            field_type = hints.get(name)

            # 1. Handle Datetimes
            if field_type == datetime or field_type == Optional[datetime]:
                processed_data[name] = parse_iso_datetime(value) if isinstance(value, str) else value

            # 2. Handle Durations (timedelta)
            elif field_type == timedelta or field_type == Optional[timedelta]:
                processed_data[name] = parse_iso_duration(value) if isinstance(value, str) else value

            # 3. Handle Enums
            elif isinstance(field_type, type) and issubclass(field_type, Enum):
                processed_data[name] = field_type(value)

            elif field_type == List[str]:
                processed_data[name] = list(value)
            elif hasattr(field_type, "__origin__") and field_type.__origin__ is list:
                processed_data[name] = value

            # 4. Handle Optionals of Enums (Generic parsing)
            elif hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
                # Basic check for Enum in Union (Optional[Priority])
                inner_types = [t for t in field_type.__args__ if isinstance(t, type) and issubclass(t, Enum)]
                if inner_types:
                    processed_data[name] = inner_types[0](value)
                else:
                    processed_data[name] = value
            else:
                processed_data[name] = value

        return cls(**processed_data)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskCIR":
        return cls.from_dict(json.loads(json_str))
