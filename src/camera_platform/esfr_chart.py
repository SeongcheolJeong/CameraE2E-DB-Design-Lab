"""Deterministic reconstruction of the Imatest Extended E-SFR ISO 2023 layout.

The public 640 x 370 artwork establishes feature topology, not manufacturing
coordinates or the reflectance of a particular print. Geometry is reconstructed
in artwork coordinates and rendered analytically at the requested sensor size.
No thumbnail pixels are used as scene radiance. This is not a certified printing
master. ISO algorithm conformance is a separate property of the analysis code.
"""

from __future__ import annotations

import math
from html import escape

import numpy as np
from PIL import Image, ImageDraw

SCHEMA = "camera_esfr_chart_geometry_v1"
REFERENCE_URL = (
    "https://docs.imatest.com/assets/wp-content/uploads/2018/09/23.2-12233-chart-web.png"
)
REFERENCE_SHA256 = "b2c859a750217d7e83699d90f19ccc46242d636df5753d2fc5df1d461a4be476"
BACKGROUND = 0.20
EDGE_DARK = 0.05
ARTWORK_SHAPE = (370.0, 640.0)

# Coordinates reconstructed from the official preview; the four extreme stars
# are deliberately separate from the nine original measurement fields.
_STAR_ROWS = (
    ("corner-top-left", 53, 56, -5),
    ("top-left", 160, 60, 5),
    ("top-center", 320, 57, 5),
    ("top-right", 482, 60, 5),
    ("corner-top-right", 588, 56, 5),
    ("middle-left", 180, 186, 5),
    ("center", 320, 186, 5),
    ("middle-right", 461, 186, 5),
    ("corner-bottom-left", 53, 315, 5),
    ("bottom-left", 160, 311, 5),
    ("bottom-center", 320, 314, 5),
    ("bottom-right", 482, 311, 5),
    ("corner-bottom-right", 588, 315, -5),
)
# Light-to-dark locations alternate left and right around the center ring.
_GRAY_CENTERS = (
    (309, 112),
    (331, 112),
    (287, 118),
    (353, 118),
    (265, 130),
    (376, 130),
    (253, 152),
    (388, 152),
    (247, 174),
    (394, 174),
    (247, 196),
    (394, 196),
    (253, 219),
    (388, 219),
    (265, 242),
    (376, 242),
    (287, 254),
    (353, 254),
    (309, 259),
    (331, 259),
)
_GRAY_DENSITIES = (
    0.10,
    0.16,
    0.22,
    0.28,
    0.35,
    0.41,
    0.49,
    0.57,
    0.65,
    0.74,
    0.83,
    0.93,
    1.04,
    1.16,
    1.30,
    1.44,
    1.61,
    1.80,
    2.03,
    2.30,
)
# Published nominal design targets, CIELAB D50, in Imatest table order.
_COLOR_LAB = (
    (38.36, 13.80, 14.65),
    (66.06, 17.74, 17.85),
    (50.09, -4.41, -22.51),
    (43.20, -13.46, 21.73),
    (55.36, 8.89, -24.82),
    (70.70, -32.89, -0.24),
    (82.23, 4.05, 79.84),
    (51.82, 49.79, -13.90),
    (50.55, -27.97, -28.14),
    (28.65, 15.60, -50.52),
    (55.05, -38.09, 31.62),
    (42.18, 54.89, 28.79),
    (62.56, 35.13, 58.05),
    (51.71, 47.69, 16.86),
    (30.38, 21.13, -20.31),
    (72.49, -23.46, 57.08),
    (40.18, 9.55, -44.29),
    (71.96, 19.49, 68.00),
)
# (published nominal table index, x, y). Coordinate-to-index association is a
# visual reconstruction; it is not a measured chart-specific color reference.
_COLOR_BOXES = (
    (16, 224, 67),
    (17, 247, 67),
    (11, 224, 89),
    (10, 247, 89),
    (12, 224, 112),
    (15, 394, 67),
    (9, 372, 89),
    (8, 394, 89),
    (7, 394, 112),
    (1, 224, 237),
    (2, 224, 259),
    (3, 247, 260),
    (13, 224, 282),
    (6, 394, 237),
    (4, 372, 259),
    (5, 394, 259),
    (18, 372, 282),
    (14, 394, 282),
)
# Visible wedge components, wide end first, then direction toward narrow end.
# The manufacturer's "12 pairs" is retained separately: public material does
# not resolve its pairing with the 16 visible connected shapes.
_WEDGES = (
    ("outer-left-top", 108, 101, 88, 21, -90),
    ("outer-left-middle", 110, 263, 126, 23, -90),
    ("outer-left-bottom", 110, 270, 87, 23, 90),
    ("outer-right-top", 531, 101, 88, 21, -90),
    ("outer-right-middle", 531, 109, 126, 23, 90),
    ("outer-right-bottom", 531, 270, 87, 23, 90),
    ("upper-horizontal", 222, 36, 58, 32, 0),
    ("upper-vertical", 382, 37, 47, 38, 90),
    ("left-horizontal", 84, 120, 104, 22, 0),
    ("right-horizontal", 556, 252, 104, 22, 180),
    ("lower-vertical", 258, 334, 47, 38, -90),
    ("lower-horizontal", 418, 334, 56, 32, 180),
    ("center-top", 276, 136, 88, 13, 0),
    ("center-left", 270, 229, 86, 13, -90),
    ("center-right", 370, 143, 86, 13, 90),
    ("center-bottom", 364, 236, 88, 13, 180),
)


