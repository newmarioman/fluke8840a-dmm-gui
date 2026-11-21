"""Instrument snapshots and bounded acquisition data independent of Tkinter."""

import math
import threading
from collections import deque
from dataclasses import dataclass

# Local Packages
from .config import (
    CALIBRATION_MODES,
    ERROR_MEANINGS,
    FUNCTION_BY_CODE,
    RATES,
    SRQ_EVENTS,
    TRIGGERS,
    FunctionSpec,
    RangeSpec,
)


@dataclass(frozen=True)
class DmmStatus:
    configuration: str
    iab: str
    function_code: int
    range_code: int
    rate_code: int
    trigger_code: int
    autorange: bool
    offset: bool
    front_input: bool
    display_mode: str | None = None

    @property
    def function(self) -> FunctionSpec:
        return FUNCTION_BY_CODE[self.function_code]

    @property
    def range(self) -> RangeSpec:
        return self.function.range_from_status(self.range_code)

    @property
    def rate(self) -> str:
        return RATES[self.rate_code]

    @property
    def digits(self) -> str:
        return "4½" if self.rate_code == 2 else "5½"

    @property
    def trigger_label(self) -> str:
        return TRIGGERS[self.trigger_code]

    @property
    def rear_trigger_enabled(self) -> bool:
        return self.trigger_code in (1, 3)

    @property
    def settling_delay_enabled(self) -> bool:
        return self.trigger_code in (1, 2)


@dataclass(frozen=True)
class DmmDiagnostics:
    srq_mask: int
    error_code: int
    calibration_mode: int
    calibration_verification: bool
    suffix_enabled: bool
    terminator_code: int
    calibration_prompt: str | None = None
    prompt_error_code: int | None = None

    @property
    def error_meaning(self) -> str:
        return ERROR_MEANINGS.get(self.error_code, "Undocumented error code")

    @property
    def srq_events(self) -> tuple[str, ...]:
        return tuple(label for bit, label in SRQ_EVENTS if self.srq_mask & bit)

    @property
    def calibration_label(self) -> str:
        return CALIBRATION_MODES[self.calibration_mode]

    @property
    def prompt_available(self) -> bool:
        return self.calibration_mode != 0 and not self.calibration_verification

    @property
    def prompt_label(self) -> str:
        if self.calibration_prompt is not None:
            return self.calibration_prompt
        if self.prompt_error_code is not None:
            meaning = ERROR_MEANINGS.get(self.prompt_error_code, "Undocumented error code")
            return f"Error {self.prompt_error_code:02d} — {meaning}"
        if self.calibration_verification:
            return "Not available during calibration verification"
        return "Not available in normal measurement mode"


@dataclass(frozen=True)
class ConnectionInfo:
    controller_name: str
    instrument_name: str = "Fluke 8840A"


@dataclass(frozen=True)
class Measurement:
    value: float
    timestamp: float
    unit: str
    raw: str = ""
    overload: bool = False
    function_code: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("Measurements require a finite value and a nonnegative timestamp.")

    @property
    def function_label(self) -> str:
        spec = FUNCTION_BY_CODE.get(self.function_code)
        return spec.label if spec else "Measurement"

    @property
    def display_text(self) -> str:
        return f"{self.format_value()} {self.unit}"

    def format_value(self) -> str:
        if self.overload:
            return "OVERLOAD"
        formatted = f"{self.value:+.{9}f}"
        if "." in formatted:
            parts = formatted.split(".")
            integer_part = parts[0]
            decimal_part = parts[1].rstrip("0")
            if decimal_part:
                return f"{integer_part}.{decimal_part}"
            return integer_part
        return formatted

class TraceBuffer:
    """Retain the latest samples while counting every acquired reading."""

    def __init__(self, capacity: int = 100) -> None:
        if (
            not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or not 2 <= capacity <= 10000
        ):
            raise ValueError("Plot history must contain from 2 to 10000 samples.")
        self.capacity = capacity
        self._samples: deque[Measurement] = deque(maxlen=capacity)
        self._count = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def append(self, sample: Measurement) -> None:
        with self._lock:
            if self._samples and sample.timestamp < self._samples[-1].timestamp:
                raise ValueError("Acquisition timestamps must be nondecreasing.")
            self._samples.append(sample)
            self._count += 1

    def snapshot(self) -> tuple[Measurement, ...]:
        with self._lock:
            return tuple(self._samples)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._count = 0
