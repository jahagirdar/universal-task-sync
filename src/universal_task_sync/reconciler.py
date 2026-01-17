from typing import Set

import typer

from .db import MappingManager
from .models import TaskCIR

reconcile_app = typer.Typer(help="Manually link tasks between services to rebuild mapping memory.")


def _perform_link(mgr: MappingManager, p1: str, p2: str, t1: TaskCIR, t2: TaskCIR):
    """Writes the mapping to the database and initializes sync state."""
    # Link both external IDs to the same internal UUID
    shared_uuid = mgr.create_mapping(p1, t1.tool_uid)
    mgr.create_mapping(p2, t2.tool_uid, shared_uuid)

    # Update state so the next sync sees them as identical
    t1.uuid = shared_uuid
    mgr.update_sync_state(t1)


@reconcile_app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    input_plugin: str = typer.Argument(..., help="Source plugin (e.g., tw)"),
    output_plugin: str = typer.Argument(..., help="Destination plugin (e.g., github)"),
    src_target: str = typer.Option(..., "--src-target", "-s", help="Source target (e.g., project:uts)"),
    dst_target: str = typer.Option(..., "--dst-target", "-d", help="Destination target (e.g., owner/repo)"),
):
    if ctx.invoked_subcommand is not None:
        return

    from .loader import get_plugin

    mgr = MappingManager()
    src = get_plugin(input_plugin)
    dst = get_plugin(output_plugin)

    src.authenticate()
    dst.authenticate()

    typer.secho(f"🚀 Reconciling {input_plugin}:{src_target} <-> {output_plugin}:{dst_target}", fg="cyan")

    # Fetch and transform
    src_tasks = [src.to_cif(r) for r in src.fetch_raw(src_target)]
    dst_tasks = [dst.to_cif(r) for r in dst.fetch_raw(dst_target)]

    linked_dst_ids: Set[str] = set()

    for s_task in src_tasks:
        s_hash = s_task.get_content_hash()

        # 1. Automatic Hash Match
        match = next(
            (d for d in dst_tasks if d.tool_uid not in linked_dst_ids and d.get_content_hash() == s_hash), None
        )

        if match:
            _perform_link(mgr, input_plugin, output_plugin, s_task, match)
            linked_dst_ids.add(match.tool_uid)
            typer.secho(f"✅ Auto-linked: {s_task.description}", fg="green")
            continue

        # 2. Manual Interactive Match
        candidates = [
            d
            for d in dst_tasks
            if d.tool_uid not in linked_dst_ids
            and (
                s_task.description.lower() in d.description.lower()
                or d.description.lower() in s_task.description.lower()
            )
        ]

        if not candidates:
            continue

        typer.echo(f"\nPotential match found for: [ {s_task.description} ]")
        for i, c in enumerate(candidates[:5]):
            typer.echo(f"  {i}) {c.description} ({c.tool_uid[:8]})")

        choice = typer.prompt("Select index to link, (s)kip, or (q)uit", default="s")
        if choice.lower() == "q":
            raise typer.Abort()
        if choice.isdigit() and int(choice) < len(candidates):
            selected = candidates[int(choice)]
            _perform_link(mgr, input_plugin, output_plugin, s_task, selected)
            linked_dst_ids.add(selected.tool_uid)
            typer.secho(f"🔗 Linked: {s_task.description}", fg="blue")

    typer.secho("\n✨ Reconciliation complete. Database is now seeded.", bold=True, fg="magenta")