def _lab_to_linear_rgb(lab):
    """D50 Lab -> XYZ, Bradford D50->D65, then linear sRGB display primaries."""
    lightness, a, b = lab
    fy = (lightness + 16) / 116
    f = np.array([fy + a / 500, fy, fy - b / 200], dtype=float)
    delta = 6 / 29
    xyz = np.where(f > delta, f**3, 3 * delta**2 * (f - 4 / 29))
    xyz *= [0.96422, 1.0, 0.82521]
    xyz = (
        np.array(
            [
                [0.9555766, -0.0230393, 0.0631636],
                [-0.0282895, 1.0099416, 0.0210077],
                [0.0122982, -0.0204830, 1.3299098],
            ]
        )
        @ xyz
    )
    rgb = (
        np.array(
            [
                [3.2404542, -1.5371385, -0.4985314],
                [-0.969266, 1.8760108, 0.041556],
                [0.0556434, -0.2040259, 1.0572252],
            ]
        )
        @ xyz
    )
    return np.clip(rgb, 0, 1).tolist()


def linear_to_srgb(value):
    value = np.clip(np.asarray(value), 0, 1)
    return np.where(value <= 0.0031308, 12.92 * value, 1.055 * value ** (1 / 2.4) - 0.055)


def chart_manifest():
    """Return a fresh JSON-safe layout and explicit evidence boundaries."""
    stars = [
        dict(
            id="star-" + name,
            center_xy=[x, y],
            half_extent=40,
            slant_deg=angle,
            dark_phase_deg=45 if angle < 0 else 0,
            cycles=4,
            radial_edges=8,
            role="extended_corner" if name.startswith("corner") else "core",
        )
        for name, x, y, angle in _STAR_ROWS
    ]
    gray = [
        dict(
            id=f"gray-{i + 1:02d}",
            center_xy=list(center),
            size=22,
            nominal_density=density,
            nominal_linear_reflectance=10 ** (-density),
        )
        for i, (center, density) in enumerate(zip(_GRAY_CENTERS, _GRAY_DENSITIES, strict=True))
    ]
    colors = [
        dict(
            id=f"color-{index:02d}",
            nominal_table_index=index,
            box_xywh=[x, y, 22, 22],
            nominal_lab_d50=list(_COLOR_LAB[index - 1]),
            nominal_linear_rgb=_lab_to_linear_rgb(_COLOR_LAB[index - 1]),
        )
        for index, x, y in _COLOR_BOXES
    ]
    fiducials = [
        dict(id=name, center_xy=[x, y], radius=11, center_normalized_xy=[x / 640, y / 370])
        for name, x, y in (
            ("top-left", 207, 118),
            ("top-right", 433, 118),
            ("bottom-left", 207, 254),
            ("bottom-right", 433, 254),
        )
    ]
    return dict(
        schema=SCHEMA,
        id="imatest-esfr-2023-extended-reconstruction-v1",
        revision="2023",
        title="Extended E-SFR ISO 2023 · 16:9",
        artwork_shape=list(ARTWORK_SHAPE),
        nominal_camera_aspect_ratio="16:9",
        active_box_xywh=[11, 11, 618, 348],
        framing=(
            "Uniform scale of full 640×370 artwork, centered in sensor raster; "
            "no anisotropic stretch"
        ),
        geometry_status=(
            "Reconstructed from official public preview; vendor vector master not available"
        ),
        coordinate_precision_note=(
            "coordinates estimated from the 640 px preview; "
            "exact manufacturing dimensions and tolerances unknown"
        ),
        physical_size_mm=None,
        source=dict(
            url=REFERENCE_URL,
            sha256=REFERENCE_SHA256,
            documentation="https://docs.imatest.com/docs/esfriso_instructions",
            algorithm_paper="https://www.imatest.com/wp-content/uploads/2024/03/Kerr_EI2024_IQSP_Recommendations_for_the_Detection_and_Analysis_of_the_ISO_12233-2023_e-SFR_Slanted_Star.pdf",
        ),
        stars=stars,
        side_squares=[
            dict(
                id="side-square-" + side,
                center_xy=[x, 186],
                half_extent=24,
                slant_deg=-5,
                focus_aid_radius=7,
            )
            for side, x in (("left", 58), ("right", 582))
        ],
        gray_patches=gray,
        color_patches=colors,
        fiducials=fiducials,
        orientation_dots=[
            dict(center_xy=[156, 254], radius=7),
            dict(center_xy=[178, 254], radius=7),
        ],
        wedges=[
            dict(
                id=name,
                wide_center_xy=[x, y],
                length=length,
                width=width,
                direction_deg=angle,
                frequency_ratio=18,
                transverse_cycles=5,
                design=(
                    "logarithmic transverse-frequency growth; schematic ticks; "
                    "no calibrated LW/PH labels"
                ),
            )
            for name, x, y, length, width, angle in _WEDGES
        ],
        feature_counts=dict(
            slanted_stars=13,
            core_stars=9,
            extended_corner_stars=4,
            radial_star_edges=104,
            side_squares=2,
            side_square_edges=8,
            gray_patches=20,
            color_patches=18,
            registration_fiducials=4,
            orientation_dots=2,
            visible_wedge_components=16,
        ),
        wedge_source_description="12 pairs of added wedge patterns",
        wedge_pair_mapping=(
            "unverified against 16 visible components; no invented additional patterns"
        ),
        edge_levels=dict(
            background=BACKGROUND,
            dark=EDGE_DARK,
            linear_contrast_ratio=4,
            status="declared nominal rendering values; not measured print reflectance",
        ),
        photometry=dict(
            color_target_source="https://docs.imatest.com/docs/esfriso_instructions",
            gray_target_source="https://docs.imatest.com/docs/esfriso_instructions",
            nominal_color_space="CIELAB D50",
            preview_conversion="Bradford D50→D65, linear sRGB, sRGB encoding",
            coordinate_color_mapping=(
                "visually reconstructed; verify before absolute color validation"
            ),
            actual_print_reflectance="unavailable",
            spectral_reflectance="unavailable",
            printed_chart_mtf="unavailable",
            screenshot_used_as_radiance=False,
        ),
        validation=dict(
            physical_camera_validation="not_established",
            iso_conformance="not_established",
            printing_master="not_established",
            wedge_frequency_calibration="not_established",
            focus_aids="schematic concentric/sector target; excluded from SFR ROIs",
            roi_guidance=(
                "~62 px at 2560×1440, below published nominal 100–400 px guidance; "
                "engineering workflow only"
            ),
        ),
    )


