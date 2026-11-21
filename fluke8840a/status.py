# -*- coding: utf-8 -*-

"""
FLUKE 8840A Protocol Decoder for Binary Status and ASCII Measurements.

This module provides low-level parsing and validation for the instrument's responses.
It decodes the 5-byte binary status packet into precise operational flags and 
converts raw ASCII measurement strings into validated floating-point numbers.

Part of the FLUKE 8840A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import re

from .config import FUNCTION_BY_CODE
from .models import DmmDiagnostics, DmmStatus, Measurement


class ProtocolError(RuntimeError):
    pass


class InstrumentError(RuntimeError):
    """A valid Fluke error reply, distinct from a failed serial transaction."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Fluke 8840A reported error {code:02d}.")


def ascii_response(payload: bytes | str) -> str:
    try:
        text = payload.decode("ascii") if isinstance(payload, bytes) else payload
        if not isinstance(text, str) or not text.isascii():
            raise ValueError("The reply is not ASCII.")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"Invalid ASCII response: {payload!r}.") from exc
    text = text.strip()
    numeric = text.split(",", 1)[0].strip()
    error = re.fullmatch(r"\+?1\.00(\d{2})0*[Ee]\+?21", numeric)
    if error:
        raise InstrumentError(int(error[1]))
    return text


class StatusDecoder:
    """G0 = function/range/rate/trigger; G5 = 1/input/manual-range/offset."""

    def decode(
        self,
        configuration: bytes | str,
        iab: bytes | str,
        *,
        display_mode: str | None = None,
    ) -> DmmStatus:
        g0, g5 = ascii_response(configuration), ascii_response(iab)
        if not re.fullmatch(r"[1-6][1-6][0-2][0-4]", g0):
            raise ProtocolError(f"Invalid Fluke G0 response: {g0!r}; expected four FRST digits.")
        if not re.fullmatch(r"1[01]{3}", g5):
            raise ProtocolError(f"Invalid Fluke G5 response: {g5!r}; expected 1IAB.")
        function, range_code, rate, trigger = map(int, g0)
        try:
            FUNCTION_BY_CODE[function].range_from_status(range_code)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        if display_mode not in (None, "ON", "OFF"):
            raise ValueError("Invalid display state.")
        return DmmStatus(
            g0,
            g5,
            function,
            range_code,
            rate,
            trigger,
            g5[2] == "0",
            g5[3] == "1",
            g5[1] == "0",
            display_mode,
        )


class DiagnosticsDecoder:
    """Decode G1, G4, G6 and G7 without clearing the error register."""

    def decode(
        self, srq: bytes | str, calibration: bytes | str, framing: bytes | str, error: bytes | str
    ) -> DmmDiagnostics:
        g1, g4, g6, g7 = map(ascii_response, (srq, calibration, framing, error))
        if not re.fullmatch(r"[0-9]{2}", g1) or not 0 <= int(g1) <= 63:
            raise ProtocolError(f"Invalid G1 SRQ mask: {g1!r}.")
        if not re.fullmatch(r"10[01][0124]", g4):
            raise ProtocolError(f"Invalid G4 calibration status: {g4!r}.")
        if not re.fullmatch(r"10[01][0-7]", g6):
            raise ProtocolError(f"Invalid G6 suffix/terminator status: {g6!r}.")
        if not re.fullmatch(r"10[0-9]{2}", g7):
            raise ProtocolError(f"Invalid G7 error status: {g7!r}.")
        return DmmDiagnostics(
            int(g1), int(g7[2:]), int(g4[3]), g4[2] == "1", g6[2] == "1", int(g6[3])
        )

    def decode_prompt(self, payload: bytes | str) -> str:
        """G2 reports the expected analog input, not a measurement or a memory dump."""
        prompt = ascii_response(payload)
        if not re.fullmatch(
            r"[+-](?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[Ee][+-]?[0-9]+(?:\s*,\s*(?:VDC|VAC|ADC|AAC|OHM))?",
            prompt,
        ):
            raise ProtocolError(f"Invalid G2 calibration prompt: {prompt!r}.")
        value = float(prompt.split(",", 1)[0])
        if not math.isfinite(value) or abs(value) >= 1e20:
            raise ProtocolError(f"Invalid G2 calibration input: {prompt!r}.")
        return prompt


class MeasurementDecoder:
    """Numeric data are already in volts, amps or ohms, even with a suffix."""

    def decode(
        self, payload: bytes, timestamp: float, unit: str, *, function_code: int | None = None
    ) -> Measurement:
        raw = ascii_response(payload)
        fields = raw.split(",")
        if len(fields) > 2:
            raise ProtocolError(f"Invalid measurement response: {payload!r}.")
        numeric = fields[0].strip()
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", numeric):
            raise ProtocolError(f"Invalid measurement response: {payload!r}.")
        value = float(numeric)
        if not math.isfinite(value) or abs(value) >= 1e20:
            raise ProtocolError(f"Invalid measurement value: {numeric!r}.")
        suffix = "".join(fields[1].upper().split()) if len(fields) == 2 else ""
        if len(fields) == 2:
            suffix_function = suffix.removeprefix(">")
            suffix_units = {"VDC": "V", "VAC": "V", "ADC": "A", "AAC": "A", "OHM": "Ω"}
            if suffix_function not in suffix_units or suffix_units[suffix_function] != unit:
                raise ProtocolError(
                    f"Measurement suffix does not match the active function: {raw!r}."
                )
            expected = {1: "VDC", 2: "VAC", 3: "OHM", 4: "OHM", 5: "ADC", 6: "AAC"}
            if function_code is not None and suffix_function != expected.get(function_code):
                raise ProtocolError(f"Measurement suffix does not match function F{function_code}.")
        overload = suffix.startswith(">") or math.isclose(abs(value), 9.99999e9, rel_tol=1e-8)
        return Measurement(value, timestamp, unit, raw, overload, function_code)
