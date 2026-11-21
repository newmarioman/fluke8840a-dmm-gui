"""Verify Fluke ASCII registers, original commands and complete transactions."""

import ast
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fluke8840a.config import FUNCTIONS, OPTION_COMMANDS, RATES, TRIGGERS
from fluke8840a.instrument import Fluke8840A, QueryTimeout
from fluke8840a.status import (
    DiagnosticsDecoder,
    InstrumentError,
    MeasurementDecoder,
    ProtocolError,
    StatusDecoder,
)
from fluke8840a.transport import SimulatedTransport

STATUS_COMMANDS = [
    "G0",
    "++read",
    "G5",
    "++read",
    "G7",
    "++read",
    "G4",
    "++read",
    "G1",
    "++read",
    "G6",
    "++read",
]


class StatusTests(unittest.TestCase):
    def test_all_valid_functions_ranges_rates_triggers_and_iab_flags(self):
        decoder = StatusDecoder()
        for function in FUNCTIONS:
            for selected_range in function.ranges:
                for rate in range(3):
                    for trigger in range(5):
                        for flags in range(8):
                            front, manual, offset = (
                                bool(flags & 4),
                                bool(flags & 2),
                                bool(flags & 1),
                            )
                            configuration = (
                                f"{int(function.code)}{selected_range.status_code}{rate}{trigger}"
                            )
                            iab = f"1{int(not front)}{int(manual)}{int(offset)}"
                            status = decoder.decode(configuration, iab, display_mode="OFF")
                            self.assertEqual(status.function, function)
                            self.assertEqual(status.range, selected_range)
                            self.assertEqual(status.rate, RATES[rate])
                            self.assertEqual(status.trigger_label, TRIGGERS[trigger])
                            self.assertEqual(status.front_input, front)
                            self.assertEqual(status.autorange, not manual)
                            self.assertEqual(status.offset, offset)
                            self.assertEqual(status.display_mode, "OFF")

    def test_manual_examples_and_current_range_use_fluke_codes(self):
        status = StatusDecoder().decode(b"1100\r\n", b"1011\r\n")
        self.assertEqual(status.function.label, "Voltage DC")
        self.assertEqual(status.range.label, "200 mV")
        self.assertFalse(status.autorange)
        self.assertTrue(status.offset)
        self.assertTrue(status.front_input)
        current = StatusDecoder().decode("5524", "1100")
        self.assertEqual(current.range.label, "2000 mA")
        self.assertEqual(current.function.unit, "A")
        self.assertEqual(current.trigger_label, "Bus without Delay")
        self.assertFalse(current.front_input)

    def test_malformed_wrong_model_and_impossible_range_status_are_rejected(self):
        for g0, g5 in (
            ("110", "1000"),
            ("11000", "1000"),
            ("1170", "1000"),
            ("1105", "1000"),
            ("7100", "1000"),
            ("5600", "1000"),
            ("1600", "1000"),
            ("1100", "0000"),
            ("1100", "1020"),
            ("1100", b"1\xff00"),
        ):
            with self.subTest(g0=g0, g5=g5), self.assertRaises(ProtocolError):
                StatusDecoder().decode(g0, g5)

    def test_diagnostics_decode_every_documented_field(self):
        decoder = DiagnosticsDecoder()
        for mask in range(64):
            for mode in (0, 1, 2, 4):
                status = decoder.decode(f"{mask:02d}", f"101{mode}", "1015", "1032")
                self.assertEqual(status.srq_mask, mask)
                self.assertEqual(status.calibration_mode, mode)
                self.assertTrue(status.calibration_verification)
                self.assertTrue(status.suffix_enabled)
                self.assertEqual(status.terminator_code, 5)
                self.assertEqual(status.error_code, 32)
        for values in (
            ("64", "1000", "1000", "1000"),
            ("0", "1000", "1000", "1000"),
            ("00", "1003", "1000", "1000"),
            ("00", "1000", "1020", "1000"),
            ("00", "1000", "1008", "1000"),
            ("00", "1000", "1000", "0000"),
        ):
            with self.assertRaises(ProtocolError):
                decoder.decode(*values)

    def test_srq_mask_describes_fluke_events_and_the_unused_bit(self):
        decoder = DiagnosticsDecoder()
        for mask, expected in (
            (0, ()),
            (1, ("Overrange",)),
            (2, ("Reserved bit (not used)",)),
            (4, ("Front panel SRQ",)),
            (8, ("Calibration step complete",)),
            (16, ("Data available",)),
            (32, ("Any error",)),
            (33, ("Overrange", "Any error")),
            (
                63,
                (
                    "Overrange",
                    "Reserved bit (not used)",
                    "Front panel SRQ",
                    "Calibration step complete",
                    "Data available",
                    "Any error",
                ),
            ),
        ):
            with self.subTest(mask=mask):
                status = decoder.decode(f"{mask:02d}", "1000", "1000", "1000")
                self.assertEqual(status.srq_events, expected)
                self.assertFalse(status.prompt_available)
        for code, meaning in (
            (0, "No error"),
            (31, "Current function requires the front inputs"),
            (71, "Syntax error in a device command"),
            (99, "Undocumented error code"),
        ):
            self.assertEqual(
                decoder.decode("00", "1000", "1000", f"10{code:02d}").error_meaning, meaning
            )

    def test_g2_prompt_accepts_numeric_input_and_suffix_but_rejects_bad_frames(self):
        decoder = DiagnosticsDecoder()
        for prompt in ("+1.00000E+0", "+190.000E-3", "+1.90000E+0, VDC", "-1.00000E+0,OHM"):
            self.assertEqual(decoder.decode_prompt(prompt.encode()), prompt)
        for invalid in ("not a prompt", "1000", "+1.0E+99", "+1.0E+0,ABC"):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                decoder.decode_prompt(invalid)
        with self.assertRaises(InstrumentError) as error:
            decoder.decode_prompt(b"+1.0051E+21")
        self.assertEqual(error.exception.code, 51)


