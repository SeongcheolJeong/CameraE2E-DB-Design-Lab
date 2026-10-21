"""Lossless, content-addressed storage of immutable JSON evidence.

References occur only in a typed node's ``children`` table. Values supplied by
callers, including dictionaries that look exactly like references or nodes,
always remain literal JSON. The semantic digest is the digest of the original
canonical JSON, so choosing a different split threshold does not change it.

There is no filesystem access or process-wide cache. A loader must return the
current bytes for the requested SHA; each call to :func:`unpack` checks them
again. Callers must not mutate a value while it is being packed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

REFERENCE_SCHEMA = "camera_evidence_ref_v1"
NODE_SCHEMA = "camera_evidence_node_v1"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DEPTH = 128
DEFAULT_MAX_NODES = 100_000
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE_KEYS = {"schema", "sha256", "bytes", "semantic_sha256", "expanded_bytes"}
_CHUNK_BYTES = 256 * 1024
_CACHE_BYTES = 64 * 1024 * 1024
_CACHE_ENTRIES = 500_000


class EvidenceObjectError(ValueError):
    """Invalid JSON, corrupt evidence, or a resource budget violation."""


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise EvidenceObjectError(f"{name} must be a positive integer")
    return value


def validate_reference(reference: Any) -> dict:
    """Validate an explicit reference and return a detached, normalized copy."""
    if type(reference) is not dict or set(reference) != _REFERENCE_KEYS:
        raise EvidenceObjectError("Evidence reference must have exactly the schema fields")
    if type(reference["schema"]) is not str or reference["schema"] != REFERENCE_SCHEMA:
        raise EvidenceObjectError("Unsupported evidence reference schema")
    for key in ("sha256", "semantic_sha256"):
        value = reference[key]
        if type(value) is not str or _SHA.fullmatch(value) is None:
            raise EvidenceObjectError(f"Evidence reference {key} must be a lowercase SHA-256")
    for key in ("bytes", "expanded_bytes"):
        _positive_integer(reference[key], f"Evidence reference {key}")
    return dict(reference)


def _string_chunks(value: str) -> Iterator[bytes]:
    """Match ensure_ascii=False JSON strings without allocating an enormous scalar."""
    yield b'"'
    for offset in range(0, len(value), 4096):
        try:
            yield json.encoder.encode_basestring(value[offset : offset + 4096])[1:-1].encode()
        except UnicodeEncodeError as exc:
            raise EvidenceObjectError("JSON strings must be valid UTF-8") from exc
    yield b'"'


def _scalar_chunks(value: Any) -> Iterator[bytes]:
    kind = type(value)
    if kind is str:
        yield from _string_chunks(value)
    elif value is None:
        yield b"null"
    elif kind is bool:
        yield b"true" if value else b"false"
    elif kind is int:
        try:
            yield str(value).encode()
        except ValueError as exc:
            raise EvidenceObjectError("JSON integer exceeds the interpreter's digit limit") from exc
    elif kind is float:
        if not math.isfinite(value):
            raise EvidenceObjectError("JSON numbers must be finite")
        yield repr(value).encode()
    else:
        raise EvidenceObjectError("Evidence values must contain only JSON types")


class _CanonicalCache:
    """Bounded read-local acceleration; correctness never depends on a hit."""

    def __init__(self, limit: int):
        self.limit = min(limit, _CACHE_BYTES)
        self.entries: dict[int, bytes] = {}
        self.interned: dict[bytes, bytes] = {}
        self.size = 0

    def get(self, value: Any) -> bytes | None:
        return self.entries.get(id(value))

    def store(self, value: Any, data: bytes) -> None:
        if len(self.entries) >= _CACHE_ENTRIES:
            return
        shared = self.interned.get(data)
        if shared is None:
            if self.size + len(data) > self.limit:
                return
            self.interned[data] = data
            self.size += len(data)
            shared = data
        self.entries[id(value)] = shared


def _canonical_chunks(value: Any, cache: _CanonicalCache | None = None) -> Iterator[bytes]:
    """Canonical byte stream for an already validated, acyclic JSON value."""
    if cache is not None:
        cached = cache.get(value)
        if cached is not None:
            yield cached
            return
    if type(value) is dict:
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield from _string_chunks(key)
            yield b":"
            yield from _canonical_chunks(value[key], cache)
        yield b"}"
    elif type(value) is list:
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _canonical_chunks(item, cache)
        yield b"]"
    else:
        yield from _scalar_chunks(value)


def _encode(value: Any, limit: int, cache: _CanonicalCache | None = None) -> bytes:
    data = bytearray()
    for chunk in _canonical_chunks(value, cache):
        if len(data) + len(chunk) > limit:
            raise EvidenceObjectError("Evidence objects exceed the physical byte budget")
        data.extend(chunk)
    return bytes(data)


def _semantic_digest(chunks: Iterator[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class _PackedValue:
    size: int
    depth: int
    reference: dict | None


def _cache_small(value: Any, size: int, cache: _CanonicalCache) -> None:
    """Use the C encoder only after validating and bounding the canonical size."""
    if type(value) not in (dict, list) or size > _CHUNK_BYTES:
        return
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    cache.store(value, data)


def pack(
    value: Any,
    *,
    min_node_bytes: int = 1024,
    max_expanded_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_physical_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Return ``{'root': reference, 'objects': {sha256: canonical_node_bytes}}``.

    The root is always a node. Every dictionary/list child at least
    ``min_node_bytes`` in its original canonical representation is another
    node. Equal children therefore share storage both within this result and
    across separately packed documents. Budgets count unique physical nodes
    but every occurrence in the expanded logical document.
    """
    for name, setting in (
        ("min_node_bytes", min_node_bytes),
        ("max_expanded_bytes", max_expanded_bytes),
        ("max_depth", max_depth),
        ("max_nodes", max_nodes),
        ("max_physical_bytes", max_physical_bytes),
    ):
        _positive_integer(setting, name)
    objects: dict[str, bytes] = {}
    physical_size = 0
    active: set[int] = set()
    memo: dict[int, _PackedValue] = {}
    canonical_cache = _CanonicalCache(max_physical_bytes)

    def visit(item: Any, level: int, *, root: bool = False) -> _PackedValue:
        nonlocal physical_size
        container = type(item) in (dict, list)
        if container:
            if level > max_depth:
                raise EvidenceObjectError("Evidence exceeds the logical depth budget")
            identity = id(item)
            if identity in active:
                raise EvidenceObjectError("Cyclic JSON values are not supported")
            if identity in memo and not root:
                previous = memo[identity]
                if level + previous.depth - 1 > max_depth:
                    raise EvidenceObjectError("Evidence exceeds the logical depth budget")
                return previous
            active.add(identity)
        children = []
        size = 0
        depth = 0
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise EvidenceObjectError("JSON object keys must be strings")
            literal = {}
            size = 2 + max(0, len(item) - 1)
            depth = 1
            for key in sorted(item):
                child = visit(item[key], level + 1)
                size += sum(map(len, _string_chunks(key))) + 1 + child.size
                depth = max(depth, child.depth + 1)
                literal[key] = item[key] if child.reference is None else None
                if child.reference is not None:
                    children.append({"path": [key], "reference": child.reference})
                if size > max_expanded_bytes:
                    raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        elif type(item) is list:
            literal = []
            size = 2 + max(0, len(item) - 1)
            depth = 1
            for index, child_value in enumerate(item):
                child = visit(child_value, level + 1)
                size += child.size
                depth = max(depth, child.depth + 1)
                literal.append(child_value if child.reference is None else None)
                if child.reference is not None:
                    children.append({"path": [index], "reference": child.reference})
                if size > max_expanded_bytes:
                    raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        else:
            literal = item
            for chunk in _scalar_chunks(item):
                size += len(chunk)
                if size > max_expanded_bytes:
                    raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        if size > max_expanded_bytes:
            raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        _cache_small(item, size, canonical_cache)
        reference = None
        if root or (container and size >= min_node_bytes):
            node = {"schema": NODE_SCHEMA, "value": literal, "children": children}
            # The total budget is charged after deduplication. One node can
            # never exceed that same budget, even when it is already present.
            encoded = _encode(node, max_physical_bytes, canonical_cache)
            sha = hashlib.sha256(encoded).hexdigest()
            if sha not in objects:
                if len(objects) >= max_nodes:
                    raise EvidenceObjectError("Evidence exceeds the unique node budget")
                if physical_size + len(encoded) > max_physical_bytes:
                    raise EvidenceObjectError("Evidence objects exceed the physical byte budget")
                objects[sha] = encoded
                physical_size += len(encoded)
            reference = {
                "schema": REFERENCE_SCHEMA,
                "sha256": sha,
                "bytes": len(encoded),
                "semantic_sha256": _semantic_digest(_canonical_chunks(item, canonical_cache)),
                "expanded_bytes": size,
            }
        result = _PackedValue(size, depth, reference)
        if container:
            active.remove(id(item))
            memo[id(item)] = result
        return result

    try:
        root = visit(value, 1, root=True).reference
    except RecursionError as exc:
        raise EvidenceObjectError("Evidence exceeds the interpreter's safe depth budget") from exc
    return {"root": root, "objects": objects}


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, child in pairs:
        if key in value:
            raise EvidenceObjectError("Duplicate keys in an evidence object")
        value[key] = child
    return value


