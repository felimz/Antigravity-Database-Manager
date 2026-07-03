"""
Interactive headless controller — standard print()/input() menus.

Provides 100% feature parity with the TUI for users who cannot or
choose not to use the full-screen interface.
"""

from __future__ import annotations

import os
import sys

from ..core.lifecycle import ApplicationContext
from ..core import db_operations as ops
from ..core.db_scanner import scan_all, format_snapshot_table, list_conversations, health_check, analyze_workspaces
from ..core import storage_manager as sm
from .logger import Logger


def run_interactive(ctx: ApplicationContext) -> int:
    """
    Launch the full interactive headless experience.
    Returns an exit code.
    """
    Logger.banner()

    # Pre-flight warnings
    warnings = ctx.perform_preflight_checks()
    for w in warnings:
        Logger.warn(w)

    if ctx.ide_running:
        try:
            ans = input("  The IDE appears to be running. Proceed? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return 0
        if ans != "y":
            Logger.info("Aborted.")
            return 0

    while True:
        print()
        print("=" * 60)
        print("  AGMERCIUM DB MANAGER — Main Menu")
        print("=" * 60)
        print()
        print("  [1]  Scan & Compare Databases")
        print("  [2]  Restore a Backup")
        print("  [3]  Run Full Recovery Pipeline")
        print("  [4]  Merge Two Databases")
        print("  [5]  Create Empty Database")
        print("  [6]  Create Manual Backup")
        print("  [7]  Browse Conversations")
        print("  [8]  Health Check")
        print("  [9]  Workspace Diagnostics")
        print("  [10] Manage Storage.json")
        print("  [Q]  Quit")
        print()

        try:
            choice = input("  Your choice: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if choice == "1":
            _menu_scan(ctx)
        elif choice == "2":
            _menu_restore(ctx)
        elif choice == "3":
            _menu_recover(ctx)
        elif choice == "4":
            _menu_merge(ctx)
        elif choice == "5":
            _menu_create(ctx)
        elif choice == "6":
            _menu_backup(ctx)
        elif choice == "7":
            _menu_browse(ctx)
        elif choice == "8":
            _menu_health(ctx)
        elif choice == "9":
            _menu_workspaces(ctx)
        elif choice == "10":
            _menu_storage(ctx)
        elif choice in ("q", ""):
            break
        else:
            Logger.warn(f"Invalid choice: '{choice}'")

    Logger.info("Goodbye.")
    return 0


# ==============================================================================
# MENU IMPLEMENTATIONS
# ==============================================================================

def _menu_scan(ctx: ApplicationContext) -> None:
    """Display the scan/compare table."""
    Logger.header("Database Scanner")
    snapshots = scan_all(ctx.db_path)
    for line in format_snapshot_table(snapshots):
        print(line)
    print()
    _pause()


def _menu_restore(ctx: ApplicationContext) -> None:
    """Interactive backup restore."""
    Logger.header("Restore a Backup")
    snapshots = scan_all(ctx.db_path)
    backup_snaps = [s for s in snapshots if not s.is_current]

    if not backup_snaps:
        Logger.info("No backups found.")
        _pause()
        return

    for line in format_snapshot_table(snapshots):
        print(line)
    print()

    try:
        raw = input(f"  Enter backup # to restore (1-{len(backup_snaps)}, or Enter to cancel): ").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if not raw:
        return

    try:
        idx = int(raw)
    except ValueError:
        Logger.warn("Invalid number.")
        return

    if idx < 1 or idx > len(backup_snaps):
        Logger.warn(f"Index out of range. Must be 1-{len(backup_snaps)}.")
        return

    selected = backup_snaps[idx - 1]
    if selected.error:
        Logger.warn(f"That backup has an error: {selected.error}")
        return

    Logger.info(f"Selected: [{idx}] {selected.label}")
    Logger.info(f"  Conversations: {selected.conversation_count}  |  "
                f"Titled: {selected.titled_count}  |  "
                f"Workspaces: {selected.workspace_count}")

    try:
        confirm = input("  Restore this backup? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return

    if confirm != "y":
        Logger.info("Cancelled.")
        return

    result = ops.restore_backup(selected.path, ctx.db_path)
    if result.success:
        Logger.success("Backup restored successfully.")
        if result.safety_snapshot_path:
            Logger.info(f"Safety snapshot at: {result.safety_snapshot_path}")
    else:
        Logger.error(f"Restore failed: {result.error}")


def _menu_recover(ctx: ApplicationContext) -> None:
    """Run the full recovery pipeline with progress output."""
    Logger.header("Full Recovery Pipeline")

    if not os.path.isdir(ctx.convs_dir):
        Logger.error(f"Conversations directory not found: {ctx.convs_dir}")
        _pause()
        return

    result = ops.run_recovery_pipeline(
        ctx.db_path, ctx.convs_dir, ctx.brain_dir,
        on_progress=lambda phase, msg: Logger.info(f"[{phase}] {msg}"),
    )

    if result.success:
        Logger.header("Recovery Complete")
        Logger.success(f"Conversations rebuilt:  {result.conversations_rebuilt}")
        Logger.success(f"Workspaces mapped:     {result.workspaces_mapped}")
        Logger.success(f"Timestamps injected:   {result.timestamps_injected}")
        Logger.success(f"JSON entries added:    {result.json_added}")
        Logger.success(f"JSON entries patched:  {result.json_patched}")
        Logger.success(f"JSON entries deleted:  {result.json_deleted}")
        Logger.info(f"Backup at: {result.backup_path}")
    else:
        Logger.error(f"Recovery failed: {result.error}")

    _pause()


def _menu_merge(ctx: ApplicationContext) -> None:
    """Interactive merge wizard with per-conversation diff."""
    Logger.header("Merge Databases")

    try:
        source = input("  Source database path: ").strip().strip('"').strip("'")
    except (KeyboardInterrupt, EOFError):
        return

    if not source or not os.path.isfile(source):
        Logger.warn("File not found or empty path.")
        return

    # Show enriched diff
    diff = ops.compute_merge_diff(source, ctx.db_path)
    print()
    Logger.info(f"Source: {diff.source_total} conversations")
    Logger.info(f"Target: {diff.target_total} conversations")
    Logger.info(f"  New (source only):  {len(diff.source_only)}")
    Logger.info(f"  Shared:             {len(diff.shared)}")
    Logger.info(f"  Target only:        {len(diff.target_only)}")
    print()

    if diff.source_only_entries:
        Logger.header("New Conversations (source only)")
        for e in diff.source_only_entries[:20]:
            print(f"  + {e.uuid[:8]}...  {e.title}")
        if len(diff.source_only_entries) > 20:
            print(f"  ... and {len(diff.source_only_entries) - 20} more")
        print()

    print("  Strategy:")
    print("    [1] Additive — only add missing conversations (safe)")
    print("    [2] Overwrite — replace shared entries with source (destructive)")
    try:
        strat_choice = input("  Choice (1): ").strip()
    except (KeyboardInterrupt, EOFError):
        return

    strategy = "overwrite" if strat_choice == "2" else "additive"

    try:
        confirm = input(f"  Merge using '{strategy}' strategy? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return

    if confirm != "y":
        Logger.info("Merge cancelled.")
        return

    result = ops.execute_merge(source, ctx.db_path, strategy)
    if result.success:
        Logger.success(f"Merge complete: +{result.added} added, ~{result.updated} updated, ={result.skipped} skipped")
        Logger.info(f"Backup at: {result.backup_path}")
    else:
        Logger.error(f"Merge failed: {result.error}")

    _pause()


def _menu_create(ctx: ApplicationContext) -> None:
    """Create an empty database."""
    Logger.header("Create Empty Database")
    try:
        path = input("  Output path: ").strip().strip('"').strip("'")
    except (KeyboardInterrupt, EOFError):
        return

    if not path:
        Logger.warn("No path provided.")
        return

    if ops.create_empty_db(path):
        Logger.success(f"Created empty database: {path}")
    else:
        Logger.error("Failed to create database.")

    _pause()


def _menu_backup(ctx: ApplicationContext) -> None:
    """Create a manual backup."""
    Logger.header("Create Backup")
    try:
        backup_path = ops.create_backup(ctx.db_path, reason="manual")
        Logger.success(f"Backup created: {backup_path}")
    except OSError as exc:
        Logger.error(f"Backup failed: {exc}")
    _pause()


def _browse_conversation_detail(ctx: ApplicationContext, sel) -> None:
    """Display detail and actions for a single conversation."""
    Logger.header(f"Conversation: {sel.title}")
    print(f"  UUID: {sel.uuid}")
    print(f"  Workspace: {sel.workspace_uri}")
    print(f"  Timestamps: {'Yes' if sel.has_timestamps else 'No'}")
    print(f"  JSON Synced: {'Yes' if sel.json_synced else 'No'}")
    print()
    act = input("  [V]iew Payload  [D]elete  [R]ename  [Enter] Back: ").strip().lower()
    if act == 'v':
        print(ops.get_conversation_payload(ctx.db_path, sel.uuid))
    elif act == 'd':
        if ops.delete_conversation(ctx.db_path, sel.uuid):
            Logger.success("Deleted successfully.")
        else:
            Logger.error("Failed to delete.")
    elif act == 'r':
        new_title = input("  New title: ").strip()
        if new_title and ops.rename_conversation(ctx.db_path, sel.uuid, new_title):
            Logger.success("Renamed successfully.")
        else:
            Logger.error("Failed to rename.")


def _menu_browse(ctx: ApplicationContext) -> None:
    """Browse and manage conversations."""
    Logger.header("Browse Conversations")
    convs = list_conversations(ctx.db_path)
    if not convs:
        Logger.info("No conversations found.")
        _pause()
        return

    for i, c in enumerate(convs[:20]):
        ws_str = f" [{c.workspace_uri}]" if c.workspace_uri else ""
        print(f"  [{i+1:>2}] {c.uuid[:8]}...  {c.title[:40]}{ws_str}")
    
    if len(convs) > 20:
        print(f"  ... and {len(convs) - 20} more. Enter 'n' to see next page.")
        
    print()
    try:
        idx_str = input("  Select conversation # to inspect, 'n' for next page, or Enter to go back: ").strip()
        if not idx_str:
            return
        if idx_str.lower() == 'n' and len(convs) > 20:
            # Show remaining conversations in pages of 20
            page = 1
            while page * 20 < len(convs):
                start = page * 20
                end = min(start + 20, len(convs))
                for i, c in enumerate(convs[start:end], start=start):
                    ws_str = f" [{c.workspace_uri}]" if c.workspace_uri else ""
                    print(f"  [{i+1:>2}] {c.uuid[:8]}...  {c.title[:40]}{ws_str}")
                if end < len(convs):
                    print(f"  ... and {len(convs) - end} more.")
                page_choice = input("  Select # to inspect, 'n' for next page, or Enter to go back: ").strip()
                if not page_choice:
                    _pause()
                    return
                if page_choice.lower() == 'n':
                    page += 1
                    continue
                idx = int(page_choice) - 1
                if 0 <= idx < len(convs):
                    sel = convs[idx]
                    _browse_conversation_detail(ctx, sel)
                else:
                    Logger.warn("Invalid selection.")
                _pause()
                return
            _pause()
            return
        idx = int(idx_str) - 1
        if 0 <= idx < len(convs):
            sel = convs[idx]
            _browse_conversation_detail(ctx, sel)
        else:
            Logger.warn("Invalid selection.")
    except (KeyboardInterrupt, EOFError, ValueError):
        pass
    _pause()


def _menu_health(ctx: ApplicationContext) -> None:
    """Display database health check."""
    Logger.header("Health Check")
    snapshots = scan_all(ctx.db_path)
    if not snapshots:
        Logger.error("No database found.")
        _pause()
        return
        
    current = snapshots[0]
    report = health_check(current)
    
    Logger.info(f"Target: {current.path}")
    Logger.info(f"Size: {current.size_bytes / (1024*1024):.1f} MB")
    Logger.info(f"Sync Status: {report.sync_status}")
    Logger.info(f"Conversations: {current.conversation_count}")
    Logger.info(f"Titled: {current.titled_count} ({report.titled_pct:.1f}%)")
    Logger.info(f"Workspaces: {current.workspace_count}")
    Logger.info(f"JSON Entries: {current.json_entry_count}")
    Logger.info(f"Orphaned Data: {'Yes' if report.has_orphans else 'No'}")
    print()
    Logger.success(f"Summary: {report.summary}")
    print()
    _pause()


def _menu_workspaces(ctx: ApplicationContext) -> None:
    """Workspace diagnostics."""
    Logger.header("Workspace Diagnostics")
    diagnostics = analyze_workspaces(ctx.db_path)
    if not diagnostics:
        Logger.info("No workspaces found.")
        _pause()
        return

    healthy = 0
    for d in diagnostics:
        if d.exists_on_disk and d.is_accessible:
            icon = "✓"
            healthy += 1
        elif d.exists_on_disk:
            icon = "⚠"
        else:
            icon = "✗"
        print(f"  {icon} {d.decoded_path}  ({len(d.bound_conversations)} convs)")

    print()
    Logger.info(f"Total: {len(diagnostics)} workspaces, {healthy} healthy")
    missing = len(diagnostics) - healthy
    if missing:
        Logger.warn(f"{missing} workspace(s) have issues.")
    _pause()


def _menu_storage(ctx: ApplicationContext) -> None:
    """Manage storage.json interactively."""
    Logger.header("Storage.json Manager")
    storage_dir = os.path.dirname(ctx.db_path)
    data = sm.read_storage(storage_dir)

    if not data:
        Logger.info("storage.json is empty or not found.")
        _pause()
        return

    entries = sm.flatten_keys(data)
    Logger.info(f"Found {len(entries)} keys.")
    print()

    for i, e in enumerate(entries[:30]):
        print(f"  [{i+1:>3}] {e.key}  [{e.value_type}]  {e.value_preview}")
    if len(entries) > 30:
        print(f"  ... and {len(entries) - 30} more")
    print()

    try:
        act = input("  [B]ackup  [P]atch key  [D]elete key  [Enter] Back: ").strip().lower()
        if act == 'b':
            bp = sm.write_storage(storage_dir, data, reason="manual_backup")
            Logger.success(f"Backup created: {bp}")
        elif act == 'p':
            key = input("  Key (dotted path): ").strip()
            value = input("  New value: ").strip()
            if key and value:
                try:
                    sm.patch_key(data, key, value)
                    sm.write_storage(storage_dir, data, reason="manual_patch")
                    Logger.success(f"Patched '{key}' = '{value}'")
                except KeyError as exc:
                    Logger.error(str(exc))
        elif act == 'd':
            key = input("  Key (dotted path): ").strip()
            if key:
                try:
                    sm.delete_key(data, key)
                    sm.write_storage(storage_dir, data, reason="manual_delete")
                    Logger.success(f"Deleted '{key}'")
                except KeyError as exc:
                    Logger.error(str(exc))
    except (KeyboardInterrupt, EOFError):
        pass
    _pause()


def _pause() -> None:
    """Wait for user to press Enter."""
    try:
        input("  Press Enter to continue...")
    except (KeyboardInterrupt, EOFError):
        pass


def run_prune_interactive(ctx: ApplicationContext, report: dict[str, dict[str, Any]]) -> None:
    """Interactive CLI menu for selecting and deleting conversations or artifacts."""
    while True:
        # Re-calculate total sizes
        total_db_pb = sum(item["db_pb_size"] for item in report.values())
        total_brain = sum(item["brain_size"] for item in report.values())
        total_cache = total_db_pb + total_brain

        Logger.header("Cache Pruning Hub")
        print(f"  Total Cache Size: {total_cache / (1024 * 1024):.2f} MB")
        print(f"    - Conversations DB/PB: {total_db_pb / (1024 * 1024):.2f} MB")
        print(f"    - Brain Artifacts:     {total_brain / (1024 * 1024):.2f} MB")
        print()
        print("  Options:")
        print("    [1] Delete Entire Conversations (Select multiple to delete)")
        print("    [2] Prune Large Brain Artifacts (Select visual media/logs/scratch)")
        print("    [3] Auto-Prune cache down to under 500MB (Proposed plan first)")
        print("    [Enter] Back")
        print("=" * 60)

        try:
            choice = input("  Select option: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not choice:
            break

        if choice == "1":
            # List largest conversations
            sorted_convs = sorted(report.items(), key=lambda x: x[1]["db_pb_size"] + x[1]["brain_size"], reverse=True)
            Logger.header("Delete Entire Conversations")
            print("  Top 15 Largest Conversations:")
            print(f"    {'#':>2} | {'UUID Prefix':<11} | {'Total Size':<10} | {'Title':<45}")
            print("-" * 80)
            for idx, (uuid, item) in enumerate(sorted_convs[:15]):
                sz = (item["db_pb_size"] + item["brain_size"]) / (1024 * 1024)
                print(f"    {idx+1:>2} | {uuid[:11]} | {sz:>7.2f} MB | {item['title'][:45]}")
            print()

            try:
                indices_str = input("  Enter indices to delete (comma-separated, e.g., 1,3,5 or [Enter] Cancel): ").strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if not indices_str:
                continue

            # Parse indices
            to_delete = []
            for item in indices_str.split(","):
                try:
                    i = int(item.strip()) - 1
                    if 0 <= i < len(sorted_convs):
                        to_delete.append(sorted_convs[i])
                except ValueError:
                    pass

            if not to_delete:
                Logger.info("No valid indices selected.")
                _pause()
                continue

            print("\nThe following conversations will be DELETED ENTIRELY:")
            for uuid, item in to_delete:
                sz = (item["db_pb_size"] + item["brain_size"]) / (1024 * 1024)
                print(f"  - {uuid} | {sz:.2f} MB | {item['title']}")

            try:
                confirm = input("\nAre you sure you want to permanently delete these? (y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                confirm = "n"

            if confirm == "y":
                saved_bytes = 0
                success_count = 0
                for uuid, item in to_delete:
                    sz = item["db_pb_size"] + item["brain_size"]
                    if ops.purge_conversation_data(ctx.db_path, ctx.convs_dir, ctx.brain_dir, uuid):
                        Logger.success(f"Purged: {item['title'][:50]}")
                        success_count += 1
                        saved_bytes += sz
                        del report[uuid]
                    else:
                        Logger.error(f"Failed to purge: {uuid}")
                Logger.success(f"\nPurged {success_count} conversations. Freed {saved_bytes / (1024*1024):.2f} MB.")
                _pause()

        elif choice == "2":
            # List largest brain directories
            sorted_brains = sorted(
                [x for x in report.items() if x[1]["brain_size"] > 0],
                key=lambda x: x[1]["brain_size"],
                reverse=True
            )
            Logger.header("Prune Large Brain Artifacts")
            print("  Top 15 Largest Brain Folders:")
            print(f"    {'#':>2} | {'UUID Prefix':<11} | {'Brain Size':<10} | {'Media Size':<10} | {'Logs Size':<9} | {'Title'}")
            print("-" * 90)
            for idx, (uuid, item) in enumerate(sorted_brains[:15]):
                br_sz = item["brain_size"] / (1024 * 1024)
                m_sz = item["media_size"] / (1024 * 1024)
                l_sz = item["log_size"] / (1024 * 1024)
                print(f"    {idx+1:>2} | {uuid[:11]} | {br_sz:>7.2f} MB | {m_sz:>7.2f} MB | {l_sz:>6.2f} MB | {item['title'][:40]}")
            print()

            try:
                index_str = input("  Enter index to prune (or [Enter] Cancel): ").strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if not index_str:
                continue

            try:
                i = int(index_str) - 1
                if not (0 <= i < len(sorted_brains)):
                    Logger.error("Invalid index.")
                    _pause()
                    continue
            except ValueError:
                Logger.error("Invalid input.")
                _pause()
                continue

            target_uuid, target_item = sorted_brains[i]
            print(f"\nSelected: {target_item['title']}")
            print("  Pruning options:")
            print("    [1] Prune Media only (webp, png, mp4, etc.)")
            print("    [2] Prune Logs only (jsonl, log, txt)")
            print("    [3] Prune Scratch directory only")
            print("    [4] Prune All non-essential artifacts (keep plan markdowns)")
            print("    [Enter] Cancel")

            try:
                prune_choice = input("  Select prune type: ").strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if prune_choice not in ["1", "2", "3", "4"]:
                continue

            media = prune_choice == "1"
            logs = prune_choice == "2"
            scratch = prune_choice == "3"
            all_non_essential = prune_choice == "4"

            confirm = input("  Confirm pruning? (y/N): ").strip().lower()
            if confirm == "y":
                if all_non_essential:
                    saved = ops.prune_conversation_artifacts(ctx.brain_dir, target_uuid, media_only=False, logs_only=False, scratch_only=False)
                else:
                    saved = ops.prune_conversation_artifacts(ctx.brain_dir, target_uuid, media_only=media, logs_only=logs, scratch_only=scratch)

                Logger.success(f"Pruned successfully. Saved {saved / (1024 * 1024):.2f} MB.")
                # Refresh report metrics for this item
                report = ops.get_cache_size_report(ctx.db_path, ctx.convs_dir, ctx.brain_dir)
                _pause()

        elif choice == "3":
            # Auto-prune to under 500MB
            target_mb = 500
            target_bytes = target_mb * 1024 * 1024
            if total_cache <= target_bytes:
                Logger.success(f"Cache is already under target size of {target_mb} MB.")
                _pause()
                continue

            bytes_to_free = total_cache - target_bytes
            Logger.header(f"Auto-Pruning down to {target_mb} MB")
            print(f"  Current cache size:  {total_cache / (1024 * 1024):.2f} MB")
            print(f"  Target cache size:   {target_mb:.2f} MB")
            print(f"  Required savings:    {bytes_to_free / (1024 * 1024):.2f} MB")
            print()

            # By default, we prune media first across all conversations
            to_delete_entire = set()
            to_prune_artifacts = {}
            saved_so_far = 0

            # 1. Prune all media
            for u in report:
                media_sz = report[u]["media_size"]
                if media_sz > 0:
                    to_prune_artifacts[u] = {"media_only": True}
                    saved_so_far += media_sz

            # 2. Delete oldest conversations if needed
            if total_cache - saved_so_far > target_bytes:
                sorted_by_age = sorted(report.items(), key=lambda x: x[1]["mtime"])
                for u, item in sorted_by_age:
                    if total_cache - saved_so_far <= target_bytes:
                        break
                    if u in to_prune_artifacts:
                        del to_prune_artifacts[u]
                    to_delete_entire.add(u)
                    saved_so_far += (item["db_pb_size"] + item["brain_size"])

            # Print proposal
            if to_delete_entire:
                print("Conversations proposed for DELETION ENTIRELY:")
                for u in to_delete_entire:
                    item = report[u]
                    sz = (item["db_pb_size"] + item["brain_size"]) / (1024 * 1024)
                    print(f"  - {u} | {sz:.2f} MB | {item['title']}")
                print()

            if to_prune_artifacts:
                print("Conversations proposed for MEDIA ARTIFACT PRUNING:")
                for u in to_prune_artifacts:
                    item = report[u]
                    print(f"  - {u} | {item['media_size'] / (1024*1024):.2f} MB | {item['title']}")
                print()

            print(f"Total proposed savings: {saved_so_far / (1024*1024):.2f} MB")
            try:
                confirm = input("\nProceed with auto-pruning? (y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                confirm = "n"

            if confirm == "y":
                actual_saved = 0
                deleted_count = 0
                pruned_count = 0

                for u in to_delete_entire:
                    item = report[u]
                    total_sz = item["db_pb_size"] + item["brain_size"]
                    if ops.purge_conversation_data(ctx.db_path, ctx.convs_dir, ctx.brain_dir, u):
                        deleted_count += 1
                        actual_saved += total_sz
                        del report[u]

                for u in to_prune_artifacts:
                    saved = ops.prune_conversation_artifacts(ctx.brain_dir, u, media_only=True)
                    if saved > 0:
                        pruned_count += 1
                        actual_saved += saved

                # Refresh report metrics
                report = ops.get_cache_size_report(ctx.db_path, ctx.convs_dir, ctx.brain_dir)
                Logger.success(f"\nAuto-pruning complete! Freed {actual_saved / (1024 * 1024):.2f} MB.")
                print(f"  - Conversations deleted: {deleted_count}")
                print(f"  - Conversations pruned:  {pruned_count}")
                _pause()

