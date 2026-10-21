"""Explicit, bounded native-coordinate image IO for measured E-SFR captures.

The CFA and four-site level arrays are anchored at the full sensor's (0, 0),
not at the acquired crop. MIPI byte order follows the Linux V4L2 packed Bayer
formats (pixfmt-srggb10p/12p/14p); partial packing groups are not inferred.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

MAX_RAW_PIXELS = 200_000_000
MAX_READ_PIXELS = 40_000_000
MAX_ISP_PIXELS = 40_000_000
MAX_FILE_BYTES = 1_073_741_824
MAX_ISP_FILE_BYTES = 268_435_456
_PACKED = {"mipi_raw10": (10, 4, 5), "mipi_raw12": (12, 2, 3), "mipi_raw14": (14, 4, 7)}
_CFAS = {"RGGB", "BGGR", "GRBG", "GBRG"}


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return int(value)


def _pair(value: Any, name: str, low: int) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} requires [row, column]")
    return [_int(x, name, low, MAX_RAW_PIXELS) for x in value]


def _levels(value: Any, name: str, max_code: int) -> list[float]:
    values = value if isinstance(value, (list, tuple)) else [value] * 4
    if len(values) != 4:
        raise ValueError(f"{name} requires a scalar or four native sites in row-major order")
    result = []
    for x in values:
        if isinstance(x, (bool, np.bool_)) or not isinstance(x, (int, float, np.number)):
            raise ValueError(f"{name} requires finite numbers")
        x = float(x)
        if not math.isfinite(x) or not 0 <= x <= max_code:
            raise ValueError(f"{name} must be in [0, {max_code}]")
        result.append(x)
    return result


def _file(path: str | Path, limit: int) -> Path:
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError(f"Image must be a nonempty regular file of at most {limit} bytes")
    return path


class RawFrame:
    """Read a declared binary Bayer frame without decoding the full image.

    ``read`` boxes use acquired-file coordinates [row, col, height, width].
    ``plane`` returns unclipped, black-subtracted DN; negative values retain
    real measurement noise. White levels are metadata, not clipping commands.
    Every call decodes at most MAX_READ_PIXELS; larger frames remain ROI-readable.
    """

    def __init__(self, path: str | Path, metadata: dict):
        if not isinstance(metadata, dict):
            raise ValueError("RAW metadata must be an object")
        fmt, geo = metadata.get("raw_format"), metadata.get("geometry")
        if not isinstance(fmt, dict) or not isinstance(geo, dict):
            raise ValueError("RAW requires raw_format and geometry objects")
        self.path = _file(path, MAX_FILE_BYTES)
        packing = fmt.get("packing")
        if packing not in {"unpacked8", "unpacked16", *_PACKED}:
            raise ValueError("Unsupported or missing RAW packing; it is never guessed")
        w = _int(fmt.get("width"), "width", 1, MAX_RAW_PIXELS)
        h = _int(fmt.get("height"), "height", 1, MAX_RAW_PIXELS)
        if h * w > MAX_RAW_PIXELS:
            raise ValueError("RAW exceeds pixel resource limit")
        bits = _int(fmt.get("bit_depth"), "bit_depth", 1, 16)
        shift = _int(fmt.get("bit_shift", 0), "bit_shift", 0, 15)
        order = fmt.get("byte_order")
        if order not in {"little", "big"}:
            raise ValueError("byte_order must explicitly be little or big")
        if packing in _PACKED:
            packed_bits, group_pixels, group_bytes = _PACKED[packing]
            if w % group_pixels:
                raise ValueError(f"{packing} width must be a multiple of {group_pixels}")
            if bits != packed_bits or shift != 0 or order != "little":
                raise ValueError("MIPI requires its declared depth, bit_shift=0, byte_order=little")
            row_bytes = (w // group_pixels) * group_bytes
        else:
            storage_bits = 8 if packing == "unpacked8" else 16
            if bits + shift > storage_bits:
                raise ValueError("bit_depth plus bit_shift exceeds storage word")
            row_bytes = w * (storage_bits // 8)
        stride = _int(fmt.get("stride_bytes"), "stride_bytes", row_bytes, MAX_FILE_BYTES)
        header = _int(fmt.get("header_bytes", 0), "header_bytes", 0, MAX_FILE_BYTES)
        expected = header + h * stride
        if expected != self.path.stat().st_size:
            raise ValueError(f"RAW file length must be exactly {expected} bytes (including stride)")
        cfa = fmt.get("cfa")
        if cfa not in _CFAS:
            raise ValueError("cfa must be RGGB, BGGR, GRBG, or GBRG at full-sensor origin")
        black = _levels(fmt.get("black_level_dn"), "black_level_dn", 2**bits - 1)
        white = _levels(fmt.get("white_level_dn"), "white_level_dn", 2**bits - 1)
        if any(b >= s for b, s in zip(black, white, strict=True)):
            raise ValueError("Every native-site black level must be below its white level")
        full = _pair(geo.get("full_sensor_shape"), "full_sensor_shape", 1)
        origin = _pair(geo.get("crop_origin"), "crop_origin", 0)
        if origin[0] + h > full[0] or origin[1] + w > full[1]:
            raise ValueError("RAW crop exceeds full sensor shape")
        self.shape = (h, w)
        self.info = {
            "decoder": "esfr_explicit_raw_v1",
            "shape": [h, w],
            "raw_format": {
                "packing": packing,
                "width": w,
                "height": h,
                "stride_bytes": stride,
                "header_bytes": header,
                "byte_order": order,
                "bit_depth": bits,
                "bit_shift": shift,
                "cfa": cfa,
                "black_level_dn": black,
                "white_level_dn": white,
            },
            "geometry": {"full_sensor_shape": full, "crop_origin": origin},
            "cfa_origin": "full_sensor_0_0",
            "level_order": "native_00_01_10_11",
            "resized": False,
            "orientation_applied": False,
            "validation": "metadata_and_exact_length; decoded_ROIs_check_declared_bits",
            "file_bytes": expected,
        }
        self._fmt = self.info["raw_format"]
        self._bytes = np.memmap(
            self.path, dtype=np.uint8, mode="r", offset=header, shape=(h, stride)
        )
        self._closed = False

    def _box(self, box: list[int] | tuple | None) -> tuple[int, int, int, int]:
        if self._closed:
            raise ValueError("RAW mapping is closed")
        if box is None:
            box = [0, 0, *self.shape]
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError("box requires [row, col, height, width]")
        r, c, h, w = [_int(x, "box", 0, MAX_RAW_PIXELS) for x in box]
        if min(h, w) < 1 or r + h > self.shape[0] or c + w > self.shape[1]:
            raise ValueError("box must be nonempty and inside acquired RAW")
        if h * w > MAX_READ_PIXELS:
            raise ValueError("Requested RAW read exceeds resource limit; use smaller ROIs")
        return r, c, h, w

    def read(self, box: list[int] | tuple | None = None) -> np.ndarray:
        r, c, h, w = self._box(box)
        fmt = self._fmt
        packing, bits = fmt["packing"], fmt["bit_depth"]
        if packing in {"unpacked8", "unpacked16"}:
            size = 1 if packing == "unpacked8" else 2
            dtype = np.dtype(
                "u1" if size == 1 else ("<u2" if fmt["byte_order"] == "little" else ">u2")
            )
            # ndarray view respects odd row strides and header offsets without reading padding.
            view = np.ndarray(
                self.shape, dtype=dtype, buffer=self._bytes, strides=(fmt["stride_bytes"], size)
            )
            words = np.array(view[r : r + h, c : c + w], dtype=np.uint16)
            used_mask = ((1 << bits) - 1) << fmt["bit_shift"]
            if np.any(np.bitwise_and(words, np.uint16(0xFFFF ^ used_mask))):
                raise ValueError(
                    "Decoded RAW ROI has nonzero bits outside declared bit_depth/shift"
                )
            return np.right_shift(words, fmt["bit_shift"])
        _, pixels, byte_count = _PACKED[packing]
        first, last = c // pixels, (c + w + pixels - 1) // pixels
        data = np.asarray(
            self._bytes[r : r + h, first * byte_count : last * byte_count], dtype=np.uint16
        ).reshape(h, last - first, byte_count)
        result = np.empty((h, last - first, pixels), dtype=np.uint16)
        low_bits = bits - 8
        # Low-order bits form one little-endian bitstream after the high-byte group.
        lows = data[..., pixels].astype(np.uint32)
        for i in range(pixels + 1, byte_count):
            lows |= data[..., i].astype(np.uint32) << (8 * (i - pixels))
        for i in range(pixels):
            result[..., i] = (data[..., i] << low_bits) | (
                (lows >> (i * low_bits)) & ((1 << low_bits) - 1)
            )
        flat = result.reshape(h, -1)
        start = c - first * pixels
        return flat[:, start : start + w].copy()

    def plane(self, box: list[int] | tuple | None, channel: str) -> tuple[np.ndarray, dict]:
        aliases = {"G0": "Gr", "G1": "Gb"}
        channel = aliases.get(channel, channel)
        if channel not in {"R", "Gr", "Gb", "B"}:
            raise ValueError("channel must be R, Gr, Gb, or B; greens are separate sublattices")
        r, c, h, w = self._box(box)
        cfa = self._fmt["cfa"]
        labels = list(cfa)
        red_row = labels.index("R") // 2
        for i, label in enumerate(labels):
            if label == "G":
                labels[i] = "Gr" if i // 2 == red_row else "Gb"
        site = labels.index(channel)
        site_r, site_c = divmod(site, 2)
        origin = self.info["geometry"]["crop_origin"]
        dr, dc = (site_r - origin[0] - r) % 2, (site_c - origin[1] - c) % 2
        if dr >= h or dc >= w:
            raise ValueError("box contains no samples of requested CFA channel")
        black = self._fmt["black_level_dn"][site]
        values = self.read([r, c, h, w])[dr::2, dc::2].astype(np.float64) - black
        sampling = {
            "channel": channel,
            "native_origin": [origin[0] + r + dr, origin[1] + c + dc],
            "pitch_native": 2,
            "native_site": [site_r, site_c],
            "shape": list(values.shape),
            "black_level_dn": black,
            "white_level_dn": self._fmt["white_level_dn"][site],
            "black_subtracted": True,
            "clipped": False,
        }
        return values, sampling

    def close(self) -> None:
        """Release the mapping; returned read/plane arrays remain independently owned."""
        if not self._closed:
            self._bytes._mmap.close()
            self._closed = True

    def __enter__(self) -> RawFrame:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _isp_size(h: int, w: int) -> None:
    if min(h, w) < 1 or h * w > MAX_ISP_PIXELS:
        raise ValueError("ISP image exceeds pixel resource limit")


def read_isp(path: str | Path, metadata: dict) -> tuple[np.ndarray, dict]:
    """Read RGB without orientation/ICC conversion/resize; honor explicit transfer.

    Unknown transfer returns normalized code values with ``not_linearized``.
    Integer 8/16-bit PNG and TIFF preserve their storage precision. Single-channel
    images are replicated and explicitly marked; alpha and multi-page TIFF are
    rejected rather than silently selecting/compositing image data.
    """
    from PIL import Image

    if not isinstance(metadata, dict) or metadata.get("transfer") not in {
        "srgb",
        "linear",
        "unknown",
    }:
        raise ValueError("ISP transfer must explicitly be srgb, linear, or unknown")
    path = _file(path, MAX_ISP_FILE_BYTES)
    transfer = metadata["transfer"]
    with path.open("rb") as stream:
        magic = stream.read(16)
    orientation, icc_present = 1, False
    if magic[:4] in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
        import tifffile

        with tifffile.TiffFile(path) as tiff:
            if len(tiff.pages) != 1:
                raise ValueError("ISP TIFF requires exactly one image page")
            page = tiff.pages[0]
            shape = page.shape
            if (
                len(shape) not in {2, 3}
                or (len(shape) == 3 and shape[-1] != 3)
                or (len(shape) == 3 and int(page.planarconfig) != 1)
                or page.dtype.kind != "u"
                or page.dtype.itemsize not in {1, 2}
            ):
                raise ValueError("ISP TIFF requires unsigned 8/16-bit gray or contiguous RGB")
            _isp_size(shape[0], shape[1])
            if int(page.photometric) not in {1, 2}:
                raise ValueError("ISP TIFF requires black-is-zero grayscale or RGB photometric")
            orientation = int(page.tags[274].value) if 274 in page.tags else 1
            icc_present = 34675 in page.tags
            codes = page.asarray(maxworkers=1)
            image_format = "TIFF"
    else:
        with Image.open(path) as im:
            if im.format not in {"JPEG", "PNG"}:
                raise ValueError("ISP supports JPEG, PNG, and TIFF only")
            _isp_size(im.height, im.width)
            if getattr(im, "n_frames", 1) != 1:
                raise ValueError("Animated or multi-frame ISP images are unsupported")
            if im.mode not in {"RGB", "L", "I", "I;16", "I;16B"}:
                raise ValueError("ISP image must be RGB or grayscale without palette or alpha")
            orientation = int(im.getexif().get(274, 1))
            icc_present = bool(im.info.get("icc_profile"))
            image_format = im.format
            if image_format == "PNG":
                # Pillow otherwise silently reduces RGB16 PNG to RGB8.
                import imagecodecs

                codes = imagecodecs.png_decode(path.read_bytes())
            else:
                codes = np.asarray(im)
    if codes.dtype.kind != "u" or codes.dtype.itemsize not in {1, 2}:
        raise ValueError("ISP requires unsigned 8/16-bit samples")
    if codes.ndim not in {2, 3} or (codes.ndim == 3 and codes.shape[-1] != 3):
        raise ValueError("ISP decoded shape must be grayscale or RGB")
    _isp_size(codes.shape[0], codes.shape[1])
    grayscale = codes.ndim == 2
    if grayscale:
        codes = np.repeat(codes[..., None], 3, axis=2)
    bits = codes.dtype.itemsize * 8
    rgb = codes.astype(np.float32) / np.float32(2**bits - 1)
    if transfer == "srgb":
        mask = rgb <= 0.04045
        rgb[mask] /= 12.92
        rgb[~mask] = ((rgb[~mask] + 0.055) / 1.055) ** 2.4
    info = {
        "decoder": "esfr_explicit_isp_v1",
        "format": image_format,
        "shape": list(rgb.shape),
        "bit_depth": bits,
        "transfer": transfer,
        "linearization": "not_linearized"
        if transfer == "unknown"
        else ("inverse_srgb" if transfer == "srgb" else "declared_linear"),
        "linear": transfer != "unknown",
        "resized": False,
        "orientation_tag": orientation,
        "orientation_applied": False,
        "icc_profile_present": icc_present,
        "icc_profile_applied": False,
        "grayscale_replicated": grayscale,
    }
    return rgb, info