def _reject_constant(value: str) -> Any:
    raise EvidenceObjectError(f"Non-finite JSON number in evidence object: {value}")


def _check_encoded_depth(data: bytes, limit: int) -> None:
    """Bound parser nesting before json.loads constructs any container objects."""
    level = 0
    quoted = False
    escaped = False
    for character in data:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (91, 123):
            level += 1
            if level > limit:
                raise EvidenceObjectError("Evidence object exceeds the encoded depth budget")
        elif character in (93, 125):
            level -= 1


def _prepare_literal(
    value: Any, cache: _CanonicalCache, stats: dict[int, tuple[int, int]]
) -> tuple[int, int]:
    """Validate decoded JSON and build bounded reusable canonical chunks once."""
    if type(value) is dict:
        size = 2 + max(0, len(value) - 1)
        depth = 1
        for key, child in value.items():
            child_size, child_depth = _prepare_literal(child, cache, stats)
            size += sum(map(len, _string_chunks(key))) + 1 + child_size
            depth = max(depth, child_depth + 1)
    elif type(value) is list:
        size = 2 + max(0, len(value) - 1)
        depth = 1
        for child in value:
            child_size, child_depth = _prepare_literal(child, cache, stats)
            size += child_size
            depth = max(depth, child_depth + 1)
    else:
        size, depth = sum(map(len, _scalar_chunks(value))), 0
    _cache_small(value, size, cache)
    if type(value) in (dict, list):
        stats[id(value)] = (size, depth)
    return size, depth