def _transform(shape):
    if len(shape) != 2 or any(isinstance(v, bool) or int(v) != v for v in shape):
        raise ValueError("shape must contain two integer dimensions")
    h, w = map(int, shape)
    if min(h, w) < 360 or h * w > 16_000_000:
        raise ValueError("Chart raster must be at least 360 px per side and at most 16 MP")
    scale = min(w / 640, h / 370)
    return h, w, scale, (w - 640 * scale) / 2, (h - 370 * scale) / 2


def _roi(center, size, shape):
    h, w, scale, ox, oy = _transform(shape)
    length = max(2, int(round(size * scale / 2)) * 2)
    x, y = center
    col = int(round((ox + x * scale - length / 2) / 2)) * 2
    row = int(round((oy + y * scale - length / 2) / 2)) * 2
    if min(row, col) < 0 or row + length > h or col + length > w:
        raise ValueError("Generated ROI lies outside native raster")
    return [row, col, length, length]


def chart_rois(shape=(1440, 2560)):
    """Native integer ROI boxes; no image warping, all 8 radial arms per star."""
    manifest = chart_manifest()
    rois = []
    for star in manifest["stars"]:
        for edge in range(8):
            angle = star["slant_deg"] + 45 * edge
            rad = math.radians(angle)
            distance = 25 if edge % 2 == 0 else 32
            center = [
                star["center_xy"][0] + distance * math.cos(rad),
                star["center_xy"][1] + distance * math.sin(rad),
            ]
            rois.append(
                dict(
                    id=f"{star['id']}-edge-{edge + 1}",
                    field_id=star["id"],
                    kind="edge",
                    box=_roi(center, 16, shape),
                    artwork_center_xy=center,
                    edge_tangent_deg=angle % 180,
                    orientation_label=f"arm-{edge + 1}",
                    feature_role=star["role"],
                    geometry_source="analytic slanted-star radial edge",
                )
            )
    for square in manifest["side_squares"]:
        angle = math.radians(square["slant_deg"])
        rotate = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
        for edge, offset in enumerate(((0, -24), (24, 0), (0, 24), (-24, 0))):
            center = (rotate @ np.asarray(offset) + square["center_xy"]).tolist()
            rois.append(
                dict(
                    id=f"{square['id']}-edge-{edge + 1}",
                    field_id=square["id"],
                    kind="edge",
                    box=_roi(center, 16, shape),
                    artwork_center_xy=center,
                    edge_tangent_deg=(square["slant_deg"] + 90 * edge) % 180,
                    orientation_label=f"square-side-{edge + 1}",
                    feature_role="side_square",
                    geometry_source="analytic rotated square edge",
                )
            )
    for patch in manifest["gray_patches"]:
        rois.append(
            dict(
                id=patch["id"],
                field_id=patch["id"],
                kind="gray",
                box=_roi(patch["center_xy"], 14, shape),
                artwork_center_xy=patch["center_xy"],
            )
        )
    for patch in manifest["color_patches"]:
        x, y, width, height = patch["box_xywh"]
        center = [x + width / 2, y + height / 2]
        rois.append(
            dict(
                id=patch["id"],
                field_id=patch["id"],
                kind="color",
                box=_roi(center, 14, shape),
                artwork_center_xy=center,
                nominal_table_index=patch["nominal_table_index"],
            )
        )
    return rois


