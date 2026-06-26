"""
Database write operations — restore, merge, create, and full recovery pipeline.

All functions are UI-agnostic. They return typed result dataclasses and never
call print() or input(). Both the TUI and Headless frontends consume these
functions identically.

**Safety Strategy — Backup-First + SQLite ACID**:

  1. A full ``shutil.copy2()`` backup is **always** created before any write.
  2. Modifications are written directly to the live database using SQLite's
     own transactional ``conn.commit()`` — which is inherently ACID-safe.
  3. On failure, the backup is preserved and its path is returned so the user
     can manually restore if needed.

  This avoids the ``os.replace()`` pattern which triggers ``[WinError 5]
  Access Denied`` on Windows when the IDE or antivirus holds a handle.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import time
import urllib.parse
from typing import Callable, Optional

from .constants import PB_KEY, JSON_KEY, BACKUP_PREFIX, DB_FILENAME
from .models import (
    DatabaseSnapshot, MergeDiff, MergeResult, RestoreResult, RecoveryResult,
    RepairResult,
)
from .db_scanner import (
    extract_existing_metadata, extract_workspace_count, scan_database,
    discover_backups, scan_all, list_conversations,
)
from .protobuf import ProtobufEncoder
from .artifacts import ArtifactParser


# ==============================================================================
# WORKSPACE HELPER (absorbed from cli.py)
# ==============================================================================

def build_workspace_dict(path: str) -> dict[str, str]:
    """
    Constructs the standardized dictionary of workspace configuration strings
    required by the Protobuf schema (Fields 9 and 17) mapping.
    """
    import sys
    import urllib.parse
    path_normalized = path.replace("\\", "/").rstrip("/")
    if sys.platform.startswith("win") and len(path_normalized) >= 2 and path_normalized[1] == ":":
        path_normalized = path_normalized[0].lower() + path_normalized[1:]
    
    folder_name = os.path.basename(path_normalized) or "RecoveredProject"

    uri_plain = f"file:///{path_normalized}"
    # Use raw colon instead of percent-encoded colon to match VS Code's runtime representation
    uri_encoded = uri_plain

    return {
        "uri_encoded": uri_encoded,
        "uri_plain": uri_plain,
        "corpus": f"local/{folder_name}",
        "git_remote": f"https://github.com/local/{folder_name}.git",
        "branch": "main",
    }



# ==============================================================================
# BACKUP OPERATIONS
# ==============================================================================

def create_backup(db_path: str, reason: str = "manual") -> str:
    """
    Creates a timestamped safety copy of the target database.

    Returns:
        The absolute path to the backup file.

    Raises:
        OSError: If the copy fails.
    """
    backup_path = f"{db_path}.{BACKUP_PREFIX}_{int(time.time())}_{reason}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def restore_backup(backup_path: str, target_path: str) -> RestoreResult:
    """
    Safely restores a backup database over the current live database.

    1. Creates a 'pre_restore_' safety snapshot of the current DB.
    2. Copies the backup directly over the live file via ``shutil.copy2()``.
    """
    safety_path = ""
    if not os.path.isfile(backup_path):
        return RestoreResult(success=False, error=f"Backup file not found: {backup_path}")
    try:
        # 1. Safety snapshot of current DB before overwriting
        if os.path.isfile(target_path):
            safety_path = create_backup(target_path, reason="before_restore")

        # 2. Overwrite the live DB directly
        shutil.copy2(backup_path, target_path)

        return RestoreResult(success=True, safety_snapshot_path=safety_path)
    except Exception as exc:
        return RestoreResult(success=False, safety_snapshot_path=safety_path,
                            error=str(exc))


def create_empty_db(target_path: str) -> bool:
    """
    Creates a new state.vscdb with the correct schema but empty ItemTable.

    Returns:
        True on success, False on failure.
    """
    conn = None
    try:
        conn = sqlite3.connect(target_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==============================================================================
# MERGE OPERATIONS
# ==============================================================================

def _extract_conversation_ids(db_path: str) -> set[str]:
    """Extracts all conversation UUIDs from a database's PB blob."""
    ids: set[str] = set()
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        row = cur.fetchone()
        if row and row[0]:
            decoded = base64.b64decode(row[0])
            _, inner_blobs = extract_existing_metadata(decoded)
            ids = set(inner_blobs.keys())
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return ids


def compute_merge_diff(source_path: str, target_path: str) -> MergeDiff:
    """Compares two databases and classifies their conversations with full metadata."""
    source_ids = _extract_conversation_ids(source_path)
    target_ids = _extract_conversation_ids(target_path)

    source_convs = {c.uuid: c for c in list_conversations(source_path)}
    target_convs = {c.uuid: c for c in list_conversations(target_path)}

    source_only_uuids = sorted(source_ids - target_ids)
    shared_uuids = sorted(source_ids & target_ids)

    source_only_entries = [source_convs[u] for u in source_only_uuids if u in source_convs]
    shared_entries = [
        (source_convs[u], target_convs[u])
        for u in shared_uuids
        if u in source_convs and u in target_convs
    ]

    return MergeDiff(
        source_only=source_only_uuids,
        target_only=sorted(target_ids - source_ids),
        shared=shared_uuids,
        source_total=len(source_ids),
        target_total=len(target_ids),
        source_only_entries=source_only_entries,
        shared_entries=shared_entries,
    )


