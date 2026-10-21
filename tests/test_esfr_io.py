"""Native Bayer decoding fixtures independent of the decoder implementation."""

from __future__ import annotations

import copy
import struct
import tracemalloc

import numpy as np
import pytest
from camera_platform.esfr_io import RawFrame, read_isp
from PIL import Image


def meta(w=4, h=2, packing="unpacked16", bits=12, stride=None, **kwargs):
    row = {
        "unpacked16": w * 2,
        "unpacked8": w,
        "mipi_raw10": w // 4 * 5,
        "mipi_raw12": w // 2 * 3,
        "mipi_raw14": w // 4 * 7,
    }[packing]
    fmt = {
        "packing": packing,
        "width": w,
        "height": h,
        "stride_bytes": stride or row,
        "byte_order": "little",
        "bit_depth": bits,
        "cfa": "RGGB",
        "black_level_dn": 0,
        "white_level_dn": (1 << bits) - 1,
    }
    fmt.update(kwargs)
    return {"raw_format": fmt, "geometry": {"full_sensor_shape": [h, w], "crop_origin": [0, 0]}}


@pytest.mark.parametrize(
    "packing,bits,payload,expected",
    [
        ("mipi_raw10", 10, bytes.fromhex("55 aa ff 00 39"), [0x155, 0x2AA, 0x3FF, 0]),
        ("mipi_raw12", 12, bytes.fromhex("ab 12 3c"), [0xABC, 0x123]),
        ("mipi_raw14", 14, bytes.fromhex("48 8d d1 ff 74 61 fd"), [0x1234, 0x2345, 0x3456, 0x3FFF]),
    ],
)
def test_independent_packed_byte_examples(tmp_path, packing, bits, payload, expected):
    path = tmp_path / "capture.bin"
    path.write_bytes(payload)
    with RawFrame(path, meta(len(expected), 1, packing, bits)) as raw:
        np.testing.assert_array_equal(raw.read(), [expected])
        np.testing.assert_array_equal(raw.read([0, 1, 1, 1]), [[expected[1]]])