rois_for_shape = chart_rois


def _primitives():
    m = chart_manifest()
    items = []

    def poly(points, value, feature):
        rgb = [value] * 3 if np.isscalar(value) else list(value)
        items.append(
            dict(kind="polygon", points=[list(p) for p in points], rgb=rgb, feature=feature)
        )

    def rect(x, y, width, height, value, feature):
        poly(((x, y), (x + width, y), (x + width, y + height), (x, y + height)), value, feature)

    def circle(x, y, radius, value, feature):
        items.append(
            dict(
                kind="ellipse",
                box=[x - radius, y - radius, x + radius, y + radius],
                rgb=[value] * 3,
                feature=feature,
            )
        )

    rect(11, 11, 618, 348, BACKGROUND, "active-area")
    for star in m["stars"]:
        cx, cy = star["center_xy"]
        for sector in range(4):
            points = [[cx, cy]]
            start = star["slant_deg"] + star["dark_phase_deg"] + 90 * sector
            for theta in (start, start + 45):
                rad = math.radians(theta)
                radius = star["half_extent"] / max(abs(math.cos(rad)), abs(math.sin(rad)))
                points.append([cx + radius * math.cos(rad), cy + radius * math.sin(rad)])
            poly(points, EDGE_DARK, star["id"])
    for square in m["side_squares"]:
        cx, cy = square["center_xy"]
        a = math.radians(square["slant_deg"])
        rotate = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        points = (
            np.array([[-24, -24], [24, -24], [24, 24], [-24, 24]]) @ rotate.T + [cx, cy]
        ).tolist()
        poly(points, EDGE_DARK, square["id"])
        circle(cx, cy, 7, 0.65, square["id"] + "-focus")
        circle(cx, cy, 6.5, EDGE_DARK, square["id"] + "-focus")
        for i in range(18):
            a0, a1 = 2 * math.pi * i / 18, 2 * math.pi * (i + 0.5) / 18
            poly(
                (
                    (cx, cy),
                    (cx + 6 * math.cos(a0), cy + 6 * math.sin(a0)),
                    (cx + 6 * math.cos(a1), cy + 6 * math.sin(a1)),
                ),
                0.65,
                square["id"] + "-focus",
            )
        for radius in (2, 4):
            circle(cx, cy, radius, EDGE_DARK, square["id"] + "-focus")
            circle(cx, cy, radius - 0.35, 0.40, square["id"] + "-focus")
    for patch in m["gray_patches"]:
        x, y = patch["center_xy"]
        rect(x - 11, y - 11, 22, 22, patch["nominal_linear_reflectance"], patch["id"])
    for patch in m["color_patches"]:
        rect(*patch["box_xywh"], patch["nominal_linear_rgb"], patch["id"])
    for wedge in m["wedges"]:
        cx, cy = wedge["wide_center_xy"]
        a = math.radians(wedge["direction_deg"])
        u, v = np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])
        t = np.linspace(0, wedge["length"], 65)
        # Reciprocal stripe spacing grows exponentially along the axis. The
        # rendering law is declared, but no vendor LPH scale is inferred.
        half_width = (
            wedge["width"] / 2 * np.exp(-np.log(wedge["frequency_ratio"]) * t / wedge["length"])
        )
        bands = wedge["transverse_cycles"] * 2
        for band in range(bands):
            lo, hi = -1 + 2 * band / bands, -1 + 2 * (band + 1) / bands
            lower = np.array([cx, cy]) + t[:, None] * u + (lo * half_width)[:, None] * v
            upper = np.array([cx, cy]) + t[:, None] * u + (hi * half_width)[:, None] * v
            poly(
                np.concatenate([lower, upper[::-1]]).tolist(),
                0.92 if band % 2 else 0.006,
                wedge["id"],
            )
        # Unlabelled marks preserve the visual ruler but make no frequency claim.
        for tick in range(16):
            along = wedge["length"] * tick / 15
            p = np.array([cx, cy]) + along * u + (wedge["width"] / 2 + 2) * v
            q = p + (2 if tick % 5 else 3) * v
            r = 0.25 * u
            poly((p - r, p + r, q + r, q - r), 0.015, wedge["id"] + "-ruler")
    for f in m["fiducials"]:
        cx, cy = f["center_xy"]
        circle(cx, cy, f["radius"], 0.003, "fiducial-" + f["id"])
        for begin in (0, math.pi):
            a = np.linspace(begin, begin + math.pi / 2, 33)
            poly(
                [[cx, cy]]
                + np.stack(
                    [cx + f["radius"] * np.cos(a), cy + f["radius"] * np.sin(a)], axis=1
                ).tolist(),
                0.95,
                "fiducial-" + f["id"],
            )
    for dot in m["orientation_dots"]:
        circle(*dot["center_xy"], dot["radius"], 0.003, "orientation-dot")
    # Frame, asymmetric alignment arrows, and 16:9 corner guides are separate
    # from the active image field. No fabricated dimension/frequency labels.
    for x in (53, 89, 320, 551, 587):
        poly(((x - 4, 0), (x + 4, 0), (x, 11)), 0.95, "alignment-top")
        poly(((x - 4, 370), (x + 4, 370), (x, 359)), 0.95, "alignment-bottom")
    for y in (17, 186, 353):
        poly(((0, y - 4), (0, y + 4), (11, y)), 0.95, "alignment-left")
        poly(((640, y - 4), (640, y + 4), (629, y)), 0.95, "alignment-right")
    for x, direction in ((12, 1), (628, -1)):
        for y in (13, 357):
            poly(
                ((x, y), (x + direction * 11, y), (x + direction * 11, y + (2 if y < 20 else -2))),
                0.55,
                "corner-guide",
            )
    return items


