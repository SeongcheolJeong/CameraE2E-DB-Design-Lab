"""Bounded node encoding reuse must keep every reconstruction check intact."""

import copy

import pytest

from camera_platform import evidence_objects as evidence


def test_node_lru_budget_and_literal_encodings_are_separate():
    literal = evidence._CanonicalCache(24)
    value = [1, 2]
    literal.store(value, b"[1,2]")
    cache = evidence._NodeCanonicalCache(literal, 40)
    a, b, c = [evidence._Node({}, index, {}, 0) for index in range(3)]
    cache.store(a, b"a" * 15)
    cache.store(b, b"b" * 15)
    assert cache.get(a) == b"a" * 15  # recently used a survives the next eviction
    cache.store(c, b"c" * 20)
    assert cache.get(b) is None
    assert cache.get(a) == b"a" * 15
    assert cache.get(c) == b"c" * 20
    assert cache.get(value) == b"[1,2]"
    assert cache.size == 35
    assert cache.size + literal.size <= 64
    cache.store(b, b"x" * 41)
    assert cache.get(b) is None
    assert cache.size == 35


def test_large_repeated_nodes_keep_detached_output_and_fresh_byte_verification():
    shared = list(range(50000))  # canonical node exceeds the old 256 KiB cutoff
    value = {"conditions": [{"values": shared}, {"values": shared}], "valid": True}
    packed = evidence.pack(value, max_expanded_bytes=2 * 1024**2)
    reads = []

    def load(checksum):
        reads.append(checksum)
        return packed["objects"][checksum]

    first = evidence.unpack(packed["root"], load)
    first["conditions"][0]["values"][0] = -1
    assert first["conditions"][1]["values"][0] == 0
    previous_reads = set(reads)
    reads.clear()
    second = evidence.unpack(packed["root"], load)
    assert second == value
    assert set(reads) == previous_reads  # no process cache may skip actual-byte reads
    target = next(key for key, data in packed["objects"].items() if len(data) > 256 * 1024)
    before = packed["objects"][target]
    packed["objects"][target] = before.replace(b"49999", b"49998", 1)
    assert len(packed["objects"][target]) == len(before)
    with pytest.raises(evidence.EvidenceObjectError, match="SHA-256"):
        evidence.unpack(packed["root"], load)


def test_semantic_hash_is_still_checked_when_node_encoding_is_cached():
    packed = evidence.pack({"a": list(range(1000))})
    root = copy.deepcopy(packed["root"])
    root["semantic_sha256"] = "0" * 64
    with pytest.raises(evidence.EvidenceObjectError, match="semantic SHA-256"):
        evidence.unpack(root, packed["objects"].__getitem__)
