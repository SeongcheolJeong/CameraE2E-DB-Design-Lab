"""Lossless storage for new large registry JSON; legacy bytes are never rewritten.

The prefix is outside JSON, so a user dictionary can never be mistaken for a
storage reference. Registry content/request/result/audit hashes still bind the
original logical JSON. Private graph blobs do not become public artifacts.
"""

from __future__ import annotations

import json

PREFIX = "@camera-evidence-storage-v1\n"
THRESHOLD = 256 * 1024
MAX_LOGICAL_BYTES = 1024**3


def encode(registry, value):
    from .evidence_objects import pack
    from .registry import canonical

    # Stop counting once the inline threshold is crossed; do not materialize a
    # many-candidate logical manifest merely to decide how to store it.
    chunks, size = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode())
        if size >= THRESHOLD:
            break
        chunks.append(chunk)
    else:
        return "".join(chunks)
    packed = pack(value, max_expanded_bytes=MAX_LOGICAL_BYTES)
    for checksum, content in packed["objects"].items():
        artifact = registry.store_blob(content, "evidence-object.json", "application/json")
        if artifact["sha256"] != checksum:
            raise ValueError("Evidence object storage changed its checksum")
    return PREFIX + canonical(packed["root"])


def decode(text, load):
    from .evidence_objects import unpack

    if not text.startswith(PREFIX):
        return json.loads(text)
    root = json.loads(text[len(PREFIX):])
    return unpack(root, load, max_expanded_bytes=MAX_LOGICAL_BYTES)


def decode_registry(registry, text):
    from .registry import IntegrityError

    try:
        return decode(text, lambda sha: registry.blob_path(sha).read_bytes())
    except (ValueError, KeyError, FileNotFoundError, RecursionError) as exc:
        raise IntegrityError("Stored evidence graph failed exact reconstruction") from exc
