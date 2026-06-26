# Unified URI Normalization Fix for Antigravity IDE and Standalone App
#
# Normalizes raw-colon URIs (file:///c:/) to percent-encoded (file:///c%3A/)
# across all Antigravity data stores:
#
# IDE targets (~/.gemini/antigravity-ide/):
#   - storage.json, workspaceStorage/*/workspace.json
#   - workbench.desktop.main.js (runtime JS patch)
#   - product.json (checksum update)
#   - state.vscdb (via run_migration.py)
#
# Standalone App targets (~/.gemini/antigravity/):
#   - agyhub_summaries_proto.pb (global trajectory summaries)
#   - conversations/*.db (per-conversation Protobuf metadata blobs)
#
# Usage:
#   python restore_encoded_uris.py               # All repairs (default)
#   python restore_encoded_uris.py --skip-standalone  # IDE only, skip standalone app
#   python restore_encoded_uris.py --standalone-only   # Standalone app only

import os
import sys
import json
import re
import shutil
import time
import subprocess
import sqlite3
import argparse

# Configure standard output to support UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_ROOT = os.path.dirname(_SCRIPT_DIR)  # Antigravity-Database-Manager/
if _LIB_ROOT not in sys.path:
    sys.path.insert(0, _LIB_ROOT)

from src.core.lifecycle import ApplicationContext
from src.core.environment import EnvironmentResolver


# =============================================================================
# Shared URI Normalization Helpers
# =============================================================================

def normalize_uri_to_raw(val):
    """Normalize a single string value: replace file:///X%3A or file:///X: with file:///x:."""
    if isinstance(val, str) and val.startswith("file:///"):
        # Replace percent-encoded colon with raw colon
        val = re.sub(r'^(file:///)([a-zA-Z])%3[Aa](/)', r'\1\2:\3', val)
        # Force lowercase drive letter
        val = re.sub(r'^(file:///)([a-zA-Z]):(/)', lambda m: m.group(1) + m.group(2).lower() + m.group(3), val)
    return val


def normalize_obj_to_raw(obj):
    """Recursively normalize all strings in a JSON-like object to use raw colons."""
    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            new_key = normalize_uri_to_raw(k)
            new_val = normalize_obj_to_raw(v)
            new_dict[new_key] = new_val
        return new_dict
    elif isinstance(obj, list):
        return [normalize_obj_to_raw(x) for x in obj]
    elif isinstance(obj, str):
        return normalize_uri_to_raw(obj)
    return obj



# =============================================================================
# Process Detection
# =============================================================================

def is_ide_running():
    """Check if the Antigravity IDE process is running."""
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity IDE.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return "Antigravity IDE.exe" in res.stdout
    except Exception:
        return False


def is_standalone_running():
    """Check if the Antigravity standalone app process is running."""
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return "Antigravity.exe" in res.stdout
    except Exception:
        return False


# =============================================================================
# IDE Repair Phases (storage.json, workspaceStorage, JS patch, checksums)
# =============================================================================

