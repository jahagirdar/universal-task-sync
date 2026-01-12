import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any, List

from universal_task_sync.models import CIRTaskEncoder, TaskCIR


class JsonPlugin:
    """Strictly handles JSON File <-> CIF translation and IO."""

    def authenticate(self):
        pass

    def to_cif(self, raw_data: dict) -> TaskCIR:
        """Enhanced to_cif that reconstructs Types from strings."""
        # Convert status string back to Enum
        if "status" in raw_data and isinstance(raw_data["status"], str):
            raw_data["status"] = TaskStatus(raw_data["status"])

        # Convert priority string back to Enum
        if raw_data.get("priority"):
            raw_data["priority"] = Priority(raw_data["priority"])

        # Convert ISO strings back to datetime
        for date_field in ["last_modified", "start", "due", "scheduled"]:
            if raw_data.get(date_field):
                raw_data[date_field] = datetime.fromisoformat(raw_data[date_field])

        return TaskCIR(**raw_data)

    def from_cif(self, task: TaskCIR) -> dict:
        """Translate a TaskCIR object to a dictionary for JSON serialization."""
        return dataclasses.asdict(task)

    def fetch_raw(self, target: str) -> List[dict]:
        """
        IO: Read from a JSON file.
        In this plugin, 'filter_query' is interpreted as the file path.
        """
        path = Path(filter_query)
        if not path.exists():
            return []

        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, list) else [data]

    def send_raw(self, raw_data: Any):
        """
        IO: Write to stdout or a file.
        By default, we output to stdout to allow piping (e.g., uts -o json > tasks.json).
        """
        # We wrap in a list because uts processes tasks one by one in the loop
        print(json.dumps(raw_data, cls=CIRTaskEncoder, indent=4))