@dataclass(frozen=True)
class _Node:
    reference: dict
    literal: Any
    children: dict[str | int, _Node]
    depth: int


class _NodeCanonicalCache:
    """Read-local LRU for expanded nodes, separate from small literal encodings.

    Large ancestors should hash already encoded children rather than repeatedly
    serialize every scalar. The two caches share the original 64 MiB budget;
    evicted nodes simply use the identical strict streaming encoder again.
    """

    def __init__(self, literal_cache: _CanonicalCache, limit: int):
        self.literal_cache = literal_cache
        self.limit = limit
        self.entries: OrderedDict[int, bytes] = OrderedDict()
        self.size = 0

    def get(self, value: Any) -> bytes | None:
        if type(value) is not _Node:
            return self.literal_cache.get(value)
        found = self.entries.get(id(value))
        if found is not None:
            self.entries.move_to_end(id(value))
        return found

    def store(self, value: _Node, data: bytes) -> None:
        if len(data) > self.limit:
            return
        previous = self.entries.pop(id(value), None)
        if previous is not None:
            self.size -= len(previous)
        while self.entries and self.size + len(data) > self.limit:
            _, previous = self.entries.popitem(last=False)
            self.size -= len(previous)
        self.entries[id(value)] = data
        self.size += len(data)


def _node_chunks(node: _Node, cache: _NodeCanonicalCache) -> Iterator[bytes]:
    cached = cache.get(node)
    if cached is not None:
        yield cached
        return
    value = node.literal
    if type(value) is dict:
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield from _string_chunks(key)
            yield b":"
            if key in node.children:
                yield from _node_chunks(node.children[key], cache)
            else:
                yield from _canonical_chunks(value[key], cache)
        yield b"}"
    elif type(value) is list:
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            if index in node.children:
                yield from _node_chunks(node.children[index], cache)
            else:
                yield from _canonical_chunks(item, cache)
        yield b"]"
    else:
        yield from _scalar_chunks(value)