def patch_storage_json(storage_path):
    """Normalize URIs in the IDE's storage.json."""
    print(f"Reading storage.json from {storage_path}...")
    if not os.path.exists(storage_path):
        print("storage.json not found.")
        return

    backup_path = storage_path + ".backup_before_encoded_fix"
    shutil.copy2(storage_path, backup_path)
    print(f"Created backup of storage.json at: {backup_path}")

    with open(storage_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    normalized_data = normalize_obj_to_raw(data)

    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump(normalized_data, f, indent=2, ensure_ascii=False)
    print("Successfully normalized storage.json workspace paths to raw colons.")


def patch_workspace_storage(workspace_storage_dir):
    """Normalize URIs in all workspaceStorage/*/workspace.json files."""
    print(f"Scanning workspaceStorage directory: {workspace_storage_dir}...")
    if not os.path.isdir(workspace_storage_dir):
        return

    patched_count = 0
    for entry in os.listdir(workspace_storage_dir):
        ws_json = os.path.join(workspace_storage_dir, entry, "workspace.json")
        if os.path.isfile(ws_json):
            try:
                with open(ws_json, "r", encoding="utf-8") as f:
                    wdata = json.load(f)

                normalized_wdata = normalize_obj_to_raw(wdata)

                if wdata != normalized_wdata:
                    shutil.copy2(ws_json, ws_json + ".backup")
                    with open(ws_json, "w", encoding="utf-8") as f:
                        json.dump(normalized_wdata, f, indent=2, ensure_ascii=False)
                    patched_count += 1
            except Exception as e:
                print(f"Error patching {ws_json}: {e}")

    print(f"Normalized {patched_count} workspace.json files in workspaceStorage to raw colons.")


def patch_workbench_js():
    """No-op: workbench JS patching is no longer needed since we use raw colons."""
    print("Skipping JS patch (IDE natively supports raw colons in-memory).")
    return


def patch_product_json_checksum():
    """No-op: product.json patching is no longer needed since we do not modify workbench JS."""
    print("Skipping product.json checksum update.")
    return



# =============================================================================
# Standalone App Repair: Protobuf URI Normalization
# =============================================================================

def decode_varint(data, pos):
    """Decode a Protobuf base-128 varint from bytes."""
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos + 1
        shift += 7
        pos += 1
    return result, pos


def encode_varint(v):
    """Encode a non-negative integer as a Protobuf base-128 varint."""
    if v < 0:
        raise ValueError(f"Cannot encode negative varint: {v}")
    if v == 0:
        return b"\x00"
    result = bytearray()
    while v > 0x7F:
        result.append((v & 0x7F) | 0x80)
        v >>= 7
    result.append(v & 0x7F)
    return bytes(result)


def parse_protobuf(data):
    """
    Parses raw bytes into a list of (field_num, wire_type, payload).
    Raises ValueError if the bytes do not match valid Protobuf syntax.
    """
    fields = []
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        wire_type = tag & 7
        field_num = tag >> 3

        if field_num == 0:
            raise ValueError("Invalid field number 0")

        if wire_type == 0:  # varint
            val, pos = decode_varint(data, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:  # 64-bit fixed
            if pos + 8 > len(data):
                raise ValueError("Out of bounds for wire type 1")
            val = data[pos:pos + 8]
            pos += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:  # length-delimited
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("Out of bounds for wire type 2")
            val = data[pos:pos + length]
            pos += length
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:  # 32-bit fixed
            if pos + 4 > len(data):
                raise ValueError("Out of bounds for wire type 5")
            val = data[pos:pos + 4]
            pos += 4
            fields.append((field_num, wire_type, val))
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")
    return fields


def process_protobuf_message(data):
    """
    Recursively decodes Protobuf layers, finds URI strings with raw colons,
    replaces them with percent-encoded colons, and rebuilds the message
    while updating varint length delimiters.
    """
    try:
        fields = parse_protobuf(data)

        rebuilt = b""
        for field_num, wire_type, val in fields:
            if wire_type == 2:
                processed_val = process_protobuf_message(val)
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += encode_varint(len(processed_val))
                rebuilt += processed_val
            elif wire_type == 0:
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += encode_varint(val)
            elif wire_type == 1:
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += val  # 8 bytes fixed
            elif wire_type == 5:
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += val  # 4 bytes fixed
        return rebuilt
    except ValueError:
        # Not a valid Protobuf structure — treat as leaf bytes (string or opaque blob)
        try:
            text = data.decode('utf-8')
            if "file:///" in text:
                # Replace percent-encoded colon with raw colon
                new_text = re.sub(r'file:///([a-zA-Z])%3[Aa]', r'file:///\1:', text)
                # Lowercase drive letters
                new_text = re.sub(r'file:///([a-zA-Z]):', lambda m: f"file:///{m.group(1).lower()}:", new_text)
                if new_text != text:
                    print(f"    Normalizing URI: {text[:80]}")
                    print(f"                  -> {new_text[:80]}")
                return new_text.encode('utf-8')
            return data
        except UnicodeDecodeError:
            return data


def patch_standalone_conversation_dbs(convs_dir):
    """Scan and patch all standalone app conversation SQLite databases."""
    print(f"\n  Scanning conversation databases in {convs_dir}...")

    if not os.path.isdir(convs_dir):
        print(f"  [WARNING] Conversations directory not found: {convs_dir}")
        return 0

    patched = 0
    for f in sorted(os.listdir(convs_dir)):
        if not f.endswith(".db"):
            continue
        db_path = os.path.join(convs_dir, f)

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob';"
            )
            if not cursor.fetchone():
                conn.close()
                continue

            cursor.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
            row = cursor.fetchone()
            if not row or not row[0]:
                conn.close()
                continue

            raw_data = row[0]

            if b"file:///" not in raw_data or not re.search(rb'file:///[a-zA-Z]:/', raw_data):
                print(f"  [OK]   {f}: No raw-colon URIs")
                conn.close()
                continue

            print(f"  [FIX]  {f}:")

            backup_path = db_path + ".bak_uri_repair"
            if not os.path.exists(backup_path):
                shutil.copy2(db_path, backup_path)
                print(f"    Backup created: {os.path.basename(backup_path)}")

            repaired_data = process_protobuf_message(raw_data)

            if repaired_data != raw_data:
                cursor.execute(
                    "UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'",
                    (repaired_data,),
                )
                conn.commit()

                cursor.execute("PRAGMA integrity_check")
                status = cursor.fetchone()[0]
                if status != "ok":
                    raise Exception(f"SQLite integrity check FAILED: {status}")

                print(f"    Patched and verified ({len(raw_data)} -> {len(repaired_data)} bytes)")
                patched += 1
            else:
                print(f"    No changes needed (URIs already normalized)")

            conn.close()

        except Exception as e:
            print(f"    ERROR: {e}")
            backup_path = db_path + ".bak_uri_repair"
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, db_path)
                print(f"    Rolled back from backup")

    return patched