def pack_row(values, bits):
    """Fixture encoder uses concatenated bit strings, not decoder bit operations."""
    group = 2 if bits == 12 else 4
    payload = bytearray()
    for start in range(0, len(values), group):
        parts = [format(int(v), f"0{bits}b") for v in values[start : start + group]]
        payload.extend(int(x[:8], 2) for x in parts)
        low = "".join(x[8:] for x in reversed(parts))
        payload.extend(int(low, 2).to_bytes(group * (bits - 8) // 8, "little"))
    return bytes(payload)


@pytest.mark.parametrize("bits,width", [(10, 4), (10, 20), (12, 2), (12, 18), (14, 16)])
def test_packed_roundtrip_stride_header_and_unaligned_roi(tmp_path, bits, width):
    values = np.random.default_rng(bits + width).integers(0, 1 << bits, (7, width), dtype=np.uint16)
    row = len(pack_row(values[0], bits))
    path = tmp_path / "raw.bin"
    path.write_bytes(b"head!" + b"".join(pack_row(x, bits) + b"\xff\xa5\x7e" for x in values))
    metadata = meta(width, 7, f"mipi_raw{bits}", bits, row + 3, header_bytes=5)
    with RawFrame(path, metadata) as raw:
        np.testing.assert_array_equal(raw.read(), values)
        np.testing.assert_array_equal(raw.read([1, 1, 3, width - 1]), values[1:4, 1:])


@pytest.mark.parametrize("order", ["little", "big"])
def test_unpacked16_endian_shift_odd_stride(tmp_path, order):
    values = np.array([[1, 127, 4095], [2001, 77, 9]], dtype=np.uint16)
    path = tmp_path / "word.bin"
    dtype = "<u2" if order == "little" else ">u2"
    path.write_bytes(b"!" + b"".join((x << 4).astype(dtype).tobytes() + b"!" for x in values))
    with RawFrame(path, meta(3, 2, stride=7, byte_order=order, bit_shift=4, header_bytes=1)) as raw:
        np.testing.assert_array_equal(raw.read(), values)
        np.testing.assert_array_equal(raw.read([0, 1, 2, 2]), values[:, 1:])


def test_unpacked8(tmp_path):
    path = tmp_path / "byte.bin"
    path.write_bytes(bytes([1, 17, 255, 9, 32, 127]))
    with RawFrame(path, meta(3, 2, "unpacked8", 8)) as raw:
        np.testing.assert_array_equal(raw.read(), [[1, 17, 255], [9, 32, 127]])


@pytest.mark.parametrize("cfa", ["RGGB", "BGGR", "GRBG", "GBRG"])
@pytest.mark.parametrize("origin", [[0, 0], [1, 0], [0, 1], [1, 1], [5, 7]])
def test_native_cfa_crop_phases_and_per_site_black(tmp_path, cfa, origin):
    # Full native coordinates encoded in pixel DN; file has an odd native crop.
    r, c = np.indices((7, 9))
    values = (100 * (r + origin[0]) + c + origin[1]).astype("<u2")
    path = tmp_path / "crop.bin"
    path.write_bytes(values.tobytes())
    metadata = meta(
        9, 7, cfa=cfa, black_level_dn=[3, 7, 11, 13], white_level_dn=[4000, 4001, 4002, 4003]
    )
    metadata["geometry"] = {"full_sensor_shape": [20, 20], "crop_origin": origin}
    with RawFrame(path, metadata) as raw:
        for channel in ["R", "Gr", "Gb", "B"]:
            plane, sampling = raw.plane([1, 1, 5, 7], channel)
            nr, nc = sampling["native_origin"]
            sr, sc = nr % 2, nc % 2
            assert cfa[2 * sr + sc] == channel[0]
            if channel.startswith("G"):
                assert (sr == cfa.index("R") // 2) == (channel == "Gr")
            black = [3, 7, 11, 13][2 * sr + sc]
            pr, pc = np.indices(plane.shape)
            np.testing.assert_array_equal(plane, 100 * (nr + 2 * pr) + nc + 2 * pc - black)
            assert sampling["pitch_native"] == 2
            assert sampling["white_level_dn"] == 4000 + 2 * sr + sc
            assert not sampling["clipped"]


def test_negative_black_subtracted_values_are_preserved(tmp_path):
    path = tmp_path / "black.bin"
    path.write_bytes(np.zeros((2, 4), dtype="<u2").tobytes())
    with RawFrame(path, meta(black_level_dn=64)) as raw:
        values, _ = raw.plane(None, "Gr")
        assert np.all(values == -64)
        with pytest.raises(ValueError, match="separate sublattices"):
            raw.plane(None, "G")


@pytest.mark.parametrize(
    "change",
    [
        {"packing": "guess"},
        {"bit_depth": True},
        {"bit_depth": 17},
        {"bit_shift": 5},
        {"byte_order": "native"},
        {"stride_bytes": 7},
        {"width": 0},
        {"cfa": "RGBW"},
        {"black_level_dn": -1},
        {"white_level_dn": 5000},
        {"black_level_dn": [0, 1]},
        {"black_level_dn": [0, 1, float("nan"), 0]},
        {"white_level_dn": 0},
    ],
)
def test_bad_raw_metadata_rejected(tmp_path, change):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(16))
    metadata = meta()
    metadata["raw_format"].update(change)
    with pytest.raises(ValueError):
        RawFrame(path, metadata)


@pytest.mark.parametrize("field", ["full_sensor_shape", "crop_origin"])
def test_geometry_required(tmp_path, field):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(16))
    metadata = meta()
    del metadata["geometry"][field]
    with pytest.raises(ValueError):
        RawFrame(path, metadata)


@pytest.mark.parametrize("delta", [-1, 1, 100])
def test_exact_payload_size(tmp_path, delta):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(16 + delta))
    with pytest.raises(ValueError, match="exactly"):
        RawFrame(path, meta())


@pytest.mark.parametrize(
    "packing,bits,width",
    [
        ("mipi_raw10", 10, 6),
        ("mipi_raw12", 12, 3),
        ("mipi_raw14", 14, 6),
    ],
)
def test_partial_packing_group_rejected(tmp_path, packing, bits, width):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(100))
    with pytest.raises(ValueError, match="multiple"):
        RawFrame(path, meta(width, 2, packing, bits))


def test_decode_rejects_bits_outside_declared_range(tmp_path):
    path = tmp_path / "raw.bin"
    path.write_bytes(struct.pack("<8H", 0, 1, 4096, 7, 0, 0, 0, 0))
    with RawFrame(path, meta()) as raw:
        with pytest.raises(ValueError, match="outside declared"):
            raw.read()
        # A locally valid ROI remains usable; metadata does not falsely claim full scanning.
        assert raw.read([0, 0, 1, 2]).tolist() == [[0, 1]]


