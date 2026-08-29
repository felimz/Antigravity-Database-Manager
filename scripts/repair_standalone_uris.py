import os
import sqlite3
import re
import shutil
import time

# --- Core Protobuf Decoding and Encoding Helpers ---

def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
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

def encode_varint(v: int) -> bytes:
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

# --- Recursive Protobuf Parser & Modifier ---

def parse_protobuf(data: bytes) -> list[tuple[int, int, bytes]]:
    """
    Parses raw bytes into a list of (field_num, wire_type, payload).
    Raises ValueError if the bytes do not match valid Protobuf syntax.
    """
    fields = []
    pos = 0
    while pos < len(data):
        start_pos = pos
        tag, pos = decode_varint(data, pos)
        wire_type = tag & 7
        field_num = tag >> 3
        
        if wire_type == 0:
            val, pos = decode_varint(data, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            if pos + 8 > len(data):
                raise ValueError("Out of bounds for wire type 1")
            val = data[pos:pos+8]
            pos += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("Out of bounds for wire type 2")
            val = data[pos:pos+length]
            pos += length
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:
            if pos + 4 > len(data):
                raise ValueError("Out of bounds for wire type 5")
            val = data[pos:pos+4]
            pos += 4
            fields.append((field_num, wire_type, val))
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")
    return fields

def process_message(data: bytes) -> bytes:
    """
    Recursively decodes Protobuf layers, finds URI strings with raw colons,
    replaces them with percent-encoded colons, normalizes drive letter casing,
    and rebuilds the message while updating length delimiters.
    """
    try:
        fields = parse_protobuf(data)
        
        rebuilt = b""
        for field_num, wire_type, val in fields:
            if wire_type == 2:
                processed_val = process_message(val)
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += encode_varint(len(processed_val))
                rebuilt += processed_val
            elif wire_type == 0:
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += encode_varint(val)
            else:
                rebuilt += encode_varint((field_num << 3) | wire_type)
                rebuilt += val
        return rebuilt
    except ValueError:
        try:
            text = data.decode('utf-8')
            if "file:///" in text:
                # Replace raw colon with percent-encoded colon
                new_text = re.sub(r'file:///([a-zA-Z]):', r'file:///\1%3A', text)
                # Lowercase drive letters
                new_text = re.sub(r'file:///([a-zA-Z])%3[Aa]', lambda m: f"file:///{m.group(1).lower()}%3A", new_text)
                if new_text != text:
                    print(f"  Replacing URI: {text[:80]} -> {new_text[:80]}")
                return new_text.encode('utf-8')
            return data
        except UnicodeDecodeError:
            return data

# --- Database and File Processing Operations ---

def patch_conversation_dbs(convs_dir: str):
    """Scan and patch all SQLite databases in the conversations folder."""
    print("\nScanning conversation databases...")
    if not os.path.isdir(convs_dir):
        print(f"[WARNING] Conversations directory not found: {convs_dir}")
        return
        
    for f in os.listdir(convs_dir):
        if not f.endswith(".db"):
            continue
        if "bak" in f or "repair" in f:
            continue
            
        db_path = os.path.join(convs_dir, f)
        backup_path = db_path + ".bak_v2110"
        
        if not os.path.exists(backup_path):
            shutil.copy2(db_path, backup_path)
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
            row = cursor.fetchone()
            if row:
                raw_data = row[0]
                repaired_data = process_message(raw_data)
                if repaired_data != raw_data:
                    cursor.execute("UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'", (repaired_data,))
                    conn.commit()
                    print(f"[SUCCESS] Patched database: {f}")
                else:
                    print(f"[INFO] No mismatch in database: {f}")
            
            cursor.execute("PRAGMA integrity_check")
            status = cursor.fetchone()[0]
            if status != "ok":
                raise Exception(f"SQLite integrity check returned: {status}")
            conn.close()
        except Exception as e:
            print(f"[ERROR] Failed to patch database {f}: {e}")

def patch_summaries_file(pb_path: str):
    """Scan and patch the global summaries protobuf file."""
    print("\nScanning summaries protobuf file...")
    if not os.path.exists(pb_path):
        print(f"[WARNING] Summaries file not found: {pb_path}")
        return
        
    backup_path = pb_path + ".bak_v2110"
    if not os.path.exists(backup_path):
        shutil.copy2(pb_path, backup_path)
    
    try:
        with open(pb_path, "rb") as f:
            raw_data = f.read()
            
        repaired_data = process_message(raw_data)
        if repaired_data != raw_data:
            with open(pb_path, "wb") as f:
                f.write(repaired_data)
            print("[SUCCESS] Patched summaries protobuf file.")
        else:
            print("[INFO] No mismatch in summaries protobuf file.")
    except Exception as e:
        print(f"[ERROR] Failed to patch summaries file: {e}")

def main():
    base_dir = r"C:\Users\felim\.gemini\antigravity"
    convs_dir = os.path.join(base_dir, "conversations")
    pb_path = os.path.join(base_dir, "agyhub_summaries_proto.pb")
    
    print("==============================================================")
    print("   ANTIGRAVITY 2.0 (v2.11.0) STANDALONE URI REPAIR TOOL")
    print("==============================================================")
    
    patch_conversation_dbs(convs_dir)
    patch_summaries_file(pb_path)
    
    print("\n==============================================================")
    print("ALL REPAIR OPERATIONS COMPLETED SUCCESSFULLY!")
    print("Please restart the standalone app to verify.")
    print("==============================================================")

if __name__ == "__main__":
    main()