def execute_merge(source_path: str, target_path: str,
                  strategy: str = "additive") -> MergeResult:
    """
    Merges conversations from source into target.

    Strategies:
      - ``additive``: Only add conversations missing from target (safe).
      - ``overwrite``: Replace target entries with source entries (destructive).

    Safety: Always creates a pre-merge backup of the target first.
    Uses SQLite ACID transactions to write directly.
    """
    backup_path = ""

    # 1. Create backup FIRST — before any reads/writes
    try:
        backup_path = create_backup(target_path, reason="before_merge")
    except OSError as exc:
        return MergeResult(success=False, error=f"Backup failed: {exc}")

    src_conn = None
    tgt_conn = None
    try:
        # 2. Read source PB + JSON (read-only connection)
        src_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=5)
        src_cur = src_conn.cursor()

        src_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        src_pb_row = src_cur.fetchone()
        src_pb_decoded = base64.b64decode(src_pb_row[0]) if (src_pb_row and src_pb_row[0]) else b""
        src_titles, src_blobs = extract_existing_metadata(src_pb_decoded) if src_pb_decoded else ({}, {})

        src_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        src_json_row = src_cur.fetchone()
        src_json = json.loads(src_json_row[0]) if (src_json_row and src_json_row[0]) else {"version": 1, "entries": {}}

        # 3. Open target for read-write
        tgt_conn = sqlite3.connect(target_path, timeout=10)
        tgt_cur = tgt_conn.cursor()

        tgt_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        tgt_pb_row = tgt_cur.fetchone()
        tgt_pb_decoded = base64.b64decode(tgt_pb_row[0]) if (tgt_pb_row and tgt_pb_row[0]) else b""
        tgt_titles, tgt_blobs = extract_existing_metadata(tgt_pb_decoded) if tgt_pb_decoded else ({}, {})

        tgt_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        tgt_json_row = tgt_cur.fetchone()
        tgt_json = json.loads(tgt_json_row[0]) if (tgt_json_row and tgt_json_row[0]) else {"version": 1, "entries": {}}

        # 4. Merge logic
        added, updated, skipped = 0, 0, 0
        merged_blobs = dict(tgt_blobs)
        merged_titles = dict(tgt_titles)
        merged_json_entries = dict(tgt_json.get("entries", {}))

        for cid, src_blob in src_blobs.items():
            if cid not in merged_blobs:
                merged_blobs[cid] = src_blob
                merged_titles[cid] = src_titles.get(cid, f"Merged {cid[:8]}")
                if cid in src_json.get("entries", {}):
                    merged_json_entries[cid] = src_json["entries"][cid]
                else:
                    merged_json_entries[cid] = {
                        "sessionId": cid,
                        "title": merged_titles.get(cid, f"Merged {cid[:8]}"),
                        "lastModified": int(time.time() * 1000),
                        "isStale": False,
                    }
                added += 1
            elif strategy == "overwrite":
                merged_blobs[cid] = src_blob
                if cid in src_titles:
                    merged_titles[cid] = src_titles[cid]
                if cid in src_json.get("entries", {}):
                    merged_json_entries[cid] = src_json["entries"][cid]
                updated += 1
            else:
                skipped += 1

        # 5. Rebuild PB blob (sorted by lastModified descending)
        sorted_cids = sorted(
            merged_blobs.keys(),
            key=lambda k: merged_json_entries.get(k, {}).get("lastModified", 0),
            reverse=True,
        )
        result_bytes = b""
        for cid in sorted_cids:
            blob = merged_blobs[cid]
            title = merged_titles.get(cid, f"Conversation {cid[:8]}")
            entry = ProtobufEncoder.build_trajectory_entry(
                cid, title, None, int(time.time()), int(time.time()),
                existing_inner_data=blob,
            )
            result_bytes += entry

        encoded_pb = base64.b64encode(result_bytes).decode("utf-8")

        # 6. Write directly to the target DB (SQLite ACID commit)
        tgt_cur.execute("SELECT value FROM ItemTable WHERE key=?", (PB_KEY,))
        if tgt_cur.fetchone():
            tgt_cur.execute("UPDATE ItemTable SET value=? WHERE key=?", (encoded_pb, PB_KEY))
        else:
            tgt_cur.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (PB_KEY, encoded_pb))

        # Sort merged entries by lastModified descending before serializing
        sorted_merged_entries = {}
        sorted_keys = sorted(
            merged_json_entries.keys(),
            key=lambda k: merged_json_entries[k].get("lastModified", 0),
            reverse=True
        )
        for k in sorted_keys:
            sorted_merged_entries[k] = merged_json_entries[k]
        merged_json = {"version": 1, "entries": sorted_merged_entries}
        tgt_cur.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (JSON_KEY, json.dumps(merged_json, ensure_ascii=False)),
        )

        tgt_conn.commit()

        return MergeResult(success=True, added=added, updated=updated,
                          skipped=skipped, backup_path=backup_path)
    except Exception as exc:
        return MergeResult(success=False, error=str(exc), backup_path=backup_path)
    finally:
        for c in (src_conn, tgt_conn):
            if c:
                try:
                    c.close()
                except Exception:
                    pass


def execute_selective_merge(source_path: str, target_path: str,
                            selected_uuids: list[str],
                            strategy: str = "additive") -> MergeResult:
    """
    Cherry-pick merge: only merges the specified conversation UUIDs from source into target.
    Uses the same backup-first + ACID strategy as ``execute_merge``.
    """
    if not selected_uuids:
        return MergeResult(success=True, added=0, updated=0, skipped=0)
    backup_path = ""
    try:
        backup_path = create_backup(target_path, reason="before_merge")
    except OSError as exc:
        return MergeResult(success=False, error=f"Backup failed: {exc}")

    src_conn = None
    tgt_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=5)
        src_cur = src_conn.cursor()

        src_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        src_pb_row = src_cur.fetchone()
        src_pb_decoded = base64.b64decode(src_pb_row[0]) if (src_pb_row and src_pb_row[0]) else b""
        src_titles, src_blobs = extract_existing_metadata(src_pb_decoded) if src_pb_decoded else ({}, {})

        src_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        src_json_row = src_cur.fetchone()
        src_json = json.loads(src_json_row[0]) if (src_json_row and src_json_row[0]) else {"version": 1, "entries": {}}

        tgt_conn = sqlite3.connect(target_path, timeout=10)
        tgt_cur = tgt_conn.cursor()

        tgt_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        tgt_pb_row = tgt_cur.fetchone()
        tgt_pb_decoded = base64.b64decode(tgt_pb_row[0]) if (tgt_pb_row and tgt_pb_row[0]) else b""
        tgt_titles, tgt_blobs = extract_existing_metadata(tgt_pb_decoded) if tgt_pb_decoded else ({}, {})

        tgt_cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        tgt_json_row = tgt_cur.fetchone()
        tgt_json = json.loads(tgt_json_row[0]) if (tgt_json_row and tgt_json_row[0]) else {"version": 1, "entries": {}}

        added, updated, skipped = 0, 0, 0
        merged_blobs = dict(tgt_blobs)
        merged_titles = dict(tgt_titles)
        merged_json_entries = dict(tgt_json.get("entries", {}))

        selected_set = set(selected_uuids)

        for cid, src_blob in src_blobs.items():
            if cid not in selected_set:
                continue

            if cid not in merged_blobs:
                merged_blobs[cid] = src_blob
                merged_titles[cid] = src_titles.get(cid, f"Merged {cid[:8]}")
                if cid in src_json.get("entries", {}):
                    merged_json_entries[cid] = src_json["entries"][cid]
                else:
                    merged_json_entries[cid] = {
                        "sessionId": cid,
                        "title": merged_titles.get(cid, f"Merged {cid[:8]}"),
                        "lastModified": int(time.time() * 1000),
                        "isStale": False,
                    }
                added += 1
            elif strategy == "overwrite":
                merged_blobs[cid] = src_blob
                if cid in src_titles:
                    merged_titles[cid] = src_titles[cid]
                if cid in src_json.get("entries", {}):
                    merged_json_entries[cid] = src_json["entries"][cid]
                updated += 1
            else:
                skipped += 1

        # Sort merged entries by lastModified descending before serializing
        sorted_keys = sorted(
            merged_json_entries.keys(),
            key=lambda k: merged_json_entries[k].get("lastModified", 0),
            reverse=True
        )

        result_bytes = b""
        for cid in sorted_keys:
            if cid not in merged_blobs:
                continue
            blob = merged_blobs[cid]
            title = merged_titles.get(cid, f"Conversation {cid[:8]}")
            entry = ProtobufEncoder.build_trajectory_entry(
                cid, title, None, int(time.time()), int(time.time()),
                existing_inner_data=blob,
            )
            result_bytes += entry

        encoded_pb = base64.b64encode(result_bytes).decode("utf-8")

        tgt_cur.execute("SELECT value FROM ItemTable WHERE key=?", (PB_KEY,))
        if tgt_cur.fetchone():
            tgt_cur.execute("UPDATE ItemTable SET value=? WHERE key=?", (encoded_pb, PB_KEY))
        else:
            tgt_cur.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (PB_KEY, encoded_pb))

        sorted_merged_entries = {k: merged_json_entries[k] for k in sorted_keys}
        merged_json = {"version": 1, "entries": sorted_merged_entries}
        tgt_cur.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (JSON_KEY, json.dumps(merged_json, ensure_ascii=False)),
        )

        tgt_conn.commit()

        return MergeResult(success=True, added=added, updated=updated,
                          skipped=skipped, backup_path=backup_path)
    except Exception as exc:
        return MergeResult(success=False, error=str(exc), backup_path=backup_path)
    finally:
        for c in (src_conn, tgt_conn):
            if c:
                try:
                    c.close()
                except Exception:
                    pass