class MeasurementTests(unittest.TestCase):
    def test_measurements_preserve_base_units_and_accept_output_suffixes(self):
        decoder = MeasurementDecoder()
        cases = (
            (b"+1.23456E+0", "V", 1, 1.23456),
            (b"-1.23456E-3,VDC", "V", 1, -0.00123456),
            (b"+1.20000E-2,ADC", "A", 5, 0.012),
            (b"+1.20000E-2,AAC", "A", 6, 0.012),
            (b"+12.3456E+6, OHM", "Ω", 3, 12345600),
            (b"-0.00000E+0", "A", 5, -0.0),
        )
        for payload, unit, function, expected in cases:
            with self.subTest(payload=payload):
                measurement = decoder.decode(payload, 1.2, unit, function_code=function)
                self.assertEqual(measurement.value, expected)
                self.assertEqual(measurement.unit, unit)
                self.assertEqual(measurement.timestamp, 1.2)

    def test_overrange_is_reported_without_discarding_the_numeric_sentinel(self):
        decoder = MeasurementDecoder()
        for payload, expected in ((b"-9.99999E+9", -9.99999e9), (b"+9.99999E+9,>VDC", 9.99999e9)):
            sample = decoder.decode(payload, 0, "V", function_code=1)
            self.assertTrue(sample.overload)
            self.assertEqual(sample.value, expected)
            self.assertEqual(sample.format_value(), "OVERLOAD")

    def test_instrument_error_replies_are_not_measurements(self):
        for code in (1, 31, 32, 40, 51, 99):
            with self.assertRaises(InstrumentError) as caught:
                MeasurementDecoder().decode(f"+1.00{code:02d}E+21".encode(), 0, "V")
            self.assertEqual(caught.exception.code, code)

    def test_bad_readings_and_mismatched_function_suffix_are_rejected(self):
        for payload in (
            b"nan",
            b"inf",
            b"+1.2,AAA",
            b"+1.2,AAC",
            b"+1.2,VAC",
            b"+1.2,VDC,other",
            b"+1.2E+21",
            b"\xff",
        ):
            with self.assertRaises(ProtocolError):
                MeasurementDecoder().decode(payload, 0, "V", function_code=1)


