import os
import sys
import time
import shutil
import sqlite3
import datetime

# Configure standard output to support UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_ROOT = os.path.dirname(_SCRIPT_DIR)  # Antigravity-Database-Manager/
if _LIB_ROOT not in sys.path:
    sys.path.insert(0, _LIB_ROOT)

from src.core.lifecycle import ApplicationContext
from src.core import db_operations as ops
from src.core import db_scanner as scanner

# Set up logging to console and file
log_file_path = os.path.join(_SCRIPT_DIR, "migration_run.log")

def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{timestamp}] [{level:<7}] {msg}"
    print(formatted_msg)
    try:
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(formatted_msg + "\n")
    except Exception as e:
        print(f"Error writing to log file: {e}")

def main():
    log("==============================================================")
    log("STARTING CONVERSATION MEMORY MIGRATION & RECOVERY PROCESS")
    log("==============================================================")
    log(f"Log file: {log_file_path}")

    # Step 1: Pre-flight Checks and Context Initialization
    log("Initializing application context...")
    try:
        ctx = ApplicationContext()
        ctx.__enter__()
        ctx.perform_preflight_checks()
        log(f"Target Database: {ctx.db_path}")
        log(f"Conversations Directory: {ctx.convs_dir}")
        log(f"Brain Directory: {ctx.brain_dir}")
    except Exception as e:
        log(f"Context initialization failed: {e}", "ERROR")
        sys.exit(1)

    # Step 2: Pre-migration Health Assessment
    log("Performing pre-migration health assessment...")
    try:
        snapshots = scanner.scan_all(ctx.db_path)
        if snapshots:
            curr_snap = snapshots[0]
            report = scanner.health_check(curr_snap)
            log(f"Current DB size: {curr_snap.size_bytes / (1024*1024):.2f} MB")
            log(f"Current DB index state: PB Count={curr_snap.conversation_count}, JSON Count={curr_snap.json_entry_count}")
            log(f"Current DB health: {report.summary}")
        else:
            log("No database snapshots found", "WARNING")
    except Exception as e:
        log(f"Pre-migration health check failed: {e}", "WARNING")

    # Step 3: Count source files
    try:
        files = os.listdir(ctx.convs_dir)
        pb_files = [f for f in files if f.endswith(".pb")]
        db_files = [f for f in files if f.endswith(".db")]
        log(f"Discovered {len(pb_files)} legacy (.pb) conversation files on disk.")
        log(f"Discovered {len(db_files)} new (.db) conversation files on disk.")
        total_source_files = len(pb_files) + len(db_files)
        log(f"Total source conversation files to index: {total_source_files}")
    except Exception as e:
        log(f"Failed to scan source directory: {e}", "ERROR")
        ctx.__exit__(None, None, None)
        sys.exit(1)

    # Step 4: Backup the Database
    pre_migration_backup = ctx.db_path + ".pre_migration_backup"
    log(f"Creating pre-migration database backup: {pre_migration_backup}")
    try:
        if os.path.exists(ctx.db_path):
            shutil.copy2(ctx.db_path, pre_migration_backup)
            log("Database backup completed successfully.")
        else:
            log("Database file does not exist, creating new database in recovery...", "WARNING")
    except Exception as e:
        log(f"Failed to create pre-migration backup: {e}", "ERROR")
        ctx.__exit__(None, None, None)
        sys.exit(1)

    # Step 5: Execute Migration Pipeline
    log("Running database repair to fix any UUID mismatches and corruptions...")
    try:
        repair_res = ops.repair_database(ctx.db_path)
        if repair_res.success:
            log(f"Database repair completed: scanned {repair_res.entries_scanned}, repaired {repair_res.entries_repaired}, preserved {repair_res.entries_preserved}")
        else:
            log(f"Database repair failed: {repair_res.error}", "WARNING")
    except Exception as e:
        log(f"Unhandled exception during database repair: {e}", "WARNING")

    log("Executing recovery pipeline to rebuild indices...")
    success = False
    try:
        result = ops.run_recovery_pipeline(
            ctx.db_path,
            ctx.convs_dir,
            ctx.brain_dir,
            on_progress=lambda phase, msg: log(f"[{phase}] {msg}", "PROGRESS")
        )
        if result.success:
            log("Recovery pipeline completed successfully.")
            log(f"Rebuilt Conversations: {result.conversations_rebuilt}")
            log(f"Workspaces Mapped:    {result.workspaces_mapped}")
            log(f"Timestamps Injected:  {result.timestamps_injected}")
            log(f"JSON Entries Added:   {result.json_added}")
            log(f"JSON Entries Patched: {result.json_patched}")
            log(f"JSON Entries Deleted: {result.json_deleted}")
            if result.backup_path:
                log(f"Pipeline internal backup path: {result.backup_path}")
            success = True
        else:
            log(f"Recovery pipeline failed: {result.error}", "ERROR")
    except Exception as e:
        log(f"Unhandled exception during recovery pipeline: {e}", "ERROR")

    # Step 6: Rollback on Failure or Finalize on Success
    if not success:
        log("Initiating automatic database rollback from backup...", "WARNING")
        try:
            if os.path.exists(pre_migration_backup):
                shutil.copy2(pre_migration_backup, ctx.db_path)
                log("Rollback completed successfully. Original database state restored.")
            else:
                log("Pre-migration backup file not found. Cannot rollback!", "CRITICAL")
        except Exception as e:
            log(f"Rollback operation failed: {e}", "CRITICAL")
        
        ctx.__exit__(None, None, None)
        sys.exit(1)

    # Clean up pre-migration backup since migration succeeded
    try:
        if os.path.exists(pre_migration_backup):
            os.remove(pre_migration_backup)
            log("Pre-migration backup file cleaned up.")
    except Exception as e:
        log(f"Failed to clean up pre-migration backup: {e}", "WARNING")

    # Step 7: Post-migration Verification and Auditing
    log("Performing post-migration verification and health check...")
    try:
        snapshots = scanner.scan_all(ctx.db_path)
        if snapshots:
            curr_snap = snapshots[0]
            report = scanner.health_check(curr_snap)
            log(f"Post-migration DB size: {curr_snap.size_bytes / (1024*1024):.2f} MB")
            log(f"Post-migration DB index state: PB Count={curr_snap.conversation_count}, JSON Count={curr_snap.json_entry_count}")
            log(f"Post-migration DB health: {report.summary}")
            
            # Print audit list of all active conversations and their formats
            convs = scanner.list_conversations(ctx.db_path)
            log(f"Active Indexed Conversations ({len(convs)}):")
            for c in convs:
                db_exists = os.path.exists(os.path.join(ctx.convs_dir, f"{c.uuid}.db"))
                pb_exists = os.path.exists(os.path.join(ctx.convs_dir, f"{c.uuid}.pb"))
                fmt = ".db" if db_exists else (".pb" if pb_exists else "unknown")
                ws_label = f" -> Workspace: {c.workspace_uri}" if c.workspace_uri else " -> No Workspace"
                log(f"  - UUID: {c.uuid} | Format: {fmt:<4} | Title: {c.title}{ws_label}")
        else:
            log("Post-migration snapshot list is empty", "WARNING")
    except Exception as e:
        log(f"Post-migration audit failed: {e}", "WARNING")

    # Step 8: Workspace Diagnostics
    log("Running workspace diagnostics...")
    try:
        diagnostics = scanner.analyze_workspaces(ctx.db_path)
        log(f"Total Unique Workspaces Found: {len(diagnostics)}")
        for d in diagnostics:
            status = "HEALTHY" if d.exists_on_disk and d.is_accessible else "MISSING/INACCESSIBLE"
            log(f"  - Path: {d.decoded_path:<60} | Status: {status:<10} | Bound Conversations: {len(d.bound_conversations)}")
    except Exception as e:
        log(f"Workspace diagnostics failed: {e}", "WARNING")

    log("==============================================================")
    log("CONVERSATION MEMORY MIGRATION & RECOVERY PROCESS COMPLETED SUCCESSFULLY")
    log("==============================================================")

    ctx.__exit__(None, None, None)

if __name__ == "__main__":
    main()