def _copy_literal(value: Any) -> Any:
    if type(value) is dict:
        return {key: _copy_literal(child) for key, child in value.items()}
    if type(value) is list:
        return [_copy_literal(child) for child in value]
    return value


def _expand(node: _Node) -> Any:
    value = node.literal
    if type(value) is dict:
        return {
            key: _expand(node.children[key]) if key in node.children else _copy_literal(child)
            for key, child in value.items()
        }
    if type(value) is list:
        return [
            _expand(node.children[index]) if index in node.children else _copy_literal(child)
            for index, child in enumerate(value)
        ]
    return value


def unpack(
    root: Any,
    load: Callable[[str], bytes],
    *,
    max_expanded_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_physical_bytes: int = DEFAULT_MAX_BYTES,
) -> Any:
    """Verify a graph and return its original JSON, without mutable aliases.

    All references, node bytes, declared expansion sizes and semantic digests
    are checked before constructing the expanded result. Declared repeated
    children count repeatedly towards the expansion budget. Only this call's
    local cache avoids reloading the same physical object within its graph.
    """
    for name, setting in (
        ("max_expanded_bytes", max_expanded_bytes),
        ("max_depth", max_depth),
        ("max_nodes", max_nodes),
        ("max_physical_bytes", max_physical_bytes),
    ):
        _positive_integer(setting, name)
    if not callable(load):
        raise EvidenceObjectError("Evidence object loader must be callable")
    cache: dict[str, _Node] = {}
    references: dict[str, dict] = {}
    active: set[str] = set()
    physical_size = 0
    cache_budget = min(max_physical_bytes, _CACHE_BYTES)
    canonical_cache = _CanonicalCache(cache_budget * 3 // 8)
    node_cache = _NodeCanonicalCache(canonical_cache, cache_budget - canonical_cache.limit)
    # Cache keys use object identity; decoded documents must stay alive until
    # all validation and expansion is finished, preventing identity reuse.
    documents: list[dict] = []

    def visit(raw_reference: Any, graph_depth: int) -> _Node:
        nonlocal physical_size
        reference = validate_reference(raw_reference)
        sha = reference["sha256"]
        if reference["expanded_bytes"] > max_expanded_bytes:
            raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        if graph_depth > max_depth + 1:
            raise EvidenceObjectError("Evidence exceeds the logical depth budget")
        if sha in references and references[sha] != reference:
            raise EvidenceObjectError("Inconsistent references to the same evidence object")
        if sha in active:
            raise EvidenceObjectError("Cyclic evidence references are not supported")
        if sha in cache:
            return cache[sha]
        if len(references) >= max_nodes:
            raise EvidenceObjectError("Evidence exceeds the unique node budget")
        if physical_size + reference["bytes"] > max_physical_bytes:
            raise EvidenceObjectError("Evidence objects exceed the physical byte budget")
        physical_size += reference["bytes"]
        references[sha] = reference
        active.add(sha)
        data = load(sha)
        if type(data) is not bytes:
            raise EvidenceObjectError("Evidence object loader must return bytes")
        if len(data) != reference["bytes"]:
            raise EvidenceObjectError("Evidence object byte length does not match its reference")
        if hashlib.sha256(data).hexdigest() != sha:
            raise EvidenceObjectError("Evidence object SHA-256 does not match its reference")
        _check_encoded_depth(data, max_depth + 6)
        try:
            document = json.loads(
                data, object_pairs_hook=_object_pairs, parse_constant=_reject_constant
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise EvidenceObjectError(f"Invalid evidence object JSON: {exc}") from exc
        if type(document) is not dict or set(document) != {"schema", "value", "children"}:
            raise EvidenceObjectError("Evidence node must have exactly the schema fields")
        if document["schema"] != NODE_SCHEMA:
            raise EvidenceObjectError("Unsupported evidence node schema")
        if type(document["children"]) is not list:
            raise EvidenceObjectError("Evidence node children must be a list")
        documents.append(document)
        literal_stats: dict[int, tuple[int, int]] = {}
        _prepare_literal(document, canonical_cache, literal_stats)
        # This also rejects overflow floats such as 1e999 and unpaired UTF-16
        # surrogates accepted by Python's JSON parser.
        if _encode(document, len(data), canonical_cache) != data:
            raise EvidenceObjectError("Evidence objects must use canonical JSON encoding")
        literal = document["value"]
        child_references = {}
        previous_key = None
        for child in document["children"]:
            if type(child) is not dict or set(child) != {"path", "reference"}:
                raise EvidenceObjectError("Evidence child must have exactly path and reference")
            path = child["path"]
            if type(path) is not list or len(path) != 1:
                raise EvidenceObjectError("Evidence child paths must select one immediate slot")
            key = path[0]
            if type(literal) is dict:
                if type(key) is not str or key not in literal:
                    raise EvidenceObjectError("Evidence child path is not an existing object key")
            elif type(literal) is list:
                if type(key) is not int or key < 0 or key >= len(literal):
                    raise EvidenceObjectError("Evidence child path is not an existing array index")
            else:
                raise EvidenceObjectError("Scalar evidence nodes cannot contain children")
            if key in child_references:
                raise EvidenceObjectError("Duplicate or overlapping evidence child paths")
            if previous_key is not None and key <= previous_key:
                raise EvidenceObjectError("Evidence child paths must be in canonical order")
            if literal[key] is not None:
                raise EvidenceObjectError("Evidence child slots must contain literal null")
            child_references[key] = validate_reference(child["reference"])
            previous_key = key
        if type(literal) in (dict, list):
            literal_size, literal_depth = literal_stats[id(literal)]
        else:
            literal_size, literal_depth = sum(map(len, _scalar_chunks(literal))), 0
        del literal_stats
        expanded_size = literal_size
        expanded_size += sum(ref["expanded_bytes"] - 4 for ref in child_references.values())
        if expanded_size > max_expanded_bytes:
            raise EvidenceObjectError("Evidence exceeds the expanded byte budget")
        if expanded_size != reference["expanded_bytes"]:
            raise EvidenceObjectError("Evidence expanded byte length does not match its reference")
        children = {
            key: visit(child_ref, graph_depth + 1) for key, child_ref in child_references.items()
        }
        depth = max(
            literal_depth,
            max((1 + child.depth for child in children.values()), default=0),
        )
        if depth > max_depth:
            raise EvidenceObjectError("Evidence exceeds the logical depth budget")
        node = _Node(reference, literal, children, depth)
        if expanded_size <= node_cache.limit:
            canonical = b"".join(_node_chunks(node, node_cache))
            node_cache.store(node, canonical)
        if _semantic_digest(_node_chunks(node, node_cache)) != reference["semantic_sha256"]:
            raise EvidenceObjectError("Evidence semantic SHA-256 does not match its reference")
        cache[sha] = node
        active.remove(sha)
        return node

    try:
        return _expand(visit(root, 1))
    except RecursionError as exc:
        raise EvidenceObjectError("Evidence exceeds the interpreter's safe depth budget") from exc