def patch_standalone_summaries(pb_path):
    """Patch the standalone app's global summaries protobuf file."""
    print(f"\n  Scanning summaries file: {pb_path}...")

    if not os.path.exists(pb_path):
        print(f"  [WARNING] Summaries file not found: {pb_path}")
        return False

    try:
        with open(pb_path, "rb") as f:
            raw_data = f.read()

        print(f"  File size: {len(raw_data)} bytes")

        if not re.search(rb'file:///[a-zA-Z]:/', raw_data):
            print("  [OK] No raw-colon URIs found")
            return False

        backup_path = pb_path + ".bak_uri_repair"
        if not os.path.exists(backup_path):
            shutil.copy2(pb_path, backup_path)
            print(f"  Backup created: {os.path.basename(backup_path)}")

        repaired_data = process_protobuf_message(raw_data)

        if repaired_data != raw_data:
            with open(pb_path, "wb") as f:
                f.write(repaired_data)
            print(f"  Patched summaries file ({len(raw_data)} -> {len(repaired_data)} bytes)")
            return True
        else:
            print("  No changes needed")
            return False

    except Exception as e:
        print(f"  ERROR: {e}")
        backup_path = pb_path + ".bak_uri_repair"
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, pb_path)
            print(f"  Rolled back from backup")
        return False


def verify_standalone_results(convs_dir, pb_path):
    """Post-repair verification: ensure no raw-colon URIs remain in standalone app files."""
    print("\n  Post-repair verification:")
    all_clean = True

    if os.path.exists(pb_path):
        data = open(pb_path, "rb").read()
        raw_hits = re.findall(rb'file:///[a-zA-Z]:/', data)
        if raw_hits:
            print(f"  [FAIL] Summaries PB still has {len(raw_hits)} raw-colon URI(s)")
            all_clean = False
        else:
            print(f"  [PASS] Summaries PB: all URIs normalized")

    if os.path.isdir(convs_dir):
        for f in sorted(os.listdir(convs_dir)):
            if not f.endswith(".db") or f.endswith(".bak_uri_repair"):
                continue
            db_path = os.path.join(convs_dir, f)
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob';"
                )
                if not cur.fetchone():
                    conn.close()
                    continue
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    raw_hits = re.findall(rb'file:///[a-zA-Z]:/', row[0])
                    if raw_hits:
                        print(f"  [FAIL] {f}: {len(raw_hits)} raw-colon URI(s) remain")
                        all_clean = False
                    else:
                        print(f"  [PASS] {f}: clean")
            except Exception as e:
                print(f"  [ERROR] {f}: {e}")
                all_clean = False

    if all_clean:
        print("  ALL FILES VERIFIED CLEAN")
    else:
        print("  SOME FILES STILL CONTAIN RAW-COLON URIs")

    return all_clean