# ==============================================================================
# DATABASE METADATA EXTRACTIONS FOR NEW FORMAT
# ==============================================================================

def extract_workspace_from_db(db_file_path: str) -> str:
    """Extracts file:/// workspace URI from the trajectory_metadata_blob table."""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_file_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob';")
        if not cur.fetchone():
            return ""
        cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main';")
        row = cur.fetchone()
        if row and row[0]:
            blob = row[0]
            idx = blob.find(b"file:///")
            if idx != -1:
                sub = blob[idx:]
                for i in range(len(sub)):
                    if sub[i] < 32 or sub[i] > 126:
                        sub = sub[:i]
                        break
                import re
                uri = sub.decode('utf-8', errors='ignore')
                return re.sub(
                    r'^(file:///)(\w)(%3A|:)',
                    lambda m: m.group(1) + m.group(2).lower() + m.group(3),
                    uri,
                    flags=re.IGNORECASE,
                )
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return ""


def extract_title_from_db(db_file_path: str) -> str | None:
    """Extracts first user prompt from steps table to use as title."""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_file_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='steps';")
        if not cur.fetchone():
            return None
        cur.execute("SELECT step_payload FROM steps WHERE idx = 0;")
        row = cur.fetchone()
        if row and row[0]:
            payload = row[0]
            text = payload.decode('utf-8', errors='ignore')
            import re
            segments = re.findall(r'[a-zA-Z0-9_/\.:%\-+@[\] ]{20,}', text)
            for seg in segments:
                seg = seg.strip()
                if "file:///" in seg:
                    continue
                if re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', seg):
                    continue
                if len(seg) > 20:
                    seg = re.sub(r'^[^a-zA-Z0-9]+', '', seg)
                    if len(seg) > 50:
                        return seg[:47] + "..."
                    return seg
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None


def extract_workspace_from_db_steps(db_file_path: str) -> str:
    """Extracts step payloads from database steps table, concatenated."""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_file_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='steps';")
        if not cur.fetchone():
            return ""
        cur.execute("SELECT step_payload FROM steps ORDER BY idx ASC;")
        rows = cur.fetchall()
        payloads = []
        for row in rows:
            val = row[0]
            if val:
                if isinstance(val, bytes):
                    payloads.append(val.decode('utf-8', errors='ignore'))
                else:
                    payloads.append(str(val))
        return "\n".join(payloads)
    except Exception:
        return ""
    finally:
        if conn:
            conn.close()


# ==============================================================================
# TITLE RESOLUTION
# ==============================================================================

def resolve_title(cid: str, existing_titles: dict[str, str],
                  brain_dir: str, convs_dir: str) -> tuple[str, str]:
    """
    Determines the best title for a conversation.

    Priority: brain artifact > preserved DB title > db prompt > timestamp fallback.

    Returns:
        (title, source_label) where source_label is 'brain', 'preserved', 'db', or 'fallback'.
    """
    brain_title = ArtifactParser.extract_title(cid, brain_dir)
    if brain_title:
        return brain_title, "brain"

    if cid in existing_titles:
        title = existing_titles[cid].strip()
        if not (title.startswith("{") or title.startswith("Conversation (") or title.startswith("Conversation ")):
            return title, "preserved"

    db_path = os.path.join(convs_dir, f"{cid}.db")
    if os.path.exists(db_path):
        db_title = extract_title_from_db(db_path)
        if db_title:
            return db_title, "db"

    pb_path = os.path.join(convs_dir, f"{cid}.pb")
    active_path = pb_path if os.path.exists(pb_path) else db_path
    if os.path.exists(active_path):
        mod_time = time.strftime("%b %d", time.localtime(os.path.getmtime(active_path)))
        return f"Conversation ({mod_time}) {cid[:8]}", "fallback"

    return f"Conversation {cid[:8]}", "fallback"


# ==============================================================================
# FULL RECOVERY PIPELINE (Phases 1-6)
# ==============================================================================

def _safe_rollback(backup_path: str, target_path: str) -> None:
    """
    Last-resort rollback: copies the pre-recovery backup back over the DB.
    Never raises — this is a best-effort safety net.
    """
    try:
        shutil.copy2(backup_path, target_path)
    except Exception:
        pass