def render_chart(shape=(1080, 1920), supersample=4):
    """Return ``(linear_rgb, native_rois, manifest)`` with bounded tile rendering.

    All opaque vector components are composed in their actual painter order at
    supersampled resolution, then each tile is averaged once. This avoids the
    incorrect independent-coverage blend at adjoining black/white wedge bands.
    Each float plane is at most 1024×1024, independent of output dimensions.
    """
    h, w, scale, ox, oy = _transform(shape)
    if (
        isinstance(supersample, bool)
        or int(supersample) != supersample
        or not 1 <= supersample <= 8
    ):
        raise ValueError("supersample must be an integer between 1 and 8")
    ss = int(supersample)
    tile_size = min(256, 1024 // ss)
    out = np.empty((h, w, 3), dtype=np.float32)
    primitives = []
    for original in _primitives():
        p = dict(original)
        if p["kind"] == "polygon":
            p["pixels"] = np.asarray(p["points"]) * scale + [ox, oy]
            p["lo"], p["hi"] = p["pixels"].min(axis=0), p["pixels"].max(axis=0)
        else:
            p["pixels"] = np.asarray(p["box"]) * scale + [ox, oy, ox, oy]
            p["lo"], p["hi"] = p["pixels"][:2], p["pixels"][2:]
        primitives.append(p)
    for ty in range(0, h, tile_size):
        for tx in range(0, w, tile_size):
            tw, th = min(tile_size, w - tx), min(tile_size, h - ty)
            overlapping = [
                p
                for p in primitives
                if p["lo"][0] <= tx + tw
                and p["hi"][0] >= tx
                and p["lo"][1] <= ty + th
                and p["hi"][1] >= ty
            ]
            geometry = []
            for p in overlapping:
                if p["kind"] == "polygon":
                    points = ((p["pixels"] - [tx, ty]) * ss - 0.5).tolist()
                    geometry.append((p, [tuple(point) for point in points]))
                else:
                    geometry.append((p, tuple((p["pixels"] - [tx, ty, tx, ty]) * ss - 0.5)))
            for channel in range(3):
                plane = Image.new("F", (tw * ss, th * ss), 0.003)
                draw = ImageDraw.Draw(plane)
                for p, xy in geometry:
                    if p["kind"] == "polygon":
                        draw.polygon(xy, fill=float(p["rgb"][channel]))
                    else:
                        draw.ellipse(xy, fill=float(p["rgb"][channel]))
                out[ty : ty + th, tx : tx + tw, channel] = np.asarray(
                    plane.resize((tw, th), Image.Resampling.BOX), dtype=np.float32
                )
    manifest = chart_manifest()
    manifest["raster"] = dict(
        shape=[h, w],
        artwork_scale=scale,
        artwork_offset_xy=[ox, oy],
        supersample=ss,
        pixel_integration="joint opaque vector composition per supersampled tile, one area average",
        max_tile_native_px=tile_size,
        max_supersampled_plane_side_px=1024,
        linear_rgb_dtype="float32",
        rendering="vector_geometry_not_thumbnail_resize",
    )
    rois = chart_rois(shape)
    manifest["raster"]["minimum_edge_roi_size_px"] = min(
        r["box"][2] for r in rois if r["kind"] == "edge"
    )
    return out, rois, manifest


def svg_document(width=1920, overlay=False):
    """Standalone vector preview of full artwork with optional numbered ROIs."""
    if isinstance(width, bool) or not np.isfinite(width) or not 320 <= width <= 20000:
        raise ValueError("SVG width must be between 320 and 20000")
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(width)}" '
        f'height="{width * 370 / 640:g}" viewBox="0 0 640 370" role="img" '
        'aria-label="Reconstructed Extended E-SFR ISO 2023 chart">',
        "<title>Extended E-SFR ISO 2023 layout reconstruction</title>",
        "<desc>Analytic geometry based on the official preview. Nominal color targets; "
        "physical print and ISO conformance not established.</desc>",
        '<rect width="640" height="370" fill="#0a0a0a"/>',
    ]
    for p in _primitives():
        rgb = np.rint(linear_to_srgb(p["rgb"]) * 255).astype(int)
        color = "#" + "".join(f"{c:02x}" for c in rgb)
        name = escape(p["feature"], quote=True)
        if p["kind"] == "polygon":
            coords = " ".join(f"{x:.5f},{y:.5f}" for x, y in p["points"])
            svg.append(f'<polygon points="{coords}" fill="{color}" data-feature="{name}"/>')
        else:
            x0, y0, x1, y1 = p["box"]
            svg.append(
                f'<ellipse cx="{(x0 + x1) / 2:g}" cy="{(y0 + y1) / 2:g}" '
                f'rx="{(x1 - x0) / 2:g}" ry="{(y1 - y0) / 2:g}" '
                f'fill="{color}" data-feature="{name}"/>'
            )
    if overlay:
        for roi in chart_rois((1480, 2560)):
            row, col, rh, rw = roi["box"]
            color = "#00e9ff" if roi["kind"] == "edge" else "#ffd34e"
            svg.append(
                f'<g><title>{escape(roi["id"])}</title><rect x="{col / 4:g}" '
                f'y="{row / 4:g}" width="{rw / 4:g}" height="{rh / 4:g}" '
                f'fill="none" stroke="{color}" stroke-width=".45"/></g>'
            )
    svg.append("</svg>")
    return "\n".join(svg)