def _extract_entry_modify_timestamp(entry_data):
    """Extract the modification timestamp from a PB entry for sorting."""
    try:
        fields = parse_protobuf(entry_data)
        for fn, wt, val in fields:
            if fn == 2 and wt == 2:
                # Wrapper: contains field 1 = base64 inner blob
                wrapper_fields = parse_protobuf(val)
                for wfn, wwt, wval in wrapper_fields:
                    if wfn == 1 and wwt == 2:
                        import base64
                        try:
                            inner = base64.b64decode(wval)
                        except Exception:
                            continue
                        # Scan inner blob for field 7 or 10 (timestamps)
                        inner_fields = parse_protobuf(inner)
                        best_ts = 0
                        for ifn, iwt, ival in inner_fields:
                            if ifn in (7, 10) and iwt == 2:
                                ts_fields = parse_protobuf(ival)
                                for tfn, twt, tval in ts_fields:
                                    if tfn == 1 and twt == 0:
                                        if ifn == 7:
                                            return tval  # Prefer field 7
                                        best_ts = max(best_ts, tval)
                        if best_ts > 0:
                            return best_ts
    except (ValueError, Exception):
        pass
    return 0


def sort_standalone_summaries(pb_path):
    """Sort the standalone app's summaries PB entries by modification timestamp (newest first)."""
    print(f"\n  Sorting summaries entries by recency...")

    if not os.path.exists(pb_path):
        print(f"  [SKIP] Summaries file not found")
        return False

    try:
        with open(pb_path, "rb") as f:
            raw_data = f.read()

        # Parse top-level repeated Field 1 entries
        top_fields = parse_protobuf(raw_data)
        entries = [(fn, wt, val) for fn, wt, val in top_fields if fn == 1 and wt == 2]
        non_entries = [(fn, wt, val) for fn, wt, val in top_fields if not (fn == 1 and wt == 2)]

        if len(entries) <= 1:
            print(f"  [OK] Only {len(entries)} entry, no sorting needed")
            return False

        # Extract timestamps and sort
        timestamped = []
        for fn, wt, val in entries:
            ts = _extract_entry_modify_timestamp(val)
            timestamped.append((ts, fn, wt, val))

        timestamped.sort(key=lambda x: x[0], reverse=True)

        # Check if already sorted
        original_order = [t[0] for t in timestamped]
        is_sorted = all(original_order[i] >= original_order[i+1] for i in range(len(original_order)-1))

        # Rebuild regardless to ensure clean output
        rebuilt = b""
        for _, fn, wt, val in timestamped:
            rebuilt += encode_varint((fn << 3) | wt)
            rebuilt += encode_varint(len(val))
            rebuilt += val

        # Add any non-entry fields back
        for fn, wt, val in non_entries:
            if wt == 0:
                rebuilt += encode_varint((fn << 3) | wt)
                rebuilt += encode_varint(val)
            elif wt == 2:
                rebuilt += encode_varint((fn << 3) | wt)
                rebuilt += encode_varint(len(val))
                rebuilt += val

        if rebuilt != raw_data:
            with open(pb_path, "wb") as f:
                f.write(rebuilt)
            print(f"  Sorted {len(entries)} entries by recency (newest first)")
            return True
        else:
            print(f"  [OK] Entries already in correct order")
            return False

    except Exception as e:
        print(f"  ERROR sorting: {e}")
        return False