def find_matching_workspace(hint_or_blob) -> str | None:
    if not hint_or_blob:
        return None
    import urllib.parse
    import re
    
    if isinstance(hint_or_blob, bytes):
        try:
            text = hint_or_blob.decode('utf-8', errors='ignore')
        except Exception:
            return None
    else:
        text = str(hint_or_blob)
        
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    home_lower = home.lower()
        
    def get_workspace_base(path: str) -> str | None:
        path_clean = path.replace("\\", "/").rstrip("/")
        bases = [
            f"{home_lower}/antigravityprojects",
            f"{home_lower}/documents/antigravity"
        ]
        for base in bases:
            path_lower = path_clean.lower()
            if path_lower.startswith(base):
                rel = path_lower[len(base):].lstrip("/")
                parts = rel.split("/")
                if parts and parts[0]:
                    project_name = parts[0]
                    prefix_len = len(base) + 1 + len(project_name)
                    return path[:prefix_len]
        return None

    def validate_dir(path: str) -> str | None:
        ws_base = get_workspace_base(path)
        if not ws_base:
            return None
        if os.path.isdir(ws_base):
            return ws_base
        return None

    # Get local workspaces map
    ws_paths = {}
    fallbacks = {
        "moduledesigncriteria": f"{home}/AntigravityProjects/moduledesigncriteria",
        "conversationtest": f"{home}/AntigravityProjects/conversationtest",
        "playground": f"{home}/AntigravityProjects/playground",
        "hermes": f"{home}/AntigravityProjects/hermes",
        "glassworm_scanner": f"{home}/AntigravityProjects/glassworm_scanner",
        "glassworm_hunter": f"{home}/AntigravityProjects/glassworm_hunter",
    }
    for name, path in fallbacks.items():
        ws_paths[name.lower()] = path
        
    bases = [
        os.path.join(os.path.expanduser("~"), "AntigravityProjects"),
        os.path.join(os.path.expanduser("~"), "Documents", "antigravity")
    ]
    for base in bases:
        if os.path.isdir(base):
            try:
                for name in os.listdir(base):
                    p = os.path.join(base, name)
                    if os.path.isdir(p) and not name.startswith('.'):
                        ws_paths[name.lower()] = p
            except Exception:
                pass

    # Dynamic scanning of IDE workspaceStorage directory (salvaged from legacy diagnostic scripts)
    try:
        from .environment import EnvironmentResolver
        ws_storage_dir = EnvironmentResolver.get_workspace_storage_path()
        if os.path.isdir(ws_storage_dir):
            for entry in os.listdir(ws_storage_dir):
                ws_json = os.path.join(ws_storage_dir, entry, "workspace.json")
                if os.path.isfile(ws_json):
                    try:
                        import json
                        with open(ws_json, "r", encoding="utf-8", errors="ignore") as fh:
                            data = json.load(fh)
                        uri = data.get("folder") or data.get("workspace")
                        if uri and uri.startswith("file:///"):
                            raw_path = uri[len("file:///"):]
                            decoded_path = urllib.parse.unquote(raw_path)
                            clean_path = decoded_path.replace("\\", "/").rstrip("/")
                            name = clean_path.split("/")[-1]
                            if name:
                                ws_paths[name.lower()] = clean_path
                    except Exception:
                        pass
    except Exception:
        pass

    text_lower = text.lower()

    # 1. Semantic Topic Keyword mapping first (high confidence)
    topic_mappings = {
        "mitosis": "playground",
        "calculator": "playground",
        "black-scholes": "playground",
        "nous portal": "hermes",
        "nous_portal": "hermes",
        "nous": "hermes",
        "hermes": "hermes",
        "options trader": "hermes",
        "webui": "hermes",
        "vps": "hermes",
        "hindsight": "hermes",
        "deduplication": "hermes",
        "memory compaction": "hermes",
        "discord.py": "hermes",
        "discord bot": "hermes",
        "ssh": "hermes",
        "glassworm scanner": "glassworm_scanner",
        "glassworm hunter": "glassworm_hunter",
        "glassworm": "glassworm_scanner",
        "moduledesigncriteria": "moduledesigncriteria",
        "design criteria": "moduledesigncriteria",
        "migration": "moduledesigncriteria",
        "recovery": "moduledesigncriteria",
        "daemon": "moduledesigncriteria",
        "conversationtest": "conversationtest",
        "playground": "playground",
    }
    for keyword, proj_name in topic_mappings.items():
        if keyword in text_lower:
            target_path = ws_paths.get(proj_name)
            if target_path:
                validated = validate_dir(target_path)
                if validated:
                    return validated

    # 2. Direct URI extraction
    uri_match = re.search(r"file:///([^\s\"'\]>)]+)", text)
    if uri_match:
        raw_uri_path = urllib.parse.unquote(uri_match.group(1))
        clean_path = raw_uri_path.replace("\\", "/")
        if len(clean_path) >= 2 and clean_path[1] == "%":
            clean_path = clean_path.replace("%3A", ":").replace("%3a", ":")
        
        validated = validate_dir(clean_path)
        if validated:
            return validated

    # 3. Direct absolute path extraction
    path_match = re.search(r"([A-Za-z]:\\[^\s\"'\]>)]+)", text)
    if not path_match:
        path_match = re.search(r"([A-Za-z]:/[^\s\"'\]>)]+)", text)
    if path_match:
        raw_path = path_match.group(1)
        validated = validate_dir(raw_path)
        if validated:
            return validated

    # 4. Generic project folder name matching
    for name in sorted(ws_paths.keys(), key=len, reverse=True):
        if len(name) < 4:
            pattern = r"\b" + re.escape(name) + r"\b"
            if re.search(pattern, text_lower):
                validated = validate_dir(ws_paths[name])
                if validated:
                    return validated
        else:
            if name in text_lower:
                validated = validate_dir(ws_paths[name])
                if validated:
                    return validated

    return None



def _extract_pb_modify_timestamp(inner_data: bytes) -> int | None:
    """
    Extract the modification timestamp (field 7, sub-field 1 = seconds)
    from a Protobuf inner blob. Falls back to field 10 if field 7 is absent.
    Returns epoch seconds or None.
    """
    try:
        pos = 0
        best_ts = None
        while pos < len(inner_data):
            tag, pos = ProtobufEncoder.decode_varint(inner_data, pos)
            wire_type = tag & 7
            field_num = tag >> 3
            if wire_type == 2:
                length, pos = ProtobufEncoder.decode_varint(inner_data, pos)
                content = inner_data[pos:pos + length]
                pos += length
                if field_num in (7, 10):
                    # Parse timestamp sub-message: field 1 = seconds
                    sub_pos = 0
                    while sub_pos < len(content):
                        sub_tag, sub_pos = ProtobufEncoder.decode_varint(content, sub_pos)
                        sub_fn = sub_tag >> 3
                        sub_wt = sub_tag & 7
                        if sub_fn == 1 and sub_wt == 0:
                            val, sub_pos = ProtobufEncoder.decode_varint(content, sub_pos)
                            if field_num == 7:
                                return val  # Prefer field 7 (last modified)
                            if best_ts is None:
                                best_ts = val  # Field 10 as fallback
                        else:
                            sub_pos = ProtobufEncoder.skip_protobuf_field(content, sub_pos, sub_wt)
            elif wire_type == 0:
                _, pos = ProtobufEncoder.decode_varint(inner_data, pos)
            elif wire_type == 1:
                pos += 8
            elif wire_type == 5:
                pos += 4
            else:
                break
        return best_ts
    except Exception:
        return None


