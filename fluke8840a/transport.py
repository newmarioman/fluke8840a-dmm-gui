# -*- coding: utf-8 -*-

"""
Hardware Transport Layer and PyAR488 GPIB-to-Serial Communication.

This module defines the abstract base transport architecture and implements 
the real-time serial interface wrapper for AR488 controllers. It handles bus writes, raw binary data stream fragments, 
and low-level EOS/EOI framing options.

Part of the FLUKE 8840A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager

# Local Packages  
from .config import FUNCTION_BY_CODE


class TransportError(RuntimeError):
    pass


class InstrumentTransport(ABC):
    eot_byte: bytes = b""

    @abstractmethod
    def open(self, port: str, address: int) -> None:
        pass

    @abstractmethod
    def write(self, command: str) -> None:
        pass

    @abstractmethod
    def read(self, size: int = 1) -> bytes:
        pass

    def read_text(self) -> str:
        return self.read(4096).decode("utf-8", errors="replace")

    def read_available(self, size: int = 1) -> bytes:
        return self.read(size)

    @contextmanager
    def measurement_reader(self):
        """Yield a reader that cooperates with the instrument's response deadline."""
        yield self.read

    def discard_input(self) -> None:
        """Discard queued bytes from an incomplete response before retrying."""
        while self.read_available(4096):
            pass

    @abstractmethod
    def close(self) -> None:
        pass


class AR488Transport(InstrumentTransport):
    """
    Controller text uses bus_read(); DMM responses use bus_read(decode=False).
    A byte buffer preserves complete ASCII replies across fragments. Direct
    serial access is limited to already available response trailers.
    """

    def __init__(self, timeout: float = 1.0, *, adapter_factory: Callable | None = None) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Serial timeout must be a finite positive number.")
        self.timeout = timeout
        self._adapter_factory = adapter_factory
        self._interface = None
        self._buffer = bytearray()

    @property
    def is_open(self) -> bool:
        return self._interface is not None

    def open(self, port: str, address: int) -> None:
        self.close()
        if not port.strip() or type(address) is not int or not 1 <= address <= 30:
            raise ValueError("Select a serial port and a GPIB address from 1 to 30.")
        factory = self._adapter_factory
        if factory is None:
            try:
                try:
                    from PyAR488 import AR488
                except ImportError:
                    from PyAR488.PyAR488 import AR488
            except ImportError as exc:
                raise TransportError(
                    "PyAR488 is not installed. Run: python -m pip install -r requirements.txt"
                ) from exc
            factory = AR488
        try:
            self._interface = factory(port, timeout=self.timeout, debug=False)
            self._interface.address(address)
            # The constructor already read this character. Do not add queries
            # or modify serial timeouts before the first ++ver exchange.
            character = getattr(self._interface, "_eot_char", "")
            self.eot_byte = character.encode("latin-1") if character else b""
            if len(self.eot_byte) > 1:
                raise ValueError("The controller EOT character must be exactly one byte.")
        except Exception as exc:
            self.close()
            raise TransportError(f"Could not open AR488 on {port}, GPIB {address}: {exc}") from exc

    def write(self, command: str) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if not isinstance(command, str):
            raise ValueError("Instrument commands must be ASCII strings.")
        # The driver receives a string and owns its ASCII conversion and LF/CR ending.
        command = command.rstrip("\r\n")
        if not command or any(char in command for char in "\r\n"):
            raise ValueError("Send one ASCII command without embedded line breaks.")
        if not command.isascii():
            raise ValueError("Instrument commands must contain ASCII characters.")
        try:
            self._interface.bus_write(command + "\n")
        except Exception as exc:
            raise TransportError(f"AR488 write error: {exc}") from exc

    def read_text(self) -> str:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            if self._buffer:
                response = bytes(self._buffer)
                self._buffer.clear()
            else:
                response = self._interface.bus_read()
            return response.decode("utf-8") if isinstance(response, bytes) else str(response or "")
        except Exception as exc:
            raise TransportError(f"AR488 read error: {exc}") from exc

    def read(self, size: int = 1) -> bytes:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            if not self._buffer:
                response = self._interface.bus_read(decode=False)
                if isinstance(response, str):
                    response = response.encode("latin-1")
                self._buffer.extend(response or b"")
            result = bytes(self._buffer[:size])
            del self._buffer[: len(result)]
            return result
        except Exception as exc:
            raise TransportError(f"AR488 read error: {exc}") from exc

    def read_available(self, size: int = 1) -> bytes:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if self._buffer:
            result = bytes(self._buffer[:size])
            del self._buffer[: len(result)]
            return result
        serial = getattr(self._interface, "_ser", None)
        if serial is None:
            return b""
        # A short, bounded availability check handles delayed USB trailers.
        # It leaves both serial timeout properties exactly as PyAR488 opened them.
        deadline = time.monotonic() + 0.05
        try:
            while time.monotonic() < deadline:
                waiting = serial.in_waiting
                if waiting:
                    return bytes(serial.read(min(size, waiting)))
            return b""
        except Exception as exc:
            raise TransportError(f"AR488 trailer read error: {exc}") from exc

    @contextmanager
    def measurement_reader(self):
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            yield self.read
        finally:
            pass

    def discard_input(self) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        self._buffer.clear()
        try:
            self._interface._ser.reset_input_buffer()
        except Exception as exc:
            raise TransportError(f"AR488 input reset error: {exc}") from exc

    def close(self) -> None:
        interface, self._interface = self._interface, None
        self._buffer.clear()
        self.eot_byte = b""
        if interface is not None:
            try:
                close = getattr(interface, "close", None)
                if callable(close):
                    close()
                else:
                    interface._ser.close()
            except Exception:
                # Keep cleanup idempotent and preserve the original I/O error.
                pass


