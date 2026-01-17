import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import typer

from .base import BasePlugin
from .models import TaskCIR

SQL_DEBUG = True


def sql_logger(query: str) -> None:
    print(f"DEBUG SQL: {query}")


def get_data_dir() -> Path:
    """Resolve the persistent data directory for the SQLite database."""
    xdg_data = os.getenv("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    data_dir = base / "universal_task_sync"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def init_db() -> None:
    """Initializes the database with lean tables for mapping and state."""
    db_path = get_data_dir() / "map.db"
    with sqlite3.connect(db_path) as conn:
        if SQL_DEBUG:
            conn.set_trace_callback(sql_logger)
            print(f"DEBUG: Opening connection to {db_path}")
        # Table 1: ID Map - Links internal UUIDs to service-specific IDs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS id_map (
                internal_uuid TEXT NOT NULL,
                service_name  TEXT NOT NULL,
                external_id   TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                PRIMARY KEY (service_name, external_id)
            )
        """)

        # Table 2: Sync State - Stores the 'Last Known Good' state as a blob
        # This is the 'Base' for 3-way merges.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                internal_uuid  TEXT PRIMARY KEY,
                content_hash   TEXT NOT NULL,
                raw_json       TEXT NOT NULL,
                last_modified  TEXT
            )
        """)

        # Table 3: Project Links - Remembers where -p maps to -t
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_map (
                src_plugin  TEXT NOT NULL,
                src_project TEXT NOT NULL,
                dst_plugin  TEXT NOT NULL,
                dst_target  TEXT NOT NULL,
                PRIMARY KEY (src_plugin, src_project, dst_plugin)
            )
        """)
        conn.commit()


class MappingManager:
    def __init__(self) -> None:
        self.db_path = get_data_dir() / "map.db"
        init_db()

    def set_status(self, internal_id: str, status: str) -> None:
        """Update status to 'active' or 'completed'."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE id_map SET status = ? WHERE internal_uuid = ?", (status, internal_id))

    # --- Sync State & 3-Way Merge Logic ---
    def ensure_mapping(self, service_name: str, external_id: str) -> str:
        """Find or create mapping, ensuring status is reset to active if reopened."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT internal_uuid FROM id_map WHERE service_name = ? AND external_id = ?",
                (service_name, str(external_id)),
            ).fetchone()
            if row:
                uid = row[0]
                conn.execute(
                    "UPDATE id_map SET status = 'active' WHERE  service_name=? AND external_id=?",
                    (service_name, external_id),
                )
                return uid

            new_uid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO id_map (internal_uuid, service_name, external_id, status) VALUES (?, ?, ?, 'active')",
                (new_uid, service_name, str(external_id)),
            )
            return new_uid

    def update_sync_state(self, task: TaskCIR) -> None:
        """Captures the full state of the task to act as the future Merge Base."""
        content_hash = task.get_content_hash()
        # We store the FULL JSON including system fields for total recovery
        raw_json = task.to_json(only_mergeable=True)

        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_state
                (internal_uuid, content_hash, raw_json, last_modified)
                VALUES (?, ?, ?, ?)
            """,
                (task.uuid, content_hash, raw_json, datetime.now().isoformat()),
            )

    def delete_mapping(self, internal_id: str) -> None:
        """
        Removes the sync memory for a task.
        Usually called when a task is completed/deleted on both sides.
        """
        with sqlite3.connect(self.db_path) as conn:
            # Remove the service links (e.g. TW UUID <-> GH ID)
            conn.execute("DELETE FROM id_map WHERE internal_uuid = ?", (internal_id,))
            # Remove the content hash/snapshot
            conn.execute("DELETE FROM sync_state WHERE internal_uuid = ?", (internal_id,))
        typer.echo(f"  🧹 Mapping cleared for internal task {internal_id[:8]}")

    def get_sync_state(self, internal_uuid: str) -> Optional[Dict[str, Any]]:
        """Retrieves the hash and data for change detection."""
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            row = conn.execute(
                "SELECT content_hash, raw_json FROM sync_state WHERE internal_uuid = ?", (internal_uuid,)
            ).fetchone()
            if row:
                return {"hash": row[0], "data": json.loads(row[1])}
            return None

    def get_sync_base(self, internal_uuid: str) -> Optional[TaskCIR]:
        """Reconstructs the TaskCIR object for use as Stage 1 in Git Mergetool."""
        state = self.get_sync_state(internal_uuid)
        return TaskCIR.from_dict(state["data"]) if state else None

    # --- Identity & Mapping ---

    def get_internal_uuid(self, service: str, external_id: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            row = conn.execute(
                "SELECT internal_uuid FROM id_map WHERE service_name = ? AND external_id = ?", (service, external_id)
            ).fetchone()
            return row[0] if row else None

    def create_mapping(self, service: str, external_id: str, existing_uuid: str | None = None) -> str:
        """
        Links an external ID to an internal UUID.
        If existing_uuid is provided (from Reconciler), it 'bridges' the tasks.
        """
        print(f"{service=}")
        new_uuid = existing_uuid or str(uuid.uuid4())[:16]
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            # INSERT OR REPLACE ensures that if we are re-mapping
            # a task to a different internal group, it updates correctly.
            conn.execute(
                """
                INSERT OR REPLACE INTO id_map (internal_uuid, service_name, external_id, status)
                VALUES (?, ?, ?,
                    COALESCE((SELECT status FROM id_map WHERE internal_uuid=? AND service_name=?), 'active')
                )
                """,
                (new_uuid, service, str(external_id), new_uuid, service),
            )
        return new_uuid

    def get_external_id(self, service: BasePlugin, internal_uuid: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            row = conn.execute(
                "SELECT external_id FROM id_map WHERE service_name = ? AND internal_uuid = ?", (service, internal_uuid)
            ).fetchone()
            return row[0] if row else None

    # --- Project Connectivity ---

    def store_project_link(self, src_p: str, src_proj: str, dst_p: str, dst_t: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            conn.execute(
                """
                INSERT OR REPLACE INTO project_map VALUES (?, ?, ?, ?)
            """,
                (src_p, src_proj, dst_p, dst_t),
            )

    def get_stored_target(self, src_p: str, src_proj: str, dst_p: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            if SQL_DEBUG:
                conn.set_trace_callback(sql_logger)
            row = conn.execute(
                """
                SELECT dst_target FROM project_map
                WHERE src_plugin=? AND src_project=? AND dst_plugin=?
            """,
                (src_p, src_proj, dst_p),
            ).fetchone()
            return row[0] if row else None
