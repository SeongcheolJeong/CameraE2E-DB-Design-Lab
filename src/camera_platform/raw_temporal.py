"""Opt-in causal, motion-guarded pair filtering of delivered Bayer RAW codes.

This is signal processing, not charge reconstruction or an acquisition mode.
It uses only the current and three past original RAW frames. It cannot tell
small genuine period-two scene changes from period-two sensor modulation.
No trap state, illumination truth, future frame or filtered feedback is used.
Existing registered ISP profiles are deliberately unchanged.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math

import numpy as np

MAX_PIXELS = 1_048_576


@dataclass(frozen=True)
class PairFilterConfig:
    """Thresholds in delivered DN; noise_sigma_dn is a declared estimate."""

    absolute_threshold_dn: float = 2.0
    relative_threshold: float = 0.01
    noise_sigma_dn: float = 0.0
    noise_multiplier: float = 3.0
    maximum_pair_fraction: float = 0.15

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.relative_threshold > 1 or not 0 < self.maximum_pair_fraction <= 1:
            raise ValueError("Relative thresholds must not exceed one")


class CausalRawPairFilter:
    """Preserve a fixed cadence; bypass changes instead of averaging old edges.

    When both phase pairs (t,t-2) and (t-1,t-3) agree, blend t with t-1.
    The gate is shared by each 2x2 Bayer cell, but values never cross sites.
    First three frames after reset pass unchanged. A blended pixel contains
    50% previous-frame information (nominal age cadence/2), even though no
    future frame is awaited. Callers must not label this zero temporal delay.
    """

    def __init__(self, *, black_level_dn: float, white_level_dn: int,
                 cadence_ns: int, config: PairFilterConfig | None = None):
        if (type(white_level_dn) is not int or not 1 <= white_level_dn <= 2**24 - 1
                or type(black_level_dn) not in (int, float)
                or not math.isfinite(black_level_dn)
                or not 0 <= black_level_dn < white_level_dn):
            raise ValueError("Require valid declared black and integer white codes")
        if type(cadence_ns) is not int or not 1 <= cadence_ns < 2**62:
            raise ValueError("Require a positive integer cadence in ns")
        if config is not None and not isinstance(config, PairFilterConfig):
            raise TypeError("config must be PairFilterConfig")
        self.black = float(black_level_dn)
        self.white = white_level_dn
        self.cadence_ns = cadence_ns
        self.config = config or PairFilterConfig()
        self._history: deque[np.ndarray] = deque(maxlen=3)
        self._last_time: int | None = None
        self._settings: str | None = None
        self._shape: tuple[int, int] | None = None

    def reset(self) -> None:
        self._history.clear()
        self._last_time = None
        self._settings = None
        self._shape = None

    def push(self, raw: np.ndarray, *, timestamp_ns: int, settings_key: str) -> dict:
        """Return float code DN and an explicit blend mask; leave raw untouched.

        timestamp_ns must use one consistent acquisition reference. settings_key
        must change with exposure, readout gain/path, CFA geometry or PD clear.
        No code clipping is applied: the output is a convex pair of input codes.
        """
        if (not isinstance(raw, np.ndarray) or raw.ndim != 2 or raw.dtype.kind not in "ui"
                or any(n < 2 or n % 2 for n in raw.shape) or raw.size > MAX_PIXELS):
            raise ValueError("Require bounded even-height/width original integer Bayer RAW")
        if np.any(raw < 0) or np.any(raw > self.white):
            raise ValueError("RAW code outside the declared ADC range")
        if type(timestamp_ns) is not int or not 0 <= timestamp_ns < 2**62:
            raise ValueError("timestamp_ns must be a nonnegative bounded integer")
        if not isinstance(settings_key, str) or not settings_key or len(settings_key) > 512:
            raise ValueError("Require an explicit bounded acquisition settings key")
        if self._last_time is not None and timestamp_ns <= self._last_time:
            raise ValueError("Frame timestamps must increase; call reset for a new acquisition")
        reason = "none"
        if self._last_time is None:
            reason = "initial"
        elif timestamp_ns - self._last_time != self.cadence_ns:
            reason = "cadence_change"
        elif settings_key != self._settings:
            reason = "settings_change"
        elif raw.shape != self._shape:
            reason = "geometry_change"
        if reason != "none":
            self._history.clear()
        current = raw.astype(np.float64)
        output = current.copy()
        gate = np.zeros(raw.shape, dtype=bool)
        ready = len(self._history) == 3
        if ready:
            oldest, two_back, previous = self._history
            scale = np.maximum.reduce([current - self.black, previous - self.black,
                                       two_back - self.black, oldest - self.black,
                                       np.zeros_like(current)])
            noise = self.config.noise_multiplier * math.sqrt(2) * self.config.noise_sigma_dn
            tolerance = (self.config.absolute_threshold_dn
                         + self.config.relative_threshold * scale + noise)
            stable = ((np.abs(current - two_back) <= tolerance)
                      & (np.abs(previous - oldest) <= tolerance)
                      & (np.abs(current - previous)
                         <= self.config.absolute_threshold_dn
                         + self.config.maximum_pair_fraction * scale + noise))
            for frame in (current, previous, two_back, oldest):
                stable &= (frame > 0) & (frame < self.white)
            # A moving/color edge in any site vetoes the cell. No spatial values
            # are averaged: this only shares the conservative decision mask.
            h, w = raw.shape
            cell = stable.reshape(h // 2, 2, w // 2, 2).all(axis=(1, 3))
            gate = np.repeat(np.repeat(cell, 2, axis=0), 2, axis=1)
            output[gate] = 0.5 * (current[gate] + previous[gate])
        self._history.append(current)
        self._last_time, self._settings, self._shape = timestamp_ns, settings_key, raw.shape
        return {"code_dn": output, "blend_mask": gate, "history_ready": ready,
                "reset_reason": reason, "timestamp_ns": timestamp_ns,
                "history_frames_available": len(self._history),
                "future_frames_used": 0,
                "nominal_blended_sample_age_ns": self.cadence_ns / 2}


def process_sequence(raw: np.ndarray, *, black_level_dn: float, white_level_dn: int,
                     timestamps_ns: list[int], cadence_ns: int,
                     settings_keys: list[str], config: PairFilterConfig | None = None) -> dict:
    """Convenience batch adapter over the exact same streaming implementation."""
    if (not isinstance(raw, np.ndarray) or raw.ndim != 3 or not 1 <= len(raw) <= 64
            or raw.size > 16 * MAX_PIXELS):
        raise ValueError("Require a bounded [frame,row,column] RAW sequence")
    if len(timestamps_ns) != len(raw) or len(settings_keys) != len(raw):
        raise ValueError("One timestamp and settings key per original frame is required")
    processor = CausalRawPairFilter(black_level_dn=black_level_dn,
                                  white_level_dn=white_level_dn,
                                  cadence_ns=cadence_ns, config=config)
    frames = [processor.push(frame, timestamp_ns=timestamp, settings_key=key)
              for frame, timestamp, key in zip(raw, timestamps_ns, settings_keys, strict=True)]
    return {"code_dn": np.stack([f["code_dn"] for f in frames]),
            "blend_mask": np.stack([f["blend_mask"] for f in frames]),
            "reset_reasons": [f["reset_reason"] for f in frames],
            "settings": asdict(processor.config),
            "future_frames_used": 0,
            "nominal_blended_sample_age_ns": cadence_ns / 2,
            "scope": "processed_delivered_codes_not_new_RAW_acquisition_or_charge_inverse"}