def run_recovery_pipeline(
    db_path: str,
    convs_dir: str,
    brain_dir: str,
    ws_assignments: dict[str, dict[str, str]] | None = None,
    on_progress: Optional[Callable[[str, str], None]] = None,
) -> RecoveryResult:
    """
    Executes the full 6-phase recovery pipeline.

    Safety: Creates a full backup before any write, then writes directly
    to the database using SQLite ACID transactions. On failure, rolls back
    from the backup automatically.
    """
    if ws_assignments is None:
        ws_assignments = {}

    def _progress(phase: str, msg: str) -> None:
        if on_progress:
            on_progress(phase, msg)

    # Phase 2: Discovery
    _progress("discovery", "Listing conversation files...")
    try:
        raw_files = os.listdir(convs_dir)
    except OSError as exc:
        return RecoveryResult(success=False, error=f"Cannot read conversations dir: {exc}")

    def get_mtime(f):
        for ext in [".pb", ".db"]:
            p = os.path.join(convs_dir, f"{f}{ext}")
            if os.path.exists(p):
                return os.path.getmtime(p)
        return 0

    all_cids = sorted(
        list({f[:-3] for f in raw_files if f.endswith(".pb") or f.endswith(".db")}),
        key=get_mtime,
        reverse=True,
    )

    if not all_cids:
        return RecoveryResult(success=True, conversations_rebuilt=0)

    _progress("discovery", f"Found {len(all_cids)} conversation(s). Extracting metadata...")

    existing_titles: dict[str, str] = {}
    existing_inner_blobs: dict[str, bytes] = {}
    meta_conn = None
    try:
        meta_conn = sqlite3.connect(db_path)
        cur = meta_conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key=?", (PB_KEY,))
        row = cur.fetchone()

        if row and row[0]:
            decoded = base64.b64decode(row[0])
            existing_titles, existing_inner_blobs = extract_existing_metadata(decoded)
    except Exception:
        pass
    finally:
        if meta_conn:
            try:
                meta_conn.close()
            except Exception:
                pass


    # Auto-assign workspaces from brain artifacts / DB metadata / titles
    for cid in all_cids:
        # Priority 1: Extract workspace URI from DB trajectory metadata (highest confidence)
        db_path_file = os.path.join(convs_dir, f"{cid}.db")
        if os.path.exists(db_path_file):
            inferred = extract_workspace_from_db(db_path_file)
            matched_ws = find_matching_workspace(inferred)
            if matched_ws:
                ws_assignments[cid] = build_workspace_dict(matched_ws)
                continue

        # Priority 2: Check existing inner blob
        inner = existing_inner_blobs.get(cid)
        if inner:
            inferred = ProtobufEncoder.extract_workspace_hint(inner)
            matched_ws = find_matching_workspace(inferred)
            if matched_ws:
                ws_assignments[cid] = build_workspace_dict(matched_ws)
                continue

        # Priority 3: Infer from brain artifacts
        inferred = ArtifactParser.infer_workspace_from_brain(cid, brain_dir)
        matched_ws = find_matching_workspace(inferred)
        if matched_ws:
            ws_assignments[cid] = build_workspace_dict(matched_ws)
            continue

        # Priority 4: Scan DB steps payloads
        if os.path.exists(db_path_file):
            steps_content = extract_workspace_from_db_steps(db_path_file)
            matched_ws = find_matching_workspace(steps_content)
            if matched_ws:
                ws_assignments[cid] = build_workspace_dict(matched_ws)
                continue

        # Priority 5: Fall back to conversation title keyword matching
        title, _ = resolve_title(cid, existing_titles, brain_dir, convs_dir)
        matched_ws = find_matching_workspace(title)
        if matched_ws:
            ws_assignments[cid] = build_workspace_dict(matched_ws)
            continue

    # Fallback: assign dominant workspace to remaining unmapped conversations
    if ws_assignments:
        from collections import Counter
        ws_counts = Counter(v["uri_plain"] for v in ws_assignments.values())
        if ws_counts:
            dominant_uri = ws_counts.most_common(1)[0][0]
            dominant_dict = next(v for v in ws_assignments.values() if v["uri_plain"] == dominant_uri)
            for cid in all_cids:
                if cid not in ws_assignments:
                    inner = existing_inner_blobs.get(cid)
                    existing_hint = ProtobufEncoder.extract_workspace_hint(inner) if inner else None
                    if not existing_hint or not find_matching_workspace(existing_hint):
                        ws_assignments[cid] = dominant_dict

    # Resolve titles and build entries
    _progress("injection", "Building Protobuf entries...")
    resolved: list[tuple[str, str, str, Optional[bytes], bool]] = []
    stats = {"brain": 0, "preserved": 0, "db": 0, "fallback": 0}

    for cid in all_cids:
        title, source = resolve_title(cid, existing_titles, brain_dir, convs_dir)
        inner_data = existing_inner_blobs.get(cid)
        has_ws = bool(inner_data and ProtobufEncoder.extract_workspace_hint(inner_data))
        resolved.append((cid, title, source, inner_data, has_ws))
        stats[source] = stats.get(source, 0) + 1

    # Phase 4: Backup — ALWAYS before any writes
    _progress("backup", "Creating safety backup...")
    try:
        backup_path = create_backup(db_path, reason="before_recovery")
    except OSError as exc:
        return RecoveryResult(success=False, error=f"Backup failed: {exc}")

    # Phase 5: Injection — write directly using SQLite ACID
    _progress("injection", "Injecting into database...")
    result_bytes = b""
    ws_total = 0
    ts_injected = 0
    stats_json: dict[str, int] = {"json_added": 0, "json_patched": 0, "json_deleted": 0}

    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()

        cur.execute("SELECT value FROM ItemTable WHERE key=?", (JSON_KEY,))
        idx_row = cur.fetchone()
        try:
            chat_idx = json.loads(idx_row[0]) if idx_row else {"version": 1, "entries": {}}
        except (json.JSONDecodeError, TypeError):
            chat_idx = {"version": 1, "entries": {}}

        # Collect entries with their effective timestamps for sorting
        built_entries: list[tuple[int, bytes]] = []  # (effective_mtime, entry_bytes)

        for cid, title, source, inner_data, has_ws in resolved:
            ws_map = ws_assignments.get(cid)
            pb_path = os.path.join(convs_dir, f"{cid}.pb")
            db_path_file = os.path.join(convs_dir, f"{cid}.db")
            active_path = pb_path if os.path.exists(pb_path) else db_path_file

            pb_mtime = int(os.path.getmtime(active_path)) if os.path.exists(active_path) else int(time.time())
            pb_ctime = int(os.path.getctime(active_path)) if os.path.exists(active_path) else int(time.time())

            entry = ProtobufEncoder.build_trajectory_entry(
                cid, title, ws_map, pb_ctime, pb_mtime, existing_inner_data=inner_data
            )

            # Determine effective timestamp for sorting:
            # Use file modification time (mtime) as the sort key. On Windows,
            # mtime reflects the last time the conversation was written to,
            # which is the correct semantic for "last modified" sort ordering.
            # Note: ctime on Windows is the file creation date, which does NOT
            # update when a conversation is modified — using it causes stale
            # entries to appear out of order in the sidebar.
            #
            # Fallback: If file mtime appears batch-corrupted (many files
            # sharing the same second from a migration run), use the brain
            # directory's newest artifact timestamp as a tiebreaker.
            effective_ts = pb_mtime

            # Check brain dir for a more accurate timestamp
            brain_conv_dir = os.path.join(brain_dir, cid)
            if os.path.isdir(brain_conv_dir):
                try:
                    brain_newest = 0
                    for root, _dirs, _files in os.walk(brain_conv_dir):
                        for fname in _files:
                            fpath = os.path.join(root, fname)
                            try:
                                mt = int(os.path.getmtime(fpath))
                                if mt > brain_newest:
                                    brain_newest = mt
                            except OSError:
                                pass
                    if brain_newest > 0 and brain_newest != effective_ts:
                        # Use brain timestamp if it's more specific (differs
                        # from file mtime, suggesting the file was batch-touched)
                        effective_ts = brain_newest
                except Exception:
                    pass

            built_entries.append((effective_ts, entry))

            if has_ws or ws_map:
                ws_total += 1
            if pb_mtime and (not inner_data or not ProtobufEncoder.has_timestamp_fields(inner_data)):
                ts_injected += 1

            mtime_ms = effective_ts * 1000
            if cid not in chat_idx.setdefault("entries", {}):
                chat_idx["entries"][cid] = {
                    "sessionId": cid,
                    "title": title,
                    "lastModified": mtime_ms,
                    "isStale": False,
                }
                stats_json["json_added"] += 1
            else:
                chat_idx["entries"][cid]["title"] = title
                chat_idx["entries"][cid]["lastModified"] = mtime_ms
                stats_json["json_patched"] += 1

        # Sort PB entries by effective timestamp descending (newest first)
        # This determines sidebar display order
        built_entries.sort(key=lambda x: x[0], reverse=True)
        result_bytes = b"".join(entry_bytes for _, entry_bytes in built_entries)

        # Prune JSON orphans safely
        valid_cids = {r[0] for r in resolved}
        entries = chat_idx.get("entries")
        if isinstance(entries, dict):
            stale_cids = [c for c in entries if c not in valid_cids]
            for stale in stale_cids:
                del entries[stale]
            stats_json["json_deleted"] = len(stale_cids)

            # Sort the entries in JSON index by lastModified descending (newest first)
            sorted_entries = {}
            sorted_keys = sorted(
                entries.keys(),
                key=lambda k: entries[k].get("lastModified", 0),
                reverse=True,
            )
            for k in sorted_keys:
                sorted_entries[k] = entries[k]
            chat_idx["entries"] = sorted_entries
        else:
            stats_json["json_deleted"] = 0
            
        encoded_pb = base64.b64encode(result_bytes).decode("utf-8")

        cur.execute("SELECT value FROM ItemTable WHERE key=?", (PB_KEY,))
        if cur.fetchone():
            cur.execute("UPDATE ItemTable SET value=? WHERE key=?", (encoded_pb, PB_KEY))
        else:
            cur.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (PB_KEY, encoded_pb))

        cur.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (JSON_KEY, json.dumps(chat_idx, ensure_ascii=False)),
        )

        conn.commit()

        _progress("done", "Recovery complete.")

        return RecoveryResult(
            success=True,
            conversations_rebuilt=len(resolved),
            workspaces_mapped=ws_total,
            timestamps_injected=ts_injected,
            json_added=stats_json["json_added"],
            json_patched=stats_json["json_patched"],
            json_deleted=stats_json["json_deleted"],
            backup_path=backup_path,
        )

    except sqlite3.Error as exc:
        _safe_rollback(backup_path, db_path)
        return RecoveryResult(
            success=False,
            error=f"SQLite error (rolled back): {exc}",
            backup_path=backup_path,
        )
    except Exception as exc:
        _safe_rollback(backup_path, db_path)
        return RecoveryResult(
            success=False,
            error=f"Unexpected error (rolled back): {exc}",
            backup_path=backup_path,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==============================================================================
# DATA INSPECTION & MANAGEMENT
# ==============================================================================

def get_conversation_payload(db_path: str, target_uuid: str) -> str:
    """Extracts and pretty-prints the JSON payload for a given conversation."""
    if not os.path.isfile(db_path):
        return "Database file not found."
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        row = cur.fetchone()
        conn.close()
        
        if row and row[0]:
            j_obj = json.loads(row[0])
            entries = j_obj.get("entries", {})
            if target_uuid in entries:
                return json.dumps(entries[target_uuid], indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error reading payload: {e}"
    
    return "No JSON payload found for this conversation."


def delete_conversation(db_path: str, conv_uuid: str) -> bool:
    """Safely removes a single conversation from PB and JSON indices."""
    if not os.path.isfile(db_path):
        return False
    conn = None
    try:
        create_backup(db_path, reason="before_conv_del")

        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()

        # 1. Update JSON
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        j_row = cur.fetchone()
        if j_row and j_row[0]:
            j_obj = json.loads(j_row[0])
            if "entries" in j_obj and conv_uuid in j_obj["entries"]:
                del j_obj["entries"][conv_uuid]
                cur.execute("UPDATE ItemTable SET value = ? WHERE key = ?",
                           (json.dumps(j_obj, ensure_ascii=False), JSON_KEY))

        # 2. Update PB
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        pb_row = cur.fetchone()
        if pb_row and pb_row[0]:
            decoded = base64.b64decode(pb_row[0])
            titles, inner_blobs = extract_existing_metadata(decoded)

            if conv_uuid in inner_blobs:
                result_bytes = b""
                for cid, blob in inner_blobs.items():
                    if cid == conv_uuid:
                        continue
                    title = titles.get(cid, f"Conversation {cid[:8]}")
                    entry = ProtobufEncoder.build_trajectory_entry(
                        cid, title, None, int(time.time()), int(time.time()), existing_inner_data=blob
                    )
                    result_bytes += entry

                encoded_pb = base64.b64encode(result_bytes).decode("utf-8")
                cur.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (encoded_pb, PB_KEY))

        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def rename_conversation(db_path: str, conv_uuid: str, new_title: str) -> bool:
    """Safely renames a conversation in both JSON and PB indices."""
    if not os.path.isfile(db_path):
        return False
    if not new_title or not new_title.strip():
        return False
    conn = None
    try:
        create_backup(db_path, reason="before_conv_rename")

        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()

        # 1. Update JSON
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (JSON_KEY,))
        j_row = cur.fetchone()
        if j_row and j_row[0]:
            j_obj = json.loads(j_row[0])
            if "entries" in j_obj and conv_uuid in j_obj["entries"]:
                j_obj["entries"][conv_uuid]["title"] = new_title
                cur.execute("UPDATE ItemTable SET value = ? WHERE key = ?",
                           (json.dumps(j_obj, ensure_ascii=False), JSON_KEY))

        # 2. Update PB
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        pb_row = cur.fetchone()
        if pb_row and pb_row[0]:
            decoded = base64.b64decode(pb_row[0])
            titles, inner_blobs = extract_existing_metadata(decoded)

            if conv_uuid in inner_blobs:
                result_bytes = b""
                for cid, blob in inner_blobs.items():
                    title = new_title if cid == conv_uuid else titles.get(cid, f"Conversation {cid[:8]}")
                    entry = ProtobufEncoder.build_trajectory_entry(
                        cid, title, None, int(time.time()), int(time.time()), existing_inner_data=blob
                    )
                    result_bytes += entry

                encoded_pb = base64.b64encode(result_bytes).decode("utf-8")
                cur.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (encoded_pb, PB_KEY))

        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def migrate_workspace(db_path: str, new_workspace_path: str) -> bool:
    """Migrates all conversations in the database to a new workspace path."""
    if not os.path.isfile(db_path):
        return False
    if not new_workspace_path or not new_workspace_path.strip():
        return False
    conn = None
    try:
        create_backup(db_path, reason="before_ws_migrate")

        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()

        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        pb_row = cur.fetchone()

        if pb_row and pb_row[0]:
            decoded = base64.b64decode(pb_row[0])
            titles, inner_blobs = extract_existing_metadata(decoded)

            ws_map = build_workspace_dict(new_workspace_path)
            result_bytes = b""

            for cid, blob in inner_blobs.items():
                title = titles.get(cid, f"Conversation {cid[:8]}")
                entry = ProtobufEncoder.build_trajectory_entry(
                    cid, title, ws_map, int(time.time()), int(time.time()), existing_inner_data=blob
                )
                result_bytes += entry

            encoded_pb = base64.b64encode(result_bytes).decode("utf-8")
            cur.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (encoded_pb, PB_KEY))

            conn.commit()

        return True
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==============================================================================
# AUTONOMOUS REPAIR
# ==============================================================================

