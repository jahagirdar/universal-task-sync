import os
import subprocess
import tempfile
from typing import Optional

import typer

from .db import MappingManager
from .loader import get_plugin  # Assuming your entry-point loader logic is here
from .models import TaskCIR
from .reconciler import reconcile_app

app = typer.Typer(help="Universal Task Sync: Bridge your task managers.")
app.add_typer(reconcile_app, name="reconcile")


import logging

logging.basicConfig(
    filename="/tmp/uts.log",
    level=logging.DEBUG,
    format="%(module)s %(funcName)s %(lineno)d %(levelname)s:: %(message)s",
)


@app.command()
def sync(
    a_plugin: str = typer.Option(..., "--a_plugin", "-a"),
    b_plugin: str = typer.Option(..., "--b_plugin", "-b"),
    a_filter: str = typer.Option(..., "--a_filter", help="a filter"),
    b_filter: Optional[str] = typer.Option(None, "--b_filter", help="b filter"),
) -> None:
    """Sync tasks with persistent mapping, recursive discovery, and hybrid link logic."""
    mgr = MappingManager()
    a = get_plugin(a_plugin)
    b = get_plugin(b_plugin)

    # --- RETAINED: Target Resolution Logic ---
    if not b_filter:
        b_filter = mgr.get_stored_target(a_plugin, a_filter, b_plugin) or typer.prompt(f"Enter {b_plugin} target")
        mgr.store_project_link(a_plugin, a_filter, b_plugin, b_filter)
    a.set_filter(a_filter)
    b.set_filter(b_filter)
    # 3. Authentication
    # Each plugin handles its own credential loading internally
    for plugin in [a, b]:
        if not plugin.authenticate():
            typer.secho(f"❌ Auth failed for {plugin.name}", fg="red")
            raise typer.Exit(1)

    # 4. Permission Guard (Plugin Specific)
    # Check if the token has the right scopes for the resolved b_filter
    if hasattr(b, "validate_permissions"):
        b.validate_permissions()
    if hasattr(a, "validate_permissions"):
        a.validate_permissions()
    if not typer.confirm(f"Sync {a_plugin} ({a_filter}) <-> {b_plugin} ({b_filter})?", default=True):
        raise typer.Abort()

    # 1. FETCH & DISCOVER (Recursive Discovery Phase)
    # Builds the graph and ensures everything has an Internal UUID
    tasks_a = {a.to_cif(r).tool_uid: a.to_cif(r) for r in a.fetch_raw()}
    tasks_b = {b.to_cif(r).tool_uid: b.to_cif(r) for r in b.fetch_raw()}
    logging.debug(f"{tasks_a=}\n{tasks_b=}")

    for t in list(tasks_a.values()):
        t.uuid = mgr.ensure_mapping(a_plugin, t.tool_uid)
        t.depends = translate_and_discover(a, tasks_a, t.depends, mgr)
        t.followers = translate_and_discover(a, tasks_a, t.followers, mgr)

    for t in list(tasks_b.values()):
        t.uuid = mgr.ensure_mapping(b_plugin, t.tool_uid)
        t.depends = translate_and_discover(b, tasks_b, t.depends, mgr)
        t.followers = translate_and_discover(b, tasks_b, t.followers, mgr)

    # 2. HYBRID SYNC PASS (Content + Available Links)
    all_uuids = set(t.uuid for t in tasks_a.values()) | set(t.uuid for t in tasks_b.values())
    pending_links = []  # Track (plugin, tool_uid, task_cif) for Pass 2

    mapped_count = len(all_uuids)
    if mapped_count > 0 and (len(tasks_a) == 0 or len(tasks_b) == 0):
        typer.confirm(
            f"⚠️ {a_plugin} returned 0 tasks but we have {mapped_count} mapped. "
            "This looks like a connection issue. Proceed and DELETE all tasks on the other side?",
            abort=True,
        )

    for uid in all_uuids:
        eid_a = mgr.get_external_id(a_plugin, uid)
        eid_b = mgr.get_external_id(b_plugin, uid)
        t_a, t_b = tasks_a.get(eid_a), tasks_b.get(eid_b)
        last_state = mgr.get_sync_state(uid)

        # A. Handle Completion (Tombstoning)
        if last_state and not (t_a and t_b):
            if not t_a and t_b:  # Finished on A
                if b.delete_task(eid_b):
                    mgr.set_status(uid, "completed")
            elif t_a and not t_b:  # Finished on B
                if a.delete_task(eid_a):
                    mgr.set_status(uid, "completed")
            continue

        # B. RETAINED: Conflict & Modification Detection
        source, target_plugin, target_eid, target_filter = None, None, None, None

        if t_a and not eid_b:  # New on A -> B
            source, target_plugin, target_eid, target_filter = t_a, b, None, b_filter
        elif t_b and not eid_a:  # New on B -> A
            source, target_plugin, target_eid, target_filter = t_b, a, None, a_filter
        elif t_a and t_b:  # Conflict Check
            dirty_a = t_a.get_content_hash() != (last_state["hash"] if last_state else None)
            dirty_b = t_b.get_content_hash() != (last_state["hash"] if last_state else None)

            if dirty_a and dirty_b:
                # RETAINED: Git-based 3-way merge
                base_task = TaskCIR.from_dict(last_state["data"]) if last_state else None
                mergedata = resolve_conflict_via_git(base_task, t_a, t_b)
                source = t_a.copy()
                source.update_from(mergedata)
                if source.get_content_hash() != t_a.get_content_hash():
                    a.update_task(eid_a, source, a_filter)
                source = t_b.copy()
                source.update_from(mergedata)
                if source.get_content_hash() != t_b.get_content_hash():
                    b.update_task(eid_b, source, b_filter)
                mgr.update_sync_state(source)
                # After a merge, we usually want to ensure links are full
                pending_links.append((a, eid_a, source))
                pending_links.append((b, eid_b, source))
                continue
            elif dirty_a:
                source, target_plugin, target_eid, target_filter = t_a, b, eid_b, b_filter
            elif dirty_b:
                source, target_plugin, target_eid, target_filter = t_b, a, eid_a, a_filter
            else:
                continue  # Clean

        if source:
            logging.debug(f"{source=} {target_plugin=} {target_eid=} {target_filter=}")
            available_remote_ids = []
            needs_second_pass = False
            for dep_uid in source.depends:
                remote_id = mgr.get_external_id(target_plugin.name, dep_uid)
                if remote_id:
                    available_remote_ids.append(remote_id)
                else:
                    needs_second_pass = True

            # Temp task for Pass 1 (links only if they exist on target)
            pass1_task = source.copy()
            pass1_task.depends = available_remote_ids

            new_eid = target_plugin.update_task(target_eid, pass1_task, target_filter)

            if not target_eid:
                mgr.create_mapping(target_plugin.name, new_eid, uid)
                target_eid = new_eid

            mgr.update_sync_state(source)
            if needs_second_pass:
                pending_links.append((target_plugin, target_eid, source))

    # 3. PASS 2: Supplemental Links
    if pending_links:
        typer.echo(f"🔗 Resolving {len(pending_links)} pending relationships...")
        for plugin, tool_uid, task in pending_links:
            plugin.update_relationships(tool_uid, task, mgr)


