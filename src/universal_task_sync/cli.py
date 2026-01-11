import os
import subprocess
import tempfile
from typing import Optional

import typer

from .config import get_config
from .db import MappingManager
from .loader import get_plugin  # Assuming your entry-point loader logic is here
from .models import TaskCIR

app = typer.Typer(help="Universal Task Sync: Bridge your task managers.")


@app.command()
def sync(
    input_plugin: str = typer.Option(..., "--input", "-i"),
    output_plugin: str = typer.Option(..., "--output", "-o"),
    project: str = typer.Option(..., "-project", help="Source project or filter"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="Destination identifier"),
):
    """Sync tasks from a source to a destination with persistent mapping memory."""
    mgr = MappingManager()

    # 1. Initialize Plugins
    src = get_plugin(input_plugin)
    dst = get_plugin(output_plugin)

    # 2. Resolve Target from History
    # If user didn't provide -t, look in the project_map table
    if not target:
        target = mgr.get_stored_target(input_plugin, project, output_plugin)
        if not target:
            typer.secho(f"❓ No target history found for {input_plugin}:{project}", fg="yellow")
            target = typer.prompt(f"Enter the {output_plugin} target (e.g., owner/repo or project:org/num)")
            mgr.store_project_link(input_plugin, project, output_plugin, target)
    else:
        # Save or update the mapping if provided explicitly
        mgr.store_project_link(input_plugin, project, output_plugin, target)

    # 3. Authentication
    # Each plugin handles its own credential loading internally
    src.authenticate()
    dst.authenticate()

    # 4. Permission Guard (Plugin Specific)
    # Check if the token has the right scopes for the resolved target
    if hasattr(dst, "validate_permissions"):
        dst.validate_permissions(target)

    # 5. The Sync Loop
    typer.echo(f"🚀 Syncing {project} -> {target}...")

    raw_tasks = src.fetch_raw(project)

    for raw in raw_tasks:
        src_task = src.to_cif(raw)
        internal_uid = mgr.get_internal_uuid(input_plugin, src_task.ext_id)

        # 1. New Task? Just push it.
        if not internal_uid:
            internal_uid = mgr.create_mapping(input_plugin, src_task.ext_id)
            src_task.uuid = internal_uid
            _push_new_task(src_task, dst, mgr, target)
            continue

        # 2. Existing Task? Fetch the "Last Known State" from DB
        src_task.uuid = internal_uid
        last_sync = mgr.get_sync_state(internal_uid)

        # Check if Source changed since last sync
        src_changed = last_sync is None or src_task.get_content_hash() != last_sync["hash"]

        if src_changed:
            # 3. CONFLICT CHECK: Did the Destination change too?
            dst_ext_id = mgr.get_external_id(output_plugin, internal_uid)
            raw_dst = dst.fetch_one(dst_ext_id, target)  # You'll need this method in plugin
            current_dst_task = dst.to_cif(raw_dst)

            dst_changed = last_sync is None or current_dst_task.get_content_hash() != last_sync["hash"]

            if dst_changed:
                base_task = TaskCIR.from_dict(last_sync["data"]) if last_sync else None
                typer.secho(f"⚠️ CONFLICT: {src_task.description} changed on both sides!", fg="red")
                # 1. Perform the 3-way merge
                merged_task = resolve_conflict_via_git(base_task, src_task, current_dst_task)

                # 2. Update Source (Taskwarrior) if needed
                _tmp = src_task.copy()
                _tmp.update_from(merged_task)
                if src_task.get_content_hash() != _tmp.get_content_hash():
                    src.update_task(_tmp)

                # 3. Update Destination (GitHub) if needed
                _tmp = current_dst_task.copy()
                _tmp.update_from(merged_task)
                if current_dst_task.get_content_hash() != _tmp.get_content_hash():
                    dst.update_task(_tmp)

                # 4. Finalize state
                mgr.update_sync_state(_tmp)
            else:
                # --- CASE B: CLEAN UPDATE (Only source changed) ---
                typer.echo(f"  ↑ Updating destination: {src_task.description[:40]}...")

                # raw_out = dst.from_cif(src_task)
                # raw_out["ext_id"] = dst_ext_id

                success_id = dst.patch_task(dst_ext_id, merged_task)
                if success_id:
                    mgr.update_sync_state(src_task)

    typer.secho("✅ Sync complete.", fg="green", bold=True)


def _push_new_task(task: TaskCIR, dst_plugin, mgr: MappingManager, target: str):
    """Handles the first-time creation and state capture of a task."""
    # 1. Translate to destination format
    raw_out = dst_plugin.from_cif(task)

    # 2. Push to destination
    new_ext_id = dst_plugin.send_raw(raw_out, target)

    if new_ext_id:
        # 3. Save the Identity Mapping (Internal UUID <-> GitHub ID)
        mgr.create_mapping(dst_plugin.name, new_ext_id, task.uuid)

        # 4. Save the State Snapshot (The Hash)
        # This prevents the next sync from thinking it's a 'new change'
        mgr.update_sync_state(task)
        typer.echo(f"  + Created and state-tracked: {task.description[:40]}")


