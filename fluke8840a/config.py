# -*- coding: utf-8 -*-

"""
Immutable Instrument Definitions and Application Preference Models.

This module defines the specifications for measurement functions, range 
mappings, digit resolutions, and trigger commands for the FLUKE 8840A DMM, 
along with the application runtime configuration and user settings models.

Part of the FLUKE 8840A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from types import MappingProxyType


class MeasurementFunction(IntEnum):
    VOLTAGE_DC = 1
    VOLTAGE_AC = 2
    RESISTANCE_2W = 3
    RESISTANCE_4W = 4
    CURRENT_DC = 5
    CURRENT_AC = 6


@dataclass(frozen=True)
class RangeSpec:
    label: str
    command: str | None
    status_code: int


@dataclass(frozen=True)
class FunctionSpec:
    code: MeasurementFunction
    label: str
    unit: str
    ranges: tuple[RangeSpec, ...]

    @property
    def command(self) -> str:
        return f"F{int(self.code)}"

    def range_from_status(self, code: int) -> RangeSpec:
        for item in self.ranges:
            if item.status_code == code:
                return item
        raise ValueError(f"Invalid range code {code} for {self.label}.")

    def range_from_label(self, label: str) -> RangeSpec:
        for item in self.ranges:
            if item.label == label:
                return item
        raise ValueError(f"Invalid range for {self.label}: {label}.")


_DC_VOLTAGE_RANGES = (
    RangeSpec("200 mV", "R1", 1),
    RangeSpec("2 V", "R2", 2),
    RangeSpec("20 V", "R3", 3),
    RangeSpec("200 V", "R4", 4),
    RangeSpec("1000 V", "R5", 5),
)
_AC_VOLTAGE_RANGES = (
    RangeSpec("200 mV AC", "R1", 1),
    RangeSpec("2 V AC", "R2", 2),
    RangeSpec("20 V AC", "R3", 3),
    RangeSpec("200 V AC", "R4", 4),
    RangeSpec("700 V AC", "R5", 5),
)
_CURRENT_RANGES = (RangeSpec("2000 mA", "R5", 5),)
_RESISTANCE_RANGES = tuple(
    RangeSpec(label, f"R{code}", code)
    for code, label in enumerate(("200 Ω", "2 kΩ", "20 kΩ", "200 kΩ", "2 MΩ", "20 MΩ"), 1)
)
FUNCTIONS = (
    FunctionSpec(MeasurementFunction.VOLTAGE_DC, "Voltage DC", "V", _DC_VOLTAGE_RANGES),
    FunctionSpec(MeasurementFunction.VOLTAGE_AC, "Voltage AC", "V", _AC_VOLTAGE_RANGES),
    FunctionSpec(MeasurementFunction.CURRENT_DC, "Current DC", "A", _CURRENT_RANGES),
    FunctionSpec(MeasurementFunction.CURRENT_AC, "Current AC", "A", _CURRENT_RANGES),
    FunctionSpec(MeasurementFunction.RESISTANCE_2W, "Resistance 2w", "Ω", _RESISTANCE_RANGES),
    FunctionSpec(MeasurementFunction.RESISTANCE_4W, "Resistance 4w", "Ω", _RESISTANCE_RANGES),
)
FUNCTION_BY_CODE = MappingProxyType({int(item.code): item for item in FUNCTIONS})
RATES = ("Slow", "Medium", "Fast")
TRIGGERS = (
    "Internal",
    "External with Delay",
    "Bus with Delay",
    "External without Delay",
    "Bus without Delay",
)
OPTION_COMMANDS = MappingProxyType(
    {
        "rate": MappingProxyType({label: f"S{code}" for code, label in enumerate(RATES)}),
        "offset": MappingProxyType({"OFF": "B0", "ON": "B1"}),
        "trigger": MappingProxyType({label: f"T{code}" for code, label in enumerate(TRIGGERS)}),
        "display": MappingProxyType({"ON": "D0", "OFF": "D1"}),
    }
)
ACQUISITION_INTERVALS = (
    "MAX",
    *(f"{i / 10:g}" for i in range(1, 10)),
    *(str(i) for i in range(1, 3601)),
)
EOI_MARKER = b"\x04"
CALIBRATION_MODES = MappingProxyType(
    {
        0: "Normal measurement",
        1: "A/D calibration",
        2: "Offset/gain calibration",
        4: "HF AC calibration",
    }
)
SRQ_EVENTS = (
    (1, "Overrange"),
    (2, "Reserved bit (not used)"),
    (4, "Front panel SRQ"),
    (8, "Calibration step complete"),
    (16, "Data available"),
    (32, "Any error"),
)
ERROR_MEANINGS = MappingProxyType(
    {
        0: "No error",
        **{code: f"Analog self-test {code:02d} failed" for code in range(1, 22)},
        25: "In-guard microcomputer RAM self-test failed",
        26: "Display RAM self-test failed",
        27: "In-guard program memory self-test failed",
        28: "External program memory self-test failed",
        29: "Calibration memory self-test failed",
        30: "AC function requires the True RMS AC option",
        31: "Current function requires the front inputs",
        40: "Computed calibration constant is out of range",
        41: "Calibration input is outside the acceptable range",
        42: "Calibration memory write failed",
        50: "Guard crossing error detected by the in-guard microcomputer",
        51: "Calibration command requires calibration mode",
        52: "Command is not valid at this calibration step",
        53: "Invalid calibration value in the P2 command",
        54: "Command is not valid during calibration verification",
        56: "Variable input is not allowed during this calibration step",
        60: "Device commands are not valid during self-tests",
        71: "Syntax error in a device command",
        72: "Guard crossing error detected by the out-guard microcomputer",
        73: "Input buffer overflow",
        77: "IEEE-488 interface self-test failed",
    }
)


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path = Path(__file__).resolve().parents[1]
    tool_name: str = "Fluke 8840A DMM"
    author: str = "Lorenzo Mariani"
    version_major: int = 1
    version_minor: int = 0
    version_patch: int = 0
    versione_tag: str = "-dev"
    version: str = str(version_major) + "." + str(version_minor) + "." + str(version_patch) + versione_tag
    width: int = 1400
    height: int = 788
    query_timeout: float = 1.0
    command_delay: float = 0.001
    status_delay: float = 0.5
    plot_interval_ms: int = 80
    theme_styles: tuple[str, ...] = (
        "winnative",
        "clam",
        "alt",
        "default",
        "classic",
        "vista",
        "xpnative",
    )
    audio_themes: tuple[str, ...] = ("big-sur", "chime", "mario", "material", "zelda")

    @property
    def settings_path(self) -> Path:
        return self.root_dir / "settings.ini"

    @property
    def display_font_path(self) -> Path:
        supplied = self.root_dir / "fonts" / "digital-7.ttf"
        return (
            supplied
            if supplied.is_file()
            else Path(__file__).resolve().parents[1] / "fonts" / "digital-7.ttf"
        )


@dataclass
class UserSettings:
    program_path: Path = field(default_factory=Path.home)
    theme_style: str = "vista"
    audio_theme: str = "big-sur"
