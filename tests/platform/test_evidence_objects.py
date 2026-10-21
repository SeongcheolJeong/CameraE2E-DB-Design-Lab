"""Lossless graph encoding, hostile envelopes, and resource boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter

import pytest

from camera_platform import evidence_objects
from camera_platform.evidence_objects import (
    NODE_SCHEMA,
    REFERENCE_SCHEMA,
    EvidenceObjectError,
    pack,
    unpack,
    validate_reference,
)
from camera_platform.registry import canonical, digest


def _bytes(value):
    return canonical(value).encode()


def _literal_reference(value):
    """Independent envelope builder, also useful for deliberately invalid nodes."""
    node = {"schema": NODE_SCHEMA, "value": value, "children": []}
    data = _bytes(node)
    return {
        "schema": REFERENCE_SCHEMA,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "semantic_sha256": digest(value),
        "expanded_bytes": len(_bytes(value)),
    }, data


def _envelope(node, *, expanded, semantic):
    data = _bytes(node)
    return {
        "schema": REFERENCE_SCHEMA,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "semantic_sha256": semantic,
        "expanded_bytes": expanded,
    }, data


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -1234567,
        3.14159,
        -0.0,
        1e-100,
        "",
        '한글 / é 🌌 \n\t"\\',
        [],
        {},
        [True, None, {"0": [1, "a"]}],
        {"z": 0, "가": 1, "a": [0, False, {"": []}]},
    ],
)
def test_round_trip_and_legacy_semantic_digest(value):
    graph = pack(value, min_node_bytes=1)
    original = _bytes(value)
    assert graph["root"]["semantic_sha256"] == digest(value)
    assert graph["root"]["expanded_bytes"] == len(original)
    result = unpack(graph["root"], graph["objects"].__getitem__)
    assert _bytes(result) == original


def test_literal_reference_and_node_shaped_user_dicts_are_not_interpreted():
    fake_ref, _ = _literal_reference("missing external blob")
    value = {
        "model": fake_ref,
        "nested": {"schema": NODE_SCHEMA, "value": None, "children": [fake_ref]},
        "payload": [{"$ref": fake_ref}, {"reference": fake_ref, "path": ["x"]}],
    }
    graph = pack(value, min_node_bytes=1)
    assert fake_ref["sha256"] not in graph["objects"]
    assert unpack(graph["root"], graph["objects"].__getitem__) == value


def test_equal_independent_subtrees_deduplicate_with_no_output_aliases():
    snapshot = {"data": list(range(1000)), "metadata": {"version": 1}}
    value = {"baseline": snapshot, "candidate": copy.deepcopy(snapshot)}
    untouched = copy.deepcopy(value)
    graph = pack(value)
    root = json.loads(graph["objects"][graph["root"]["sha256"]])
    assert len(root["children"]) == 2
    assert root["children"][0]["reference"] == root["children"][1]["reference"]
    assert len(graph["objects"]) == 3
    calls = Counter()

    def load(sha):
        calls[sha] += 1
        return graph["objects"][sha]

    result = unpack(graph["root"], load)
    assert result == untouched
    assert set(calls.values()) == {1}
    result["candidate"]["data"][0] = 99
    result["candidate"]["metadata"]["version"] = 2
    assert result["baseline"] == snapshot
    assert value == untouched
    assert unpack(graph["root"], load) == untouched
    assert set(calls.values()) == {2}


def test_deduplication_across_separately_packed_candidate_documents():
    snapshot = {"calibration": {"samples": [index / 8 for index in range(1024)]}}
    graphs = [pack({"candidate": index, "source": copy.deepcopy(snapshot)}) for index in range(4)]
    closure = {sha: blob for graph in graphs for sha, blob in graph["objects"].items()}
    assert len(closure) == 7  # Three shared source nodes plus four candidate roots.
    assert (
        sum(map(len, closure.values()))
        < sum(graph["root"]["expanded_bytes"] for graph in graphs) / 2
    )
    for index, graph in enumerate(graphs):
        assert unpack(graph["root"], closure.__getitem__) == {
            "candidate": index,
            "source": snapshot,
        }


def test_encoding_is_stable_and_threshold_changes_only_physical_graph():
    source = {"b": list(range(500)), "a": {"x": "a" * 1500}}
    reordered = {"a": {"x": "a" * 1500}, "b": list(range(500))}
    first, second = pack(source), pack(reordered)
    assert first == second
    dense = pack(source, min_node_bytes=1)
    flat = pack(source, min_node_bytes=100_000)
    assert dense["root"]["semantic_sha256"] == flat["root"]["semantic_sha256"]
    assert dense["root"]["expanded_bytes"] == flat["root"]["expanded_bytes"]
    assert dense["root"]["sha256"] != flat["root"]["sha256"]


@pytest.mark.parametrize("value", [(1, 2), {1: "x"}, {False: 0}, {1}, b"x", object()])
def test_non_json_types_rejected(value):
    with pytest.raises(EvidenceObjectError):
        pack(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "\ud800"])
def test_nonfinite_and_non_utf8_values_rejected(value):
    with pytest.raises(EvidenceObjectError):
        pack({"x": value})


def test_cycles_refused_but_repeated_input_aliases_allowed():
    cyclic = []
    cyclic.append({"again": cyclic})
    with pytest.raises(EvidenceObjectError, match="Cyclic"):
        pack(cyclic)
    shared = {"sample": list(range(1000))}
    graph = pack([shared, shared])
    result = unpack(graph["root"], graph["objects"].__getitem__)
    assert result == [shared, shared]
    assert result[0] is not result[1]


@pytest.mark.parametrize(
    "setting",
    ["min_node_bytes", "max_expanded_bytes", "max_depth", "max_nodes", "max_physical_bytes"],
)
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.0, "2"])
def test_pack_budgets_require_positive_exact_integers(setting, invalid):
    with pytest.raises(EvidenceObjectError, match="positive integer"):
        pack({}, **{setting: invalid})


@pytest.mark.parametrize(
    "setting", ["max_expanded_bytes", "max_depth", "max_nodes", "max_physical_bytes"]
)
def test_unpack_budgets_require_positive_exact_integers(setting):
    graph = pack({})
    with pytest.raises(EvidenceObjectError, match="positive integer"):
        unpack(graph["root"], graph["objects"].__getitem__, **{setting: True})


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("schema", None),
        ("sha256", "A" * 64),
        ("sha256", "g" * 64),
        ("sha256", 0),
        ("semantic_sha256", "0" * 63),
        ("bytes", True),
        ("bytes", 0),
        ("expanded_bytes", False),
        ("expanded_bytes", -1),
        ("expanded_bytes", 4.0),
    ],
)
def test_malformed_root_references_refused_before_loader(field, value):
    reference = pack(None)["root"]
    reference[field] = value
    with pytest.raises(EvidenceObjectError):
        unpack(reference, lambda _: pytest.fail("invalid reference must not be loaded"))


def test_reference_exact_keys_and_detached_result():
    ref = pack(None)["root"]
    assert validate_reference(ref) == ref
    assert validate_reference(ref) is not ref
    for invalid in (
        {**ref, "extra": 1},
        {key: value for key, value in ref.items() if key != "bytes"},
    ):
        with pytest.raises(EvidenceObjectError):
            validate_reference(invalid)


def test_expanded_budget_counts_repeated_children_before_load():
    source = {"source": list(range(1000))}
    graph = pack([source] * 20)
    size = graph["root"]["expanded_bytes"]
    assert sum(map(len, graph["objects"].values())) < size / 4
    with pytest.raises(EvidenceObjectError, match="expanded byte budget"):
        unpack(
            graph["root"],
            lambda _: pytest.fail("root budget must precede loading"),
            max_expanded_bytes=size - 1,
        )
    with pytest.raises(EvidenceObjectError, match="expanded byte budget"):
        pack([source] * 20, max_expanded_bytes=size - 1)
    assert (
        unpack(graph["root"], graph["objects"].__getitem__, max_expanded_bytes=size)
        == [source] * 20
    )


def test_unique_node_and_physical_byte_budgets():
    source = {"source": list(range(1000))}
    graph = pack([source] * 4)
    physical = sum(map(len, graph["objects"].values()))
    count = len(graph["objects"])
    assert pack([source] * 4, max_nodes=count, max_physical_bytes=physical) == graph
    assert (
        unpack(
            graph["root"],
            graph["objects"].__getitem__,
            max_nodes=count,
            max_physical_bytes=physical,
        )
        == [source] * 4
    )
    for limits, match in [
        ({"max_nodes": count - 1}, "node budget"),
        ({"max_physical_bytes": physical - 1}, "physical byte budget"),
    ]:
        with pytest.raises(EvidenceObjectError, match=match):
            pack([source] * 4, **limits)
        with pytest.raises(EvidenceObjectError, match=match):
            unpack(graph["root"], graph["objects"].__getitem__, **limits)


def test_depth_budget_includes_literal_and_referenced_containers_and_alias_context():
    source = {"last": [1, 2]}
    value = {"a": source, "b": {"source": source}}
    graph = pack(value, min_node_bytes=1, max_depth=4)
    assert unpack(graph["root"], graph["objects"].__getitem__, max_depth=4) == value
    with pytest.raises(EvidenceObjectError, match="depth budget"):
        pack(value, min_node_bytes=1, max_depth=3)
    with pytest.raises(EvidenceObjectError, match="depth budget"):
        unpack(graph["root"], graph["objects"].__getitem__, max_depth=3)
    flat = pack(value, min_node_bytes=100_000)
    with pytest.raises(EvidenceObjectError, match="depth budget"):
        unpack(flat["root"], flat["objects"].__getitem__, max_depth=3)


def test_blob_corruption_is_detected_on_each_public_read():
    graph = pack({"a": list(range(500))})
    assert unpack(graph["root"], graph["objects"].__getitem__)["a"][0] == 0
    sha = next(key for key in graph["objects"] if key != graph["root"]["sha256"])
    original = graph["objects"][sha]
    graph["objects"][sha] = original.replace(b"100", b"101", 1)
    with pytest.raises(EvidenceObjectError, match="SHA-256"):
        unpack(graph["root"], graph["objects"].__getitem__)


@pytest.mark.parametrize("returned", [None, bytearray(b"x"), "x", b"", b"x"])
def test_loader_requires_exact_bytes_size_and_hash(returned):
    graph = pack(None)
    with pytest.raises(EvidenceObjectError):
        unpack(graph["root"], lambda _: returned)
    with pytest.raises(EvidenceObjectError, match="callable"):
        unpack(graph["root"], None)


def test_missing_object_error_is_not_hidden():
    graph = pack({"source": list(range(500))})
    del graph["objects"][next(key for key in graph["objects"] if key != graph["root"]["sha256"])]
    with pytest.raises(KeyError):
        unpack(graph["root"], graph["objects"].__getitem__)


@pytest.mark.parametrize("field", ["semantic_sha256", "expanded_bytes"])
def test_semantic_hash_and_expanded_size_are_verified(field):
    graph = pack({"source": list(range(500))})
    if field == "semantic_sha256":
        graph["root"][field] = "f" * 64
    else:
        graph["root"][field] += 1
    with pytest.raises(EvidenceObjectError, match="semantic SHA|expanded byte length"):
        unpack(graph["root"], graph["objects"].__getitem__)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda node: node.update(extra=1),
        lambda node: node.pop("value"),
        lambda node: node.update(schema="wrong"),
        lambda node: node.update(children={}),
        lambda node: node["children"][0].update(extra=1),
        lambda node: node["children"][0].update(path=[]),
        lambda node: node["children"][0].update(path=["a", "nested"]),
        lambda node: node["children"][0].update(path=["missing"]),
        lambda node: node["children"][0].update(path=[True]),
        lambda node: node["value"].update(a=0),
        lambda node: node["children"].append(copy.deepcopy(node["children"][0])),
        lambda node: node["children"].reverse(),
    ],
)
def test_malformed_node_shapes_slots_order_and_paths(mutate):
    graph = pack({"a": list(range(500)), "b": list(range(500))})
    node = json.loads(graph["objects"][graph["root"]["sha256"]])
    mutate(node)
    ref, data = _envelope(
        node, expanded=graph["root"]["expanded_bytes"], semantic=graph["root"]["semantic_sha256"]
    )
    graph["objects"][ref["sha256"]] = data
    with pytest.raises(EvidenceObjectError):
        unpack(ref, graph["objects"].__getitem__)


@pytest.mark.parametrize("index", [True, False, -1, 1, "0", 0.0])
def test_malformed_array_slots(index):
    graph = pack([list(range(500))])
    node = json.loads(graph["objects"][graph["root"]["sha256"]])
    node["children"][0]["path"] = [index]
    ref, data = _envelope(
        node, expanded=graph["root"]["expanded_bytes"], semantic=graph["root"]["semantic_sha256"]
    )
    graph["objects"][ref["sha256"]] = data
    with pytest.raises(EvidenceObjectError):
        unpack(ref, graph["objects"].__getitem__)


def test_inconsistent_metadata_for_same_sha_is_rejected():
    graph = pack({"a": list(range(500)), "b": list(range(500))})
    node = json.loads(graph["objects"][graph["root"]["sha256"]])
    node["children"][1]["reference"]["semantic_sha256"] = "0" * 64
    ref, data = _envelope(
        node, expanded=graph["root"]["expanded_bytes"], semantic=graph["root"]["semantic_sha256"]
    )
    graph["objects"][ref["sha256"]] = data
    with pytest.raises(EvidenceObjectError, match="Inconsistent references"):
        unpack(ref, graph["objects"].__getitem__)


def test_forged_small_root_does_not_allow_large_expansion_or_load_children():
    child_ref, _ = _literal_reference(["large"] * 1000)
    node = {
        "schema": NODE_SCHEMA,
        "value": [None] * 100,
        "children": [{"path": [index], "reference": child_ref} for index in range(100)],
    }
    ref, data = _envelope(node, expanded=2, semantic=digest([]))
    calls = []

    def load(sha):
        calls.append(sha)
        assert sha == ref["sha256"], "expansion preflight must precede loading children"
        return data

    with pytest.raises(EvidenceObjectError, match="expanded byte budget"):
        unpack(ref, load, max_expanded_bytes=10_000)
    assert calls == [ref["sha256"]]


@pytest.mark.parametrize(
    "data",
    [
        b'{"schema":"camera_evidence_node_v1","value":1,"value":2,"children":[]}',
        b'{"children":[],"schema":"camera_evidence_node_v1","value":NaN}',
        b'{"children":[],"schema":"camera_evidence_node_v1","value":1e999}',
        b'{"children":[],"schema":"camera_evidence_node_v1","value":"\\ud800"}',
        b'{"children": [],"schema":"camera_evidence_node_v1","value":null}',
        b'{"children":[],"schema":"camera_evidence_node_v1","value":"\xff"}',
    ],
)
def test_noncanonical_duplicate_nonfinite_and_invalid_utf8_objects(data):
    ref, _ = _literal_reference(None)
    ref.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    with pytest.raises(EvidenceObjectError):
        unpack(ref, lambda _: data)


def test_parser_depth_is_bounded_before_decoding():
    data = (
        b'{"children":[],"schema":"camera_evidence_node_v1","value":'
        + b"[" * 1000
        + b"0"
        + b"]" * 1000
        + b"}"
    )
    ref, _ = _literal_reference(None)
    ref.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    with pytest.raises(EvidenceObjectError, match="encoded depth budget"):
        unpack(ref, lambda _: data, max_depth=8)


def test_long_escaped_unicode_strings_match_registry_canonical_exactly():
    value = {"key\n" * 2048: '\n\x00한글🌌\\"' * 2048}
    graph = pack(value)
    assert graph["root"]["semantic_sha256"] == digest(value)
    assert graph["root"]["expanded_bytes"] == len(_bytes(value))
    assert unpack(graph["root"], graph["objects"].__getitem__) == value


def test_tiny_acceleration_cache_and_chunk_limits_do_not_change_wire_or_semantics(monkeypatch):
    value = {"a": list(range(2000)), "b": [{"data": [1.2, 3.4]}] * 100}
    expected = pack(value)
    monkeypatch.setattr(evidence_objects, "_CACHE_BYTES", 16)
    monkeypatch.setattr(evidence_objects, "_CACHE_ENTRIES", 2)
    monkeypatch.setattr(evidence_objects, "_CHUNK_BYTES", 16)
    assert pack(value) == expected
    assert unpack(expected["root"], expected["objects"].__getitem__) == value


def test_cache_byte_and_entry_budgets_are_independent_of_expansion():
    cache = evidence_objects._CanonicalCache(6)
    values = [{"x": index} for index in range(4)]
    for value, data in zip(values, [b"abc", b"abc", b"def", b"ghi"], strict=True):
        cache.store(value, data)
    assert cache.size == 6
    assert cache.get(values[0]) is cache.get(values[1])
    assert cache.get(values[2]) == b"def"
    assert cache.get(values[3]) is None


def test_overriding_depth_budget_cannot_escape_interpreter_safety():
    value = 1
    for _ in range(1200):
        value = [value]
    with pytest.raises(EvidenceObjectError, match="depth budget"):
        pack(value, max_depth=10_000)