class InstrumentTests(unittest.TestCase):
    def setUp(self):
        self.bus = SimulatedTransport(sample_delay=0)
        self.device = Fluke8840A(self.bus, command_delay=0, status_delay=0, query_timeout=0.05)
        self.device.connect("SIMULATED", 23)

    def tearDown(self):
        self.device.disconnect()

    def test_connect_follows_the_original_fluke_handshake(self):
        self.assertEqual(
            self.bus.commands,
            ["++ver", "*", "G8", "++read", *STATUS_COMMANDS],
        )
        self.assertEqual(self.device.connection_info.instrument_name, "FLUKE,8840A,0,V4.0")

    def test_function_buttons_send_fluke_function_and_autorange_then_read_back(self):
        for function in FUNCTIONS:
            self.bus.commands.clear()
            status = self.device.select_function(int(function.code))
            self.assertEqual(self.bus.commands, [f"F{int(function.code)}", "R0", *STATUS_COMMANDS])
            self.assertEqual(status.function, function)
            self.assertTrue(status.autorange)

    def test_every_range_and_option_is_sent_immediately_and_read_back(self):
        for function in FUNCTIONS:
            self.device.select_function(int(function.code))
            for selected in function.ranges:
                self.bus.commands.clear()
                status = self.device.set_option("range", selected.label)
                self.assertEqual(self.bus.commands, [selected.command, *STATUS_COMMANDS])
                self.assertEqual(status.range, selected)
                self.assertFalse(status.autorange)
        for key, choices in OPTION_COMMANDS.items():
            for label, command in choices.items():
                self.bus.commands.clear()
                status = self.device.set_option(key, label)
                self.assertEqual(self.bus.commands, [command, *STATUS_COMMANDS])
                actual = (
                    status.rate
                    if key == "rate"
                    else status.trigger_label
                    if key == "trigger"
                    else status.display_mode
                    if key == "display"
                    else "ON"
                    if status.offset
                    else "OFF"
                )
                self.assertEqual(actual, label)

    def test_autorange_off_uses_r7_and_preserves_the_current_range(self):
        self.device.set_option("range", "20 V")
        self.bus.commands.clear()
        status = self.device.set_option("autorange", "OFF")
        self.assertEqual(self.bus.commands[0], "R7")
        self.assertEqual(status.range.label, "20 V")
        self.assertFalse(status.autorange)
        self.assertTrue(self.device.set_option("autorange", "ON").autorange)

    def test_internal_reads_do_not_send_question_mark_other_modes_do(self):
        for code, mode in enumerate(TRIGGERS):
            self.device.set_option("trigger", mode)
            self.bus.commands.clear()
            sample = self.device.read_measurement(1.5)
            self.assertEqual(self.bus.commands, ["++read"] if code == 0 else ["?", "++read"])
            self.assertEqual(sample.timestamp, 1.5)

    def test_reset_clear_and_disconnect_use_fluke_commands(self):
        self.device.set_option("rate", "Fast")
        self.device.set_option("display", "OFF")
        self.bus.commands.clear()
        status = self.device.reset()
        self.assertEqual(self.bus.commands[0], "*")
        self.assertEqual(status.rate, "Slow")
        self.assertEqual(status.display_mode, "ON")
        self.bus.commands.clear()
        self.device.clear_errors()
        self.assertEqual(self.bus.commands[0], "X0")
        self.bus.commands.clear()
        self.device.disconnect()
        self.assertEqual(self.bus.commands, ['X0', '*', '++loc'])
        self.assertFalse(self.device.connected)

    def test_custom_queries_suffixes_and_display_tracking(self):
        self.assertEqual(self.device.send_custom("G0", True), "1200")
        self.device.send_custom("D1")
        self.assertEqual(self.device.status.display_mode, "OFF")
        self.device.send_custom("Y1")
        self.assertFalse(self.device.read_measurement().overload)
        self.device.select_function(5)
        self.assertIn("ADC", self.device.read_measurement().raw)
        self.device.send_custom("*")
        self.assertEqual(self.device.status.display_mode, "ON")

    def test_custom_read_and_version_do_not_send_duplicate_reads(self):
        for command in ("++read", "++ver"):
            self.bus.commands.clear()
            self.assertIsNotNone(self.device.send_custom(command, True))
            self.assertEqual(self.bus.commands, [command, *STATUS_COMMANDS])

    def test_invalid_input_is_rejected_without_sending_a_command(self):
        for key, value in (
            ("rate", "3½"),
            ("trigger", "Single"),
            ("offset", "other"),
            ("range", "300 V"),
            ("autorange", "invalid"),
            ("unknown", "ON"),
        ):
            before = self.bus.commands.copy()
            with self.assertRaises(ValueError):
                self.device.set_option(key, value)
            self.assertEqual(self.bus.commands, before)
        for command in ("", "F1\nR0", "λ", "++addr 4", "++auto 1", "W7", "F1W5"):
            before = self.bus.commands.copy()
            with self.assertRaises(ValueError):
                self.device.send_custom(command)
            self.assertEqual(self.bus.commands, before)

    def test_binary_and_printable_trailer_values_do_not_break_valid_status(self):
        for eot in range(256):
            self.bus.eot_byte = bytes((eot,))
            self.assertEqual(self.device.refresh_status().configuration, "1200")
            self.assertGreater(self.device.read_measurement().value, 0)
            self.assertEqual(self.bus._buffer, b"")

    def test_unknown_trailers_are_discarded_after_a_complete_response(self):
        original = self.bus.write

        def write(command):
            original(command)
            if command == "++read":
                self.bus._buffer.extend(b"?\xffextra")

        self.bus.write = write
        self.device.refresh_status()
        self.assertGreater(self.device.read_measurement().value, 0)
        self.assertTrue(self.device.connected)

    def test_fragmented_ascii_and_missing_replies_are_handled(self):
        original = self.bus.read
        self.bus.read = lambda size=1: original(min(size, 1))
        self.assertEqual(self.device.refresh_status().configuration, "1200")
        self.assertGreater(self.device.read_measurement().value, 0)
        self.bus.read = lambda size=1: b""
        with self.assertRaises(QueryTimeout):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)
        self.assertFalse(self.bus.is_open)

    def test_instrument_error_does_not_close_a_healthy_transport(self):
        original = self.bus.write

        def write(command):
            if command == "++read" and not self.bus._pending_status:
                self.bus._buffer.extend(b"+1.0032E+21\r\n")
            else:
                original(command)

        self.bus.write = write
        with self.assertRaises(InstrumentError):
            self.device.read_measurement()
        self.assertTrue(self.device.connected)
        self.assertIsNotNone(self.device.refresh_status())

    def test_diagnostics_query_the_correct_registers_and_do_not_clear_errors(self):
        self.bus.error_code, self.bus.srq_mask, self.bus.suffix_enabled = 32, 33, True
        self.bus.commands.clear()
        diagnostics = self.device.read_diagnostics()
        self.assertEqual(
            self.bus.commands,
            STATUS_COMMANDS,
        )
        self.assertEqual(diagnostics.error_code, 32)
        self.assertEqual(diagnostics.srq_mask, 33)
        self.assertTrue(diagnostics.suffix_enabled)
        self.assertEqual(self.bus.error_code, 32)

    def test_every_status_refresh_updates_registers_without_creating_a_g2_error(self):
        self.assertIsNotNone(self.device.diagnostics)
        self.bus.error_code, self.bus.srq_mask = 31, 33
        self.bus.commands.clear()
        self.device.set_option("rate", "Medium")
        self.assertEqual(self.device.diagnostics.error_code, 31)
        self.assertEqual(self.device.diagnostics.srq_events, ("Overrange", "Any error"))
        self.assertIn("normal measurement", self.device.diagnostics.prompt_label)
        self.assertNotIn("G2", self.bus.commands)
        self.assertNotIn("X0", self.bus.commands)
        self.assertEqual(self.bus.error_code, 31)

    def test_g2_is_read_in_each_active_calibration_mode_and_skipped_during_verification(self):
        for mode in (1, 2, 4):
            self.bus.calibration_mode = mode
            self.bus.commands.clear()
            self.device.refresh_status()
            self.assertEqual(self.bus.commands[-2:], ["G2", "++read"])
            self.assertEqual(self.device.diagnostics.calibration_prompt, "+1.90000E+0")
            self.assertTrue(self.device.connected)
            self.bus.calibration_verification = True
            self.bus.commands.clear()
            self.device.refresh_status()
            self.assertNotIn("G2", self.bus.commands)
            self.assertIn("verification", self.device.diagnostics.prompt_label)
            self.assertIsNone(self.device.diagnostics.calibration_prompt)
            self.bus.calibration_verification = False
        self.bus.calibration_mode = 0
        self.device.refresh_status()
        self.assertIsNone(self.device.diagnostics.calibration_prompt)

    def test_g2_instrument_error_is_reported_without_closing_the_connection(self):
        self.bus.calibration_mode, self.bus.error_code = 2, 41
        self.device.refresh_status()
        diagnostics = self.device.diagnostics
        self.assertEqual(diagnostics.prompt_error_code, 41)
        self.assertIn("Error 41", diagnostics.prompt_label)
        self.assertIsNone(diagnostics.calibration_prompt)
        self.assertTrue(self.device.connected)
        self.assertTrue(self.bus.is_open)
        self.bus.error_code = 0
        self.device.refresh_status()
        self.assertIsNone(self.device.diagnostics.prompt_error_code)
        self.assertEqual(self.device.diagnostics.error_code, 0)

    def test_status_waits_before_g0_so_new_settings_are_reported(self):
        clock = [0.0]
        bus = SimulatedTransport(sample_delay=0)
        device = Fluke8840A(bus, command_delay=0, query_timeout=0.05)
        original = bus.write
        pending = [None]
        events = []

        def write(command):
            events.append((command, clock[0]))
            if command == "S2":
                pending[0] = clock[0] + 0.5
                return
            if command == "G0" and pending[0] is not None and clock[0] >= pending[0]:
                bus.rate_code = 2
            original(command)

        bus.write = write
        with patch(
            "fluke8840a.instrument.time.sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ):
            try:
                device.connect("SIMULATED", 1)
                events.clear()
                status = device.set_option("rate", "Fast")
                self.assertEqual(status.rate, "Fast")
                self.assertEqual(device.status_delay, 0.5)
                self.assertEqual(events[0][0], "S2")
                self.assertEqual(events[1][0], "G0")
                self.assertEqual(events[1][1] - events[0][1], 0.5)
            finally:
                device.disconnect()

    def test_concurrent_commands_keep_both_status_queries_in_the_same_transaction(self):
        entered, release = threading.Event(), threading.Event()
        original = self.bus.write

        def write(command):
            original(command)
            if command == "S1":
                entered.set()
                release.wait(1)

        self.bus.write = write
        self.bus.commands.clear()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.device.set_option, "rate", "Medium")
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.device.set_option, "offset", "ON")
            time.sleep(0.01)
            release.set()
            self.assertEqual(first.result().rate, "Medium")
            self.assertTrue(second.result().offset)
        self.assertEqual(
            self.bus.commands,
            ["S1", *STATUS_COMMANDS, "B1", *STATUS_COMMANDS],
        )

if __name__ == "__main__":
    unittest.main()