def run_standalone_repair():
    """
    Execute the full standalone app URI normalization repair.
    Patches agyhub_summaries_proto.pb and all conversation/*.db files.
    """
    base_dir = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity")

    if not os.path.isdir(base_dir):
        print(f"  [SKIP] Standalone app directory not found: {base_dir}")
        print(f"  (This is normal if the standalone app has never been used)")
        return

    convs_dir = os.path.join(base_dir, "conversations")
    pb_path = os.path.join(base_dir, "agyhub_summaries_proto.pb")

    print(f"  Target directory: {base_dir}")

    # Check if standalone app is running
    if is_standalone_running():
        print("\n  [WARNING] Antigravity standalone app is currently running!")
        print("  Please close the standalone app to allow repair to proceed safely.")
        print("  Waiting for standalone app to close (press Ctrl+C to cancel)...")
        while is_standalone_running():
            time.sleep(2)
        print("  Standalone app closed. Proceeding...")

    dbs_patched = patch_standalone_conversation_dbs(convs_dir)
    pb_patched = patch_standalone_summaries(pb_path)

    # Sort entries by recency after URI normalization
    sort_standalone_summaries(pb_path)

    verify_standalone_results(convs_dir, pb_path)

    print(f"\n  Summary: {dbs_patched} conversation DB(s) patched, "
          f"summaries PB {'patched' if pb_patched else 'unchanged'}")
    print(f"  Backups stored as *.bak_uri_repair alongside originals.")


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Normalize Windows workspace URIs across Antigravity IDE and Standalone App"
    )
    parser.add_argument(
        "--skip-standalone",
        action="store_true",
        help="Skip standalone app Protobuf repair (only patch IDE files)"
    )
    parser.add_argument(
        "--standalone-only",
        action="store_true",
        help="Only run standalone app Protobuf repair (skip IDE patches)"
    )
    args = parser.parse_args()

    run_ide = not args.standalone_only
    run_standalone = not args.skip_standalone

    print("=" * 60)
    print("  ANTIGRAVITY URI NORMALIZATION — UNIFIED REPAIR TOOL")
    print("=" * 60)
    print()
    print(f"  Mode: {'IDE + Standalone' if run_ide and run_standalone else 'IDE only' if run_ide else 'Standalone only'}")
    print()

    # ----- IDE REPAIR -----
    if run_ide:
        print("=" * 60)
        print("PHASE 1: IDE Data Store Repair")
        print("=" * 60)

        if is_ide_running():
            print("\n[WARNING] Antigravity IDE is currently running!")
            print("Please close the IDE application to allow the restore to apply safely.")
            print("Waiting for Antigravity IDE to close (press Ctrl+C to cancel)...")
            while is_ide_running():
                time.sleep(2)
            print("IDE closed successfully. Proceeding with restore...")
        else:
            print("Verified that Antigravity IDE is not running.")

        ctx = ApplicationContext()
        ctx.__enter__()
        ctx.perform_preflight_checks()

        storage_path = EnvironmentResolver.get_storage_json_path()
        patch_storage_json(storage_path)

        workspace_storage_dir = EnvironmentResolver.get_workspace_storage_path()
        patch_workspace_storage(workspace_storage_dir)

        ctx.__exit__(None, None, None)

        # Apply the javascript patch to workbench.desktop.main.js
        print("\nApplying workbench JavaScript patch...")
        patch_workbench_js()

        # Update product.json checksums to avoid "installation corrupt" warning
        print("\nUpdating product.json integrity checksums...")
        patch_product_json_checksum()

        # Run migration script to rebuild db with %3A
        print("\nRunning run_migration.py to rebuild conversation database index...")
        import run_migration
        run_migration.main()

    # ----- STANDALONE APP REPAIR -----
    if run_standalone:
        print()
        print("=" * 60)
        phase_num = "2" if run_ide else "1"
        print(f"PHASE {phase_num}: Standalone App Protobuf URI Normalization")
        print("=" * 60)
        print()
        print("  This phase patches the standalone Antigravity app's Protobuf")
        print("  metadata to fix the worktree false-positive detection bug.")
        print("  (Use --skip-standalone to disable this phase)")
        print()

        run_standalone_repair()

    # ----- DONE -----
    print()
    print("=" * 60)
    print("REPAIR COMPLETED SUCCESSFULLY!")
    targets = []
    if run_ide:
        targets.append("Antigravity IDE")
    if run_standalone:
        targets.append("Standalone App")
    print(f"You can now open: {', '.join(targets)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
