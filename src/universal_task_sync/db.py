import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Optional  # Added Optional here

from .models import TaskCIR


def get_data_dir() -> Path:
    """Resolve XDG_DATA_HOME, defaulting to ~/.local/share/universal_task_sync"""
    xdg_data = os.getenv("XDG_DATA_HOME")
    if xdg_data:
        base = Path(xdg_data)
    else:
        base = Path.home() / ".local" / "share"

    data_dir = base / "universal_task_sync"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def init_db():
    db_path = get_data_dir() / "map.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Table 1: ID Map (The Bridge)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS id_map (
            internal_uuid TEXT NOT NULL,
            service_name  TEXT NOT NULL,
            external_id   TEXT NOT NULL,
            PRIMARY KEY (internal_uuid, service_name)
        )
    """)

    # Table 2: Sync State (Comprehensive Field List)
    # This stores the "Last Known Good" state of a task
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            internal_uuid  TEXT PRIMARY KEY,
            type           TEXT DEFAULT 'task',
            description    TEXT,
            body           TEXT,
            project        TEXT,
            status         TEXT,
            priority       TEXT,
            tags           TEXT,           -- Stored as JSON string or comma-separated
            start_date     DATETIME,
            due_date       DATETIME,
            scheduled_date DATETIME,
            effort         TEXT,           -- timedelta as string (e.g. '2:00:00')
            actual_effort  TEXT,
            progress       INTEGER DEFAULT 0,
            owner          TEXT,
            delegate       TEXT,
            depends        TEXT,           -- JSON list of internal_uuids
            last_modified  DATETIME,
            content_hash   TEXT            -- MD5/SHA of core fields to detect changes
        )
    """)
    # Table 3: Project Memory (New!)
    # Maps a source (e.g., tw:project_name) to a target (e.g., gh:owner/repo)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_map (
            src_plugin    TEXT NOT NULL,
            src_project   TEXT NOT NULL,
            dst_plugin    TEXT NOT NULL,
            target_id     TEXT NOT NULL,
            PRIMARY KEY (src_plugin, src_project, dst_plugin)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext ON id_map (external_id, service_name)")
    conn.commit()
    conn.close()
    return db_path


class MappingManager:
    def __init__(self):
        self.db_path = init_db()

    def get_internal_uuid(self, service: str, ext_id: str) -> Optional[str]:
        """Check if an external task is already known."""
        with sqlite3.connect(self.db_path) as conn:
            res = conn.execute(
                "SELECT internal_uuid FROM id_map WHERE service_name=? AND external_id=?", (service, ext_id)
            ).fetchone()
            return res[0] if res else None

    def create_mapping(self, service: str, ext_id: str, internal_uuid: str = None) -> str:
        """Link an external task to a new or existing internal UUID."""
        uid = internal_uuid or str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO id_map (internal_uuid, service_name, external_id) VALUES (?, ?, ?)",
                (uid, service, ext_id),
            )
            return uid

    def get_stored_target(self, src_plugin: str, src_project: str, dst_plugin: str) -> Optional[str]:
        """Find if we previously synced this source project to a specific destination."""
        with sqlite3.connect(self.db_path) as conn:
            res = conn.execute(
                "SELECT target_id FROM project_map WHERE src_plugin=? AND src_project=? AND dst_plugin=?",
                (src_plugin, src_project, dst_plugin),
            ).fetchone()
            return res[0] if res else None

    def store_project_link(self, src_plugin: str, src_project: str, dst_plugin: str, target_id: str):
        """Save the link for future one-word syncs."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO project_map VALUES (?, ?, ?, ?)",
                (src_plugin, src_project, dst_plugin, target_id),
            )

    def update_sync_state(self, task: TaskCIR):
        """Saves the full object and its hash for future 3-way merges."""
        content_data = {
            "description": task.description,
            "body": task.body,
            "status": task.status.value,
            "tags": sorted(task.tags) if task.tags else [],
        }
        content_hash = hashlib.md5(json.dumps(content_data, sort_keys=True).encode()).hexdigest()

        # Serialize the whole task for the 'Base' file in merges
        full_json = json.dumps(task.to_dict())

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_state
                (internal_uuid, content_hash, raw_json, last_modified)
                VALUES (?, ?, ?, ?)
            """,
                (task.uuid, content_hash, full_json, task.last_modified.isoformat()),
            )
            conn.commit()

    def get_sync_state(self, internal_uuid: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            res = conn.execute(
                "SELECT content_hash as hash FROM sync_state WHERE internal_uuid=?", (internal_uuid,)
            ).fetchone()
            return {"hash": res[0]} if res else None

    def get_sync_base(self, internal_uuid: str) -> Optional[TaskCIR]:
        with sqlite3.connect(self.db_path) as conn:
            res = conn.execute("SELECT raw_json FROM sync_state WHERE internal_uuid=?", (internal_uuid,)).fetchone()
            if res:
                data = json.loads(res[0])
                return TaskCIR.from_dict(data)
            return None

    def get_external_id(self, service: str, internal_uuid: str) -> Optional[str]:
        """
        Retrieves the service-specific ID (e.g., GitHub issue number)
        for a given internal UUID.
        """
        query = "SELECT external_id FROM id_map WHERE service_name = ? AND internal_uuid = ?"

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(query, (service, internal_uuid)).fetchone()
            return row[0] if row else None

    def get_all_mapped_uuids(self, service1: str, service2: str) -> set:
        with sqlite3.connect(self.db_path) as conn:
            res = conn.execute(
                """
                SELECT DISTINCT internal_uuid FROM id_map
                WHERE service_name IN (?, ?)
            """,
                (service1, service2),
            ).fetchall()
            return {r[0] for r in res}