def repair_database(db_path: str) -> RepairResult:
    """
    Autonomously repairs known Protobuf corruptions in a state.vscdb file.

    Strategy:
      1. Creates a full backup before any modifications.
      2. Reads the raw PB blob and parses each entry.
      3. For each entry, runs the diagnostic scanner.
      4. Corrupted entries are surgically rebuilt: salvageable data (title,
         UUID, workspace, timestamps) is extracted and re-encoded through
         the fixed ProtobufEncoder.
      5. Clean entries are preserved byte-for-byte.
      6. The repaired blob is written back via SQLite ACID.
    """
    if not os.path.isfile(db_path):
        return RepairResult(success=False, error="Database file not found")

    from .diagnostic import diagnose_database, GHOST_BYTES, DOUBLE_WRAP, UUID_MISMATCH

    # Run diagnosis first
    report = diagnose_database(db_path)
    if report.error:
        return RepairResult(success=False, error=f"Diagnosis failed: {report.error}")
    if report.is_healthy:
        return RepairResult(
            success=True, entries_scanned=report.total_entries,
            entries_preserved=report.total_entries,
        )

    # Create backup before any writes
    try:
        backup_path = create_backup(db_path, reason="before_repair")
    except OSError as exc:
        return RepairResult(success=False, error=f"Backup failed: {exc}")

    read_conn = None
    try:
        read_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        cur = read_conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (PB_KEY,))
        row = cur.fetchone()

        decoded = base64.b64decode(row[0])
    except Exception as exc:
        return RepairResult(success=False, error=f"Read failed: {exc}", backup_path=backup_path)
    finally:
        if read_conn:
            try:
                read_conn.close()
            except Exception:
                pass

    # Build set of corrupt UUIDs
    corrupt_uuids = {e.uuid for e in report.entry_diagnostics if e.is_corrupt or e.has_warnings}

    # Parse and selectively rebuild
    repaired_bytes = b""
    stats = {"repaired": 0, "preserved": 0, "ghosts": 0, "wraps": 0, "uuids": 0}
    pos = 0

    while pos < len(decoded):
        entry_start = pos
        try:
            tag, pos = ProtobufEncoder.decode_varint(decoded, pos)
        except Exception:
            break
        wire_type = tag & 7
        if wire_type != 2:
            break
        length, pos = ProtobufEncoder.decode_varint(decoded, pos)
        raw_entry = decoded[pos:pos + length]
        pos += length

        # Extract UUID from the entry
        uid = _extract_uuid_from_entry(raw_entry)

        if uid and uid in corrupt_uuids:
            # Salvage and rebuild
            salvaged = _salvage_entry(raw_entry)
            if salvaged:
                title, ws_uri, create_ts, modify_ts = salvaged
                ws_dict = build_workspace_dict(ws_uri) if ws_uri else None
                rebuilt = ProtobufEncoder.build_trajectory_entry(
                    uid, title, ws_dict,
                    create_ts or int(time.time()),
                    modify_ts or int(time.time()),
                )
                repaired_bytes += rebuilt
                stats["repaired"] += 1

                # Count what we fixed
                entry_diag = next((e for e in report.entry_diagnostics if e.uuid == uid), None)
                if entry_diag:
                    for f in entry_diag.findings:
                        if f.corruption_type == GHOST_BYTES:
                            stats["ghosts"] += 1
                        elif f.corruption_type == DOUBLE_WRAP:
                            stats["wraps"] += 1
                        elif f.corruption_type == UUID_MISMATCH:
                            stats["uuids"] += 1
            else:
                # Cannot salvage — preserve original
                repaired_bytes += decoded[entry_start:pos]
                stats["preserved"] += 1
        else:
            # Clean entry — preserve byte-for-byte
            repaired_bytes += decoded[entry_start:pos]
            stats["preserved"] += 1

    # Write repaired blob
    try:
        encoded_pb = base64.b64encode(repaired_bytes).decode("utf-8")
        wconn = sqlite3.connect(db_path, timeout=10)
        wcur = wconn.cursor()
        wcur.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (encoded_pb, PB_KEY))
        wconn.commit()
        wconn.close()
    except Exception as exc:
        _safe_rollback(backup_path, db_path)
        return RepairResult(success=False, error=f"Write failed (rolled back): {exc}",
                            backup_path=backup_path)

    return RepairResult(
        success=True,
        entries_scanned=report.total_entries,
        entries_repaired=stats["repaired"],
        entries_preserved=stats["preserved"],
        ghost_bytes_stripped=stats["ghosts"],
        double_wraps_fixed=stats["wraps"],
        uuid_mismatches_fixed=stats["uuids"],
        backup_path=backup_path,
    )