def resolve_conflict_via_git(base_task: Optional[TaskCIR], p1_task: TaskCIR, p2_task: TaskCIR) -> TaskCIR:
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init", "-q"], cwd=tmpdir)
        filename = "TASK.json"
        full_merged_path = os.path.join(tmpdir, filename)

        def get_git_hash(task_obj):
            # IMPORTANT: Use the encoder to ensure Enums don't crash the hasher
            content = task_obj.to_json(only_mergeable=True) if task_obj else "{}"
            res = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], input=content, text=True, capture_output=True, cwd=tmpdir
            )
            return res.stdout.strip()

        # Generate the three stages
        h_base = get_git_hash(base_task)
        h_p1 = get_git_hash(p1_task)
        h_p2 = get_git_hash(p2_task)

        # Update index with the 3 stages
        index_info = f"100644 {h_base} 1\t{filename}\n100644 {h_p1} 2\t{filename}\n100644 {h_p2} 3\t{filename}\n"
        subprocess.run(["git", "update-index", "--index-info"], input=index_info, cwd=tmpdir, text=True)

        # Write the 'Current' version to disk so kdiff3 has a file to edit
        with open(full_merged_path, "w") as f:
            f.write(p1_task.to_json(only_mergeable=True))

        # DEBUG: Print hashes to see if they are all identical
        print(f"DEBUG: B:{h_base} P1:{h_p1} P2:{h_p2}")

        # The config flags here prevent the 'mv' error even if kdiff3 fails
        cmd = ["git", "-c", "mergetool.keepBackup=false", "mergetool", "--tool=kdiff3", "--no-prompt", filename]

        while True:
            subprocess.run(cmd, cwd=tmpdir)
            try:
                with open(full_merged_path) as f:
                    return TaskCIR.to_dict(TaskCIR.from_json(f.read()))
            except Exception as e:
                typer.secho(f"❌ Merge Result Invalid: {e}", fg="red")
                if not typer.confirm("Fix in editor?"):
                    raise typer.Abort()


def sync_peers(p1, p2, target1, target2, mgr):
    # 1. Fetch live data from both 'databases'
    # We use 'pending' for TW and 'open' for GitHub to keep the set small
    tasks1 = {t.ext_id: p1.to_cif(t) for t in p1.fetch_raw(target1)}
    tasks2 = {t.ext_id: p2.to_cif(t) for t in p2.fetch_raw(target2)}

    # 2. Get the set of all internal UUIDs we know about for these two targets
    all_uuids = mgr.get_all_mapped_uuids(p1.name, p2.name)

    for uid in all_uuids:
        # Get last known state from our local DB
        last_sync = mgr.get_sync_state(uid)

        # Get live IDs for this specific task
        id1 = mgr.get_external_id(p1.name, uid)
        id2 = mgr.get_external_id(p2.name, uid)

        # Get the actual TaskCIR objects
        t1 = tasks1.get(id1)
        t2 = tasks2.get(id2)

        # Determine changes relative to the DB
        p1_changed = t1 and (last_sync is None or t1.get_content_hash() != last_sync["hash"])
        p2_changed = t2 and (last_sync is None or t2.get_content_hash() != last_sync["hash"])

        if p1_changed and p2_changed:
            # Conflict: Both peer databases diverged from our state DB
            merged = resolve_conflict_3way(last_sync, t1, t2)
            p1.update_task(merged)
            p2.send_raw(p2.from_cif(merged) | {"ext_id": id2}, target2)
            mgr.update_sync_state(merged)

        elif p1_changed:
            # Sync p1 -> p2
            p2.send_raw(p2.from_cif(t1) | {"ext_id": id2}, target2)
            mgr.update_sync_state(t1)

        elif p2_changed:
            # Sync p2 -> p1
            p1.update_task(t2 | {"ext_id": id1})
            mgr.update_sync_state(t2)


def construct_tool_cmd(paths: dict, tmpdir: str) -> list:
    """
    Maps path keys to the specific argument structure of the chosen difftool.
    paths keys: 'BASE', 'P1' (Local), 'P2' (Remote), 'MERGED' (Output)
    """
    config = get_config()
    tool = config.get("difftool", "vimdiff")

    def get_p(key):

        val = paths[key]
        file_path = val[1] if isinstance(val, (tuple, list)) else val
        return os.path.basename(file_path)

    # Extract raw paths from the tuple/dict structure
    base = get_p("BASE")
    p1 = get_p("P1")
    p2 = get_p("P2")
    merged_path = get_p("MERGED")
    rv = ["git", "mergetool", "--tool=vimdiff", "--no-prompt", base, p1, p2, merged_path]
    print(f"returning {rv}")
    return rv


from tabulate import tabulate

config_app = typer.Typer(help="Manage UTS and Plugin configurations.")
app.add_typer(config_app, name="config")


@config_app.command("list")
def config_list():
    """List all available keys, defaults, and current values."""
    manifest = get_full_manifest()
    current_config = load_user_config()  # Your existing JSON loader

    table_data = []
    for key, default_val in manifest.items():
        # Show actual value if set, otherwise show the default
        actual = current_config.get(key)
        status = actual if actual is not None else f"{default_val} (default)"
        table_data.append([key, status])

    print(tabulate(table_data, headers=["Configuration Key", "Current Value / Default"]))


@config_app.command("get")
def config_get(key: str):
    """Retrieve the current value of a specific key."""
    manifest = get_full_manifest()
    current_config = load_user_config()

    if key in current_config:
        typer.echo(current_config[key])
    elif key in manifest:
        typer.echo(f"{manifest[key]} (default)")
    else:
        typer.secho(f"❌ Key '{key}' not found in core or any plugin.", fg="red")


@config_app.command("set")
def config_set(key: str, value: str):
    """Save a setting to your local config file."""
    manifest = get_full_manifest()

    if key not in manifest:
        typer.confirm(f"Key '{key}' is unknown. Set anyway?", abort=True)

    save_to_config_file(key, value)
    typer.secho(f"✅ Saved: {key} = {value}", fg="green")


if __name__ == "__main__":
    app()