# Compatibility name for the same hardware transport.
PyAR488Transport = AR488Transport


class SimulatedTransport(InstrumentTransport):
    """Exercise Fluke ASCII commands, status and acquisition without a serial port."""

    def __init__(self, *, sample_delay: float = 0.01, eot: bytes = b"") -> None:
        if not math.isfinite(sample_delay) or sample_delay < 0 or len(eot) > 1:
            raise ValueError("Invalid simulator timing or EOT marker.")
        self.sample_delay, self.eot_byte = sample_delay, eot
        self.is_open, self.address, self.sample_count = False, 1, 0
        self.commands: list[str] = []
        self._buffer = bytearray()
        self._pending_reply: bytes | None = None
        self.front_input = True
        self._reset()

    @property
    def _pending_status(self) -> bool:
        return self._pending_reply is not None

    def _reset(self) -> None:
        self.function_code, self.range_code, self.rate_code, self.trigger_code = 1, 1, 0, 0
        self.autorange, self.offset, self.suffix_enabled = True, False, False
        self.display_mode, self.srq_mask, self.error_code = "ON", 0, 0
        self.calibration_mode, self.calibration_verification, self.terminator_code = 0, False, 0
        self._pending_reply = None

    def open(self, port: str, address: int) -> None:
        if (
            not isinstance(port, str)
            or not port.strip()
            or type(address) is not int
            or not 1 <= address <= 30
        ):
            raise ValueError("Select a serial port and a GPIB address from 1 to 30.")
        if self.is_open:
            raise TransportError("The simulated controller is already open.")
        self.is_open, self.address = True, address
        self._buffer.clear()
        self._reset()

    def write(self, command: str) -> None:
        if not self.is_open:
            raise TransportError("The simulated controller is closed.")
        if (
            not isinstance(command, str)
            or not command.isascii()
            or any(char in command.rstrip("\r\n") for char in "\r\n")
        ):
            raise ValueError("Send one ASCII command without embedded line breaks.")
        command = command.rstrip("\r\n")
        self.commands.append(command)
        if command == "++ver":
            self._buffer.extend(b"AR488 software simulator\r\n")
        elif command == "++addr":
            self._buffer.extend(f"{self.address}\r\n".encode("ascii"))
        elif command == "++clr":
            self._reset()
        elif command.startswith("++read"):
            if self._pending_reply is not None:
                self._buffer.extend(self._pending_reply + b"\r\n" + self.eot_byte)
                self._pending_reply = None
            else:
                self._measurement()
        elif command.startswith("++"):
            self._buffer.extend(b"0\r\n")
        else:
            self._instrument_command(command.upper().replace(" ", ""))

    def _instrument_command(self, command: str) -> None:
        if command == "*":
            self._reset()
        elif re.fullmatch(r"G[0-8]", command):
            if command == "G0" and self.autorange:
                self._auto_range()
            replies = {
                "G0": f"{self.function_code}{self.range_code}{self.rate_code}{self.trigger_code}",
                "G1": f"{self.srq_mask:02d}",
                "G2": (
                    "+1.0051E+21"
                    if not self.calibration_mode
                    else "+1.0054E+21"
                    if self.calibration_verification
                    else f"+1.00{self.error_code:02d}E+21"
                    if self.error_code
                    else "+1.90000E+0"
                ),
                "G3": " " * 16,
                "G4": f"10{int(self.calibration_verification)}{self.calibration_mode}",
                "G5": f"1{int(not self.front_input)}{int(not self.autorange)}{int(self.offset)}",
                "G6": f"10{int(self.suffix_enabled)}{self.terminator_code}",
                "G7": f"10{self.error_code:02d}",
                "G8": "FLUKE,8840A,0,V4.0",
            }
            self._pending_reply = replies[command].encode("ascii")
        elif re.fullmatch(r"F[1-6]", command):
            self.function_code = int(command[1])
            self._auto_range()
        elif command == "R0":
            self.autorange = True
            self._auto_range()
        elif command == "R7":
            self.autorange = False
        elif re.fullmatch(r"R[1-6]", command):
            ranges = FUNCTION_BY_CODE[self.function_code].ranges
            if any(item.status_code == int(command[1]) for item in ranges):
                self.range_code, self.autorange = int(command[1]), False
            else:
                self.error_code = 40
        elif re.fullmatch(r"S[0-2]", command):
            self.rate_code = int(command[1])
        elif re.fullmatch(r"T[0-4]", command):
            self.trigger_code = int(command[1])
        elif command in ("B0", "B1"):
            self.offset = command == "B1"
        elif command in ("D0", "D1"):
            self.display_mode = "ON" if command == "D0" else "OFF"
        elif command in ("Y0", "Y1"):
            self.suffix_enabled = command == "Y1"
        elif command == "X0":
            self.error_code = 0
        elif command == "W0":
            self.terminator_code = 0
        elif command == "?":
            pass  # The read operation produces the newly triggered measurement.
        elif re.fullmatch(r"N[1-6][0-7][0-2][0-4]P0", command):
            function, range_code, rate, trigger = map(int, command[1:5])
            self._instrument_command(f"F{function}")
            self._instrument_command(f"R{range_code}")
            self.rate_code, self.trigger_code = rate, trigger
        else:
            # Accept concatenated normal commands, as supported by the actual DMM.
            tokens = re.findall(r"[FRSTBDYXGW][0-9]|[?*]", command)
            if len(tokens) > 1 and "".join(tokens) == command:
                for token in tokens:
                    self._instrument_command(token)
            else:
                self.error_code = 32

    def _auto_range(self) -> None:
        unit = FUNCTION_BY_CODE[self.function_code].unit
        self.range_code = {"V": 2, "A": 5, "Ω": 2}[unit]

    def _measurement(self) -> None:
        if self.sample_delay:
            time.sleep(self.sample_delay)
        self.sample_count += 1
        unit = FUNCTION_BY_CODE[self.function_code].unit
        base = {"V": 1.2, "A": 0.012, "Ω": 1000.0}[unit]
        value = base * (1 + 0.15 * math.sin(self.sample_count / 12))
        reading = f"{value:+.9E}"
        if self.suffix_enabled:
            suffix = {1: "VDC", 2: "VAC", 3: "OHM", 4: "OHM", 5: "ADC", 6: "AAC"}
            reading += f",{suffix[self.function_code]}"
        self._buffer.extend(reading.encode("ascii") + b"\r\n" + self.eot_byte)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise TransportError("The simulated controller is closed.")
        result = bytes(self._buffer[:size])
        del self._buffer[: len(result)]
        return result

    def read_available(self, size: int = 1) -> bytes:
        return self.read(size)

    def close(self) -> None:
        self.is_open = False
        self._buffer.clear()
        self._pending_reply = None
