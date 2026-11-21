# -*- coding: utf-8 -*-

"""
FLUKE 8840A Digital Multimeter Command Serializer and Protocol Decoder.

This module encapsulates the core logical layer for managing transaction states,
sending safe ASCII instrument sequences, tracking trigger/display modes, and 
handling communication session contexts for the FLUKE 8840A DMM hardware.

Part of the FLUKE 8840A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace

# Local Packages
from .config import EOI_MARKER, FUNCTION_BY_CODE, OPTION_COMMANDS
from .models import ConnectionInfo, DmmDiagnostics, DmmStatus, Measurement
from .status import (
    DiagnosticsDecoder,
    InstrumentError,
    MeasurementDecoder,
    ProtocolError,
    StatusDecoder,
    ascii_response,
)
from .transport import InstrumentTransport, TransportError


class QueryTimeout(TimeoutError):
    """A response deadline expired, with the command kept for diagnostics."""

    def __init__(self, message: str, *, command: str = "") -> None:
        super().__init__(message)
        self.command = command


class Fluke8840A:
    MAX_CONSECUTIVE_TIMEOUTS = 5
    def __init__(
        self,
        transport: InstrumentTransport,
        *,
        query_timeout: float = 1.0,
        command_delay: float = 0.001,
        status_delay: float = 0.5,
        trace: Callable[[str, str], None] | None = None,
        activity: Callable[[str], None] | None = None,
    ) -> None:
        for name, value in (
            ("query timeout", query_timeout),
            ("command delay", command_delay),
            ("status delay", status_delay),
        ):
            if not math.isfinite(value) or value < 0 or (name == "query timeout" and value == 0):
                raise ValueError(f"Invalid {name}.")
        self.transport = transport
        self.status_delay = status_delay
        self.query_timeout, self.command_delay = (
            query_timeout,
            command_delay,
        )
        self.trace = trace or (lambda direction, text: None)
        self.activity = activity or (lambda direction: None)
        self._lock = threading.RLock()
        self._status: DmmStatus | None = None
        self._info: ConnectionInfo | None = None
        self._diagnostics: DmmDiagnostics | None = None
        self._connected, self._display_mode = False, None
        self.status_decoder = StatusDecoder()
        self.measurement_decoder = MeasurementDecoder()
        self.diagnostics_decoder = DiagnosticsDecoder()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> DmmStatus | None:
        return self._status

    @property
    def connection_info(self) -> ConnectionInfo | None:
        return self._info

    @property
    def diagnostics(self) -> DmmDiagnostics | None:
        return self._diagnostics

    @contextmanager
    def _session(self, require_connection: bool = True, *, keep_connection_on_timeout: bool = False):
        with self._lock:
            if require_connection and not self._connected:
                raise TransportError("Connect to the DMM first.")
            try:
                yield
            except QueryTimeout:
                if not keep_connection_on_timeout:
                    self._release()
                raise
            except (TransportError, ProtocolError):
                self._release()
                raise

    def _release(self) -> None:
        self._connected, self._status, self._info, self._diagnostics = False, None, None, None
        self._display_mode = None
        self.transport.close()

    def connect(self, port: str, address: int) -> DmmStatus:
        with self._session(False):
            if self._connected:
                raise ValueError("The DMM is already connected.")
            stage = "opening the serial port"
            try:
                self.transport.open(port, address)
                stage = "reading the controller version"
                self._write("++ver")
                version = self._read_controller_line().decode("ascii", errors="replace").strip()
                if not version or "unrecognized command" in version.lower():
                    raise ProtocolError(f"Invalid AR488 version response: {version!r}.")
                self._received(f"Controller: {version}")
                # Follow the supplied Fluke program, including its reset and ID query.
                self._write("*")
                self._display_mode = "ON"
                stage = "reading the Fluke identification (G8)"
                identification = ascii_response(self._query("G8"))
                fields = [field.strip() for field in identification.split(",")]
                if (
                    len(fields) != 4
                    or fields[0].upper() != "FLUKE"
                    or fields[1].upper() != "8840A"
                    or fields[2] != "0"
                    or not fields[3]
                ):
                    raise ProtocolError(
                        f"Expected a Fluke 8840A G8 identification; received {identification!r}."
                    )
                stage = "reading the Fluke status registers"
                status = self._read_status()
            except Exception as exc:
                self._release()
                raise TransportError(
                    f"Connection failed on {port}, GPIB address {address}, while {stage}: {exc}"
                ) from exc
            self._info, self._connected = ConnectionInfo(version, identification), True
            return status

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self._connected:
                    self._write("X0")
                    self._write("*")
                    self._write("++loc")
            finally:
                self._release()

    def refresh_status(self) -> DmmStatus:
        with self._session():
            return self._read_status()

    def select_function(self, code: int) -> DmmStatus:
        if type(code) is not int or code not in FUNCTION_BY_CODE:
            raise ValueError("Select one of the six Fluke measurement functions.")
        with self._session():
            self._write(FUNCTION_BY_CODE[code].command)
            self._write("R0")
            return self._read_status()

    def set_option(self, key: str, value: str) -> DmmStatus:
        with self._session():
            if self._status is None:
                raise ProtocolError("Read the DMM status before changing settings.")
            if key == "range":
                command = self._status.function.range_from_label(value).command
            elif key == "autorange":
                if value not in ("ON", "OFF"):
                    raise ValueError("Select ON or OFF for autorange.")
                command = "R0" if value == "ON" else "R7"
            else:
                try:
                    command = OPTION_COMMANDS[key][value]
                except KeyError as exc:
                    raise ValueError(f"Invalid {key} setting: {value}.") from exc
            self._write(command)
            if key == "display":
                self._display_mode = value
            return self._read_status()

    def reset(self) -> DmmStatus:
        with self._session():
            self._write("*")
            self._display_mode, self._diagnostics = "ON", None
            return self._read_status()

    def clear_errors(self) -> DmmStatus:
        with self._session():
            self._write("X0")
            self._diagnostics = None
            return self._read_status()

    def read_diagnostics(self) -> DmmDiagnostics:
        with self._session():
            self._read_status()
            return self._diagnostics

    def read_measurement(self, timestamp: float | Callable[[], float] = 0.0, *, keep_connection_on_timeout: bool = False) -> Measurement:
        with self._session(keep_connection_on_timeout=keep_connection_on_timeout):
            status = self._status
            if status is None:
                raise ProtocolError("Read the DMM status before acquisition.")
            if status.trigger_code != 0:
                self._write("?")
            deadline = time.monotonic() + self.query_timeout
            self._write("++read")
            try:
                with self.transport.measurement_reader() as reader:
                    payload = self._read_frame(deadline, reader)
            except QueryTimeout:
                if keep_connection_on_timeout:
                    # A partial reply must not become part of the next sample.
                    self.transport.discard_input()
                raise               
            self._received(payload.decode("ascii", errors="replace").strip())
            elapsed = timestamp() if callable(timestamp) else timestamp
            return self.measurement_decoder.decode(
                payload, elapsed, status.function.unit, function_code=status.function_code
            )
        
    def send_custom(self, command: str, query: bool = False) -> str | None:
        command = command.strip()
        if not command or not command.isascii() or any(char in command for char in "\r\n"):
            raise ValueError("Enter one ASCII command without line breaks.")
        head = command.split()[0].lower()
        if head in {
            "++auto",
            "++eot_enable",
            "++eot_char",
            "++eos",
            "++eoi",
            "++eor",
            "++mode",
            "++addr",
            "++rst",
        }:
            raise ValueError(
                "Connection and message framing settings are managed by the application."
            )
        compact = command.upper().replace(" ", "").replace(",", "")
        # Keep line termination available to the driver's readline API.
        if not head.startswith("++") and re.search(r"W[1-7]", compact):
            raise ValueError(
                "Use W0 (CR/LF/EOI); other output terminators are not supported by this application."
            )
        with self._session():
            response = None
            if head == "++read":
                self._write(command)
                response = self._describe_response(self._read_frame(time.monotonic() + self.query_timeout))
            elif head == "++ver" or (head.startswith("++") and query):
                self._write(command)
                response = self._describe_response(self._read_controller_line())
            else:
                self._write(command)
                if query:
                    self._write("++read")
                    response = self._describe_response(self._read_frame(time.monotonic() + self.query_timeout))
                if compact in ("D0", "D1"):
                    self._display_mode = "ON" if compact == "D0" else "OFF"
                elif compact == "*" or head == "++clr":
                    self._display_mode, self._diagnostics = "ON", None
                elif not head.startswith("++") and not re.fullmatch( r"(?:[FRSTBYXG][0-9]|\?|W0)", compact):
                    self._display_mode = "ON"
            if response is not None:
                self._received(response)
            self._read_status()
            return response

    def _write(self, command: str) -> None:
        self.activity("TX")
        self.transport.write(command)
        self.trace("OUT", command)
        if self.command_delay:
            time.sleep(self.command_delay)

    def _received(self, text: str) -> None:
        self.activity("RX")
        self.trace("IN", text)

    def _clean_autorange_queue(self):
        actual_trigger = "T" + str(self._status.trigger_code)
        self._write("T1")
        time.sleep(0.2)
        for _ in range(3):
            self._write("++read")
            time.sleep(0.05)
            self.transport.read(4096) 
        if hasattr(self, '_buffer'):
            self._buffer.clear()
        self._write(actual_trigger)
        time.sleep(0.2)

    def _query(
        self,
        command: str,
        deadline: float | None = None,
        reader: Callable[[int], bytes] | None = None,
    ) -> str:
        attempts = 1 if deadline is not None else self.MAX_CONSECUTIVE_TIMEOUTS
        for attempt in range(1, attempts + 1):
            response_deadline = deadline if deadline is not None else time.monotonic() + self.query_timeout
            self._write(command)
            self._write("++read")
            try:
                if reader is None:
                    with self.transport.measurement_reader() as response_reader:
                        payload = self._read_frame(response_deadline, response_reader)
                else:
                    payload = self._read_frame(response_deadline, reader)
            except QueryTimeout as exc:
                timeout = QueryTimeout(f"{command}: {exc}", command=command)
                if deadline is not None:
                    raise timeout from exc
                self.trace(
                    "WARN",
                    f"{timeout} ({attempt}/{self.MAX_CONSECUTIVE_TIMEOUTS} consecutive).",
                )
                if attempt == attempts:
                    raise QueryTimeout(
                        f"{attempt} consecutive response timeouts (timeout {self.query_timeout:g} s each). "
                        f"Last command: {command}. Connection closed. {exc}",
                        command=command,
                    ) from exc
                self._discard_incomplete_response()
            except TransportError as exc:
                raise TransportError(f"{command}: {exc}") from exc
            else:
                self._received(payload.decode("ascii", errors="replace"))
                ascii_response(payload)
                return payload

    def _discard_incomplete_response(self) -> None:
        self._write("++!")
        self.transport.discard_input()

    def _read_status(self) -> DmmStatus:
        # The one-second wait precedes G0, not the parsing of an old response.
        if self.status_delay:
            time.sleep(self.status_delay)
        configuration, iab = self._query("G0"), self._query("G5")
        status = self.status_decoder.decode(configuration, iab, display_mode=self._display_mode)
        diagnostics = self._read_diagnostics()
        self._status, self._diagnostics = status, diagnostics
        return self._status

    def _read_diagnostics(self) -> DmmDiagnostics:
        error, calibration = self._query("G7"), self._query("G4")
        srq, framing = self._query("G1"), self._query("G6")
        diagnostics = self.diagnostics_decoder.decode(srq, calibration, framing, error)
        # G2 itself causes error 51 outside calibration, and error 54 during verification.
        if diagnostics.prompt_available:
            try:
                prompt = self.diagnostics_decoder.decode_prompt(self._query("G2"))
                diagnostics = replace(diagnostics, calibration_prompt=prompt)
            except InstrumentError as exc:
                diagnostics = replace(diagnostics, error_code=exc.code, prompt_error_code=exc.code)
        return diagnostics

    def _read_exact(
        self, length: int, deadline: float, reader: Callable[[int], bytes] | None = None
    ) -> bytes:
        reader = reader or self.transport.read
        result = bytearray()
        while len(result) < length:
            if time.monotonic() >= deadline:
                raise QueryTimeout(f"DMM response timed out after {self.query_timeout:g} seconds.")
            data = reader(length - len(result))
            if data:
                result.extend(data)
            else:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.001, remaining))
        if len(result) != length:
            raise ProtocolError("The transport returned more bytes than requested.")
        return bytes(result)

    def _read_frame(
        self, deadline: float, reader: Callable[[int], bytes] | None = None
    ) -> bytes:
        result = bytearray()
        while len(result) <= 4096:
            byte = self._read_exact(1, deadline, reader)
            if byte == EOI_MARKER or (byte == b"\x00" and byte == self.transport.eot_byte):
                if not result:
                    continue
                self._drain_terminators()
                return bytes(result)
            result.extend(byte)
            if byte == b"\n":
                self._drain_terminators()
                if result.strip(b"\r\n"):
                    return bytes(result)
                result.clear()
        raise ProtocolError("DMM response exceeded the message size limit.")

    def _drain_terminators(self) -> None:
        # Preserve the working correction: discard extra trailer values after
        # a complete response without rejecting non-CR/LF/EOT bytes.
        for _ in range(32):
            if not self.transport.read_available(1):
                return
        raise ProtocolError("Too many response trailer bytes.")

    def _read_controller_line(self) -> bytes:
        payload, deadline = bytearray(), time.monotonic() + self.query_timeout
        while time.monotonic() < deadline:
            fragment = self.transport.read_text()
            if not fragment:
                time.sleep(0.005)
                continue
            payload.extend(fragment.encode("utf-8"))
            if len(payload) > 4096:
                raise ProtocolError("Controller response exceeded the message size limit.")
            if b"\n" in payload:
                line = bytes(payload).split(b"\n", 1)[0].strip(b"\r\x04")
                if line:
                    self._drain_terminators()
                    return line
                payload.clear()
        raise QueryTimeout(
            f"Controller response timed out after {self.query_timeout:g} seconds. Received: {bytes(payload[:128])!r}"
        )

    @staticmethod
    def _describe_response(payload: bytes) -> str:
        try:
            text = payload.decode("ascii").strip()
            ascii_response(text)
            return text if "\x00" not in text else f"Raw ASCII bytes: {payload.hex(' ')}"
        except UnicodeDecodeError:
            return f"Binary response: {payload.hex(' ')}"
