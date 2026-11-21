"""Serial AR488 fixture with an independent Fluke ASCII command implementation."""

try:
    from PyAR488 import AR488 as AR488
except ImportError:
    from PyAR488.PyAR488 import AR488 as AR488


class FlukeController:
    """Allow the original startup sequence; expose premature mode reconfiguration.

    This fixture reproduces an unresponsive ++ver after early configuration;
    it does not claim to identify the connected board's physical failure mode.
    """

    VERSION = b"AR488 GPIB controller, ver. 0.51.29, 18/03/2024"

    def __init__(self, *, eos=0, eoi=1, eot=0, eot_enable=1, automatic=0):
        self.config = {
            "++addr": 1,
            "++mode": 1,
            "++eoi": eoi,
            "++eos": eos,
            "++eot_enable": eot_enable,
            "++eot_char": eot,
            "++eor": 0,
            "++idn": 0,
            "++auto": automatic,
        }
        self.port_options, self.writes, self.commands = None, [], []
        self.input, self.partial = bytearray(), bytearray()
        self.escaped, self.stalled, self.identified, self.closed = False, False, False, False
        self.pending_query, self.status_count = None, 0
        self._reset_device()
        self.timeout, self.write_timeout = 1, None

    def open(self, **options):
        self.port_options = options
        self.timeout = options["timeout"]
        return self

    def write(self, payload):
        self.writes.append(payload)
        for value in payload:
            if self.escaped:
                self.partial.append(value)
                self.escaped = False
            elif value == 27:
                self.escaped = True
            elif value in (10, 13):
                if self.partial:
                    command = bytes(self.partial)
                    self.partial.clear()
                    self._handle(command)
            else:
                self.partial.append(value)
        return len(payload)

    def _reset_device(self):
        self.function, self.range, self.rate, self.trigger = 1, 1, 0, 0
        self.manual, self.offset, self.display_off, self.suffix = 0, 0, 0, 0
        self.srq_mask, self.error_code, self.calibration_mode, self.verification = 0, 0, 0, 0
        self.pending_query = None

    def _handle(self, command):
        self.commands.append(command)
        if self.stalled:
            return
        if command == b"++mode 1" and not self.identified:
            self.stalled = True
            return
        if command == b"++ver":
            self.identified = True
            self.input.extend(self.VERSION + b"\r\n")
        elif command == b"++read":
            if self.pending_query == b"G8":
                response = b"FLUKE,8840A,0,V4.0"
            elif self.pending_query == b"G0":
                response = f"{self.function}{self.range}{self.rate}{self.trigger}".encode()
                self.status_count += 1
            elif self.pending_query == b"G5":
                response = f"10{self.manual}{self.offset}".encode()
            elif self.pending_query == b"G1":
                response = f"{self.srq_mask:02d}".encode()
            elif self.pending_query == b"G4":
                response = f"10{self.verification}{self.calibration_mode}".encode()
            elif self.pending_query == b"G7":
                response = f"10{self.error_code:02d}".encode()
            elif self.pending_query == b"G2":
                if not self.calibration_mode:
                    self.error_code = 51
                elif self.verification:
                    self.error_code = 54
                response = (
                    f"+1.00{self.error_code:02d}E+21".encode()
                    if self.error_code
                    else b"+190.000E-3,VDC"
                )
            elif self.pending_query == b"G6":
                response = f"10{self.suffix}0".encode()
            else:
                response = b"-1.20000E+1"
                if self.suffix:
                    response += {
                        1: b",VDC",
                        2: b",VAC",
                        3: b",OHM",
                        4: b",OHM",
                        5: b",ADC",
                        6: b",AAC",
                    }[self.function]
            self.pending_query = None
            self.input.extend(response + b"\r\n")
            if self.config["++eot_enable"]:
                self.input.append(self.config["++eot_char"])
        elif command.startswith(b"++"):
            words = command.decode("ascii").split()
            if words[0] in self.config:
                if len(words) == 2:
                    self.config[words[0]] = int(words[1])
                else:
                    self.input.extend(f"{self.config[words[0]]}\r\n".encode("ascii"))
        elif command == b"*":
            self._reset_device()
        elif command in (b"G0", b"G1", b"G2", b"G4", b"G5", b"G6", b"G7", b"G8"):
            self.pending_query = command
        elif command in (b"F1", b"F2", b"F3", b"F4", b"F5", b"F6"):
            self.function = int(command[1:])
            self.range = 5 if self.function in (5, 6) else 1
        elif command == b"R0":
            self.manual = 0
            self.range = 5 if self.function in (5, 6) else 2
        elif command == b"R7":
            self.manual = 1
        elif command in (b"R1", b"R2", b"R3", b"R4", b"R5", b"R6"):
            self.range, self.manual = int(command[1:]), 1
        elif command in (b"S0", b"S1", b"S2"):
            self.rate = int(command[1:])
        elif command in (b"T0", b"T1", b"T2", b"T3", b"T4"):
            self.trigger = int(command[1:])
        elif command in (b"B0", b"B1"):
            self.offset = int(command[1:])
        elif command in (b"D0", b"D1"):
            self.display_off = int(command[1:])
        elif command in (b"Y0", b"Y1"):
            self.suffix = int(command[1:])
        elif command == b"X0":
            self.error_code = 0

    def read(self, size=1):
        result = bytes(self.input[: min(size, 2)])
        del self.input[: len(result)]
        return result

    @property
    def in_waiting(self):
        return len(self.input)

    def readline(self):
        end = self.input.find(b"\n")
        if end == -1:
            result = bytes(self.input)
            self.input.clear()
        else:
            result = bytes(self.input[: end + 1])
            del self.input[: end + 1]
        return result

    def reset_input_buffer(self):
        self.input.clear()

    def close(self):
        self.closed = True