def test_12mp_roi_only_read_and_native_origin(tmp_path):
    path = tmp_path / "12mp.bin"
    with path.open("wb") as stream:
        stream.truncate(3000 * 4000 * 2)
        stream.seek((2001 * 4000 + 3001) * 2)
        stream.write(struct.pack("<H", 1234))
    tracemalloc.start()
    with RawFrame(path, meta(4000, 3000)) as raw:
        assert isinstance(raw._bytes, np.memmap)
        values = raw.read([2000, 3000, 11, 13])
        plane, sampling = raw.plane([2000, 3000, 11, 13], "B")
        assert raw.shape == (3000, 4000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2_000_000
    assert values[1, 1] == 1234
    assert plane[0, 0] == 1234
    assert sampling["native_origin"] == [2001, 3001]


@pytest.mark.parametrize(
    "box", [[-1, 0, 1, 1], [0, 0, 0, 1], [1, 1, 2, 2], [0, 0, 2.0, 4], [0, 0, 1], [False, 0, 1, 1]]
)
def test_roi_bounds(tmp_path, box):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(16))
    with RawFrame(path, meta()) as raw, pytest.raises(ValueError):
        raw.read(box)


@pytest.mark.parametrize("transfer", ["srgb", "linear", "unknown"])
def test_isp_explicit_transfer_and_no_orientation(tmp_path, transfer):
    pixels = np.array([[[0, 12, 128], [64, 255, 1]]], dtype=np.uint8)
    path = tmp_path / "isp.png"
    exif = Image.Exif()
    exif[274] = 6
    Image.fromarray(pixels).save(path, exif=exif)
    rgb, info = read_isp(path, {"transfer": transfer})
    expected = pixels.astype(np.float32) / 255
    if transfer == "srgb":
        expected = np.where(
            expected <= 0.04045, expected / 12.92, ((expected + 0.055) / 1.055) ** 2.4
        )
    np.testing.assert_allclose(rgb, expected, atol=2e-7)
    assert rgb.shape == (1, 2, 3)
    assert info["orientation_tag"] == 6
    assert not info["orientation_applied"]
    assert info["linear"] == (transfer != "unknown")
    if transfer == "unknown":
        assert info["linearization"] == "not_linearized"


@pytest.mark.parametrize("format", ["PNG", "TIFF"])
def test_16bit_rgb_precision_preserved(tmp_path, format):
    import imagecodecs
    import tifffile

    pixels = np.array([[[1, 32769, 65535], [128, 257, 1000]]], dtype=np.uint16)
    path = tmp_path / f"isp.{format.lower()}"
    if format == "PNG":
        path.write_bytes(imagecodecs.png_encode(pixels))
    else:
        tifffile.imwrite(path, pixels, photometric="rgb")
    rgb, info = read_isp(path, {"transfer": "linear"})
    assert info["bit_depth"] == 16
    np.testing.assert_allclose(rgb * 65535, pixels, atol=0.003)


def test_jpeg_and_grayscale_explicit(tmp_path):
    path = tmp_path / "gray.jpg"
    Image.fromarray(np.full((8, 9), 128, dtype=np.uint8)).save(path)
    rgb, info = read_isp(path, {"transfer": "unknown"})
    assert rgb.shape == (8, 9, 3)
    assert info["grayscale_replicated"]
    np.testing.assert_array_equal(rgb[..., 0], rgb[..., 2])


def test_isp_rejections_and_bound_before_decode(tmp_path, monkeypatch):
    import camera_platform.esfr_io as io
    import tifffile

    path = tmp_path / "rgba.png"
    Image.fromarray(np.zeros((3, 4, 4), dtype=np.uint8)).save(path)
    with pytest.raises(ValueError, match="alpha"):
        read_isp(path, {"transfer": "linear"})
    with pytest.raises(ValueError, match="transfer"):
        read_isp(path, {})
    path = tmp_path / "multiple.tiff"
    with tifffile.TiffWriter(path) as writer:
        writer.write(np.zeros((2, 3), dtype=np.uint8))
        writer.write(np.zeros((2, 3), dtype=np.uint8))
    with pytest.raises(ValueError, match="one image page"):
        read_isp(path, {"transfer": "linear"})
    path = tmp_path / "bound.png"
    Image.fromarray(np.zeros((3, 4, 3), dtype=np.uint8)).save(path)
    monkeypatch.setattr(io, "MAX_ISP_PIXELS", 10)
    with pytest.raises(ValueError, match="resource limit"):
        read_isp(path, {"transfer": "linear"})


def test_input_metadata_not_mutated(tmp_path):
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(16))
    metadata = meta()
    original = copy.deepcopy(metadata)
    with RawFrame(path, metadata) as raw:
        raw.read()
    assert metadata == original