def _extract_uuid_from_entry(raw_entry: bytes) -> str:
    """Extract conversation UUID from a raw TrajectorySummary entry."""
    # Handle potential double-wrapping by unwrapping Field 1 layers
    data = raw_entry
    try:
        t, ep = ProtobufEncoder.decode_varint(data, 0)
        if (t >> 3) == 1 and (t & 7) == 2:
            l, ep = ProtobufEncoder.decode_varint(data, ep)
            if ep + l == len(data):  # Single Field 1 wrap — unwrap
                data = data[ep:ep + l]
    except Exception:
        pass

    try:
        pos = 0
        while pos < len(data):
            tag, pos = ProtobufEncoder.decode_varint(data, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                l, pos = ProtobufEncoder.decode_varint(data, pos)
                content = data[pos:pos + l]
                pos += l
                if fn == 1:
                    return content.decode('utf-8', errors='strict')
            elif wt == 0:
                _, pos = ProtobufEncoder.decode_varint(data, pos)
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return ""


def _salvage_entry(raw_entry: bytes) -> tuple[str, str, int, int] | None:
    """
    Extracts salvageable metadata from a potentially corrupted entry.

    Returns (title, workspace_uri, create_epoch, modify_epoch) or None.
    """
    # Unwrap double-wrapping layers
    data = raw_entry
    for _ in range(2):  # At most 2 unwrap attempts
        try:
            t, ep = ProtobufEncoder.decode_varint(data, 0)
            if (t >> 3) == 1 and (t & 7) == 2:
                l, ep = ProtobufEncoder.decode_varint(data, ep)
                if ep + l == len(data):
                    data = data[ep:ep + l]
                else:
                    break
            else:
                break
        except Exception:
            break

    # Parse TrajectorySummary fields to get into the base64 payload
    try:
        pos = 0
        while pos < len(data):
            tag, pos = ProtobufEncoder.decode_varint(data, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                l, pos = ProtobufEncoder.decode_varint(data, pos)
                content = data[pos:pos + l]
                pos += l
                if fn == 2:
                    # This is the Base64Wrapper — extract the base64 string
                    sp = 0
                    _, sp = ProtobufEncoder.decode_varint(content, sp)
                    sl, sp = ProtobufEncoder.decode_varint(content, sp)
                    b64_str = content[sp:sp + sl].decode('utf-8', errors='ignore')
                    # Filter out any U+FFFD before decoding
                    b64_clean = b64_str.replace('\ufffd', '')
                    try:
                        inner = base64.b64decode(b64_clean)
                    except Exception:
                        return None
                    return _parse_trajectory_payload(inner)
            elif wt == 0:
                _, pos = ProtobufEncoder.decode_varint(data, pos)
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return None


def _parse_trajectory_payload(inner: bytes) -> tuple[str, str, int, int] | None:
    """Parse a TrajectoryPayload to extract title, workspace URI, and timestamps."""
    title = "Recovered"
    ws_uri = ""
    create_ts = 0
    modify_ts = 0

    try:
        pos = 0
        while pos < len(inner):
            tag, pos = ProtobufEncoder.decode_varint(inner, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                l, pos = ProtobufEncoder.decode_varint(inner, pos)
                content = inner[pos:pos + l]
                pos += l
                if fn == 1:
                    try:
                        title = content.decode('utf-8', errors='strict')
                    except UnicodeDecodeError:
                        title = "Recovered"
                elif fn == 3:  # created_at Timestamp
                    create_ts = _extract_timestamp_seconds(content)
                elif fn == 7:  # updated_at Timestamp
                    modify_ts = _extract_timestamp_seconds(content)
                elif fn == 9:  # WorkspaceInfo
                    ws_uri = _extract_workspace_uri_from_field9(content)
            elif wt == 0:
                _, pos = ProtobufEncoder.decode_varint(inner, pos)
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass

    return (title, ws_uri, create_ts, modify_ts)


def _extract_timestamp_seconds(ts_msg: bytes) -> int:
    """Extract the seconds field (Field 1) from a Timestamp message."""
    try:
        pos = 0
        while pos < len(ts_msg):
            tag, pos = ProtobufEncoder.decode_varint(ts_msg, pos)
            fn, wt = tag >> 3, tag & 7
            if fn == 1 and wt == 0:
                val, _ = ProtobufEncoder.decode_varint(ts_msg, pos)
                return val
            pos = ProtobufEncoder.skip_protobuf_field(ts_msg, pos, wt)
    except Exception:
        pass
    return 0


def _extract_workspace_uri_from_field9(ws_msg: bytes) -> str:
    """Extract the primary URI (Field 1) from a WorkspaceInfo message."""
    try:
        pos = 0
        while pos < len(ws_msg):
            tag, pos = ProtobufEncoder.decode_varint(ws_msg, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                l, pos = ProtobufEncoder.decode_varint(ws_msg, pos)
                content = ws_msg[pos:pos + l]
                pos += l
                if fn == 1:
                    uri = content.decode('utf-8', errors='ignore')
                    if uri.startswith('file:///'):
                        # Decode the URI to get an OS path
                        import urllib.parse
                        raw = uri.replace('file:///', '')
                        decoded_path = urllib.parse.unquote(raw)
                        return decoded_path
            elif wt == 0:
                _, pos = ProtobufEncoder.decode_varint(ws_msg, pos)
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return ""