def resolve_conflict_via_git(base_task: Optional[TaskCIR], p1_task: TaskCIR, p2_task: TaskCIR) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init", "-q"], cwd=tmpdir)
        filename = "TASK.json"
        full_merged_path = os.path.join(tmpdir, filename)

        def get_git_hash(task_obj: TaskCIR | None) -> str:
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


from tabulate import tabulate

config_app = typer.Typer(help="Manage UTS and Plugin configurations.")
app.add_typer(config_app, name="config")


@config_app.command("list")
def config_list() -> None:
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


@app.command()
def init():
    """Initialize the mapping database and tables."""
    from .db import MappingManager

    mgr = MappingManager()
    # If your class has an explicit init method:
    # mgr.initialize_tables()
    typer.secho("✅ Database initialized successfully.", fg="green")


def translate_and_discover(plugin, task_dict, id_list, mgr):
    """Translates Tool IDs to UUIDs; discovers unknown tasks via API."""
    internal_uuids = []
    for tid in id_list:
        # 1. Search DB (includes 'completed' status items)
        uid = mgr.get_internal_uuid(plugin.name, str(tid))

        # 2. On-Demand Discovery if totally unknown
        if not uid:
            raw = plugin.fetch_one(str(tid))
            if raw:
                cif = plugin.to_cif(raw)
                uid = mgr.ensure_mapping(plugin.name, cif.tool_uid)
                cif.uuid = uid
                task_dict[cif.tool_uid] = cif
        if uid:
            internal_uuids.append(uid)
    return internal_uuids


if __name__ == "__main__":
    app()
