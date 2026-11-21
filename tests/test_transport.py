"""Exercise the real PyAR488 driver against a serial controller fixture."""

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from controller import AR488, FlukeController

from fluke8840a.instrument import Fluke8840A
from fluke8840a.transport import AR488Transport, TransportError

STATUS_COMMANDS = [
    "G0\n",
    "++read\n",
    "G5\n",
    "++read\n",
    "G7\n",
    "++read\n",
    "G4\n",
    "++read\n",
    "G1\n",
    "++read\n",
    "G6\n",
    "++read\n",
]


class TransportTests(unittest.TestCase):
    def make_device(self):
        return Fluke8840A(AR488Transport(), command_delay=0, status_delay=0, query_timeout=0.05)

    def test_real_driver_reads_extended_status_and_g2_without_mixing_the_next_measurement(self):
        controller, device = FlukeController(), self.make_device()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                controller.srq_mask, controller.error_code = 33, 31
                device.refresh_status()
                self.assertEqual(device.diagnostics.srq_mask, 33)
                self.assertEqual(device.diagnostics.error_code, 31)
                self.assertNotIn(b"G2", controller.commands)
                controller.error_code, controller.calibration_mode = 0, 2
                device.refresh_status()
                self.assertEqual(device.diagnostics.calibration_prompt, "+190.000E-3,VDC")
                self.assertEqual(controller.commands[-2:], [b"G2", b"++read"])
                controller.calibration_mode = 0
                device.refresh_status()
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.input, b"")
                self.assertTrue(device.connected)
            finally:
                device.disconnect()

    def test_real_driver_uses_original_constructor_and_fluke_handshake(self):
        controller, device = FlukeController(), self.make_device()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                status = device.connect("COM5", 23)
                self.assertEqual(
                    controller.port_options, {"port": "COM5", "baudrate": 115200, "timeout": 1}
                )
                self.assertEqual(controller.timeout, 1)
                self.assertIsNone(controller.write_timeout)
                self.assertEqual(controller.status_count, 1)
                self.assertEqual(status.configuration, "1100")
                self.assertEqual(status.iab, "1000")
                self.assertIn(b"++ver\n\n\r", controller.writes)
                self.assertNotIn(b"++mode 1", controller.commands)
                self.assertNotIn(b"++eot_enable 0", controller.commands)
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.input, b"")
            finally:
                device.disconnect()
        self.assertTrue(controller.closed)

    def test_commands_are_strings_with_one_lf_passed_to_actual_bus_write(self):
        controller, device = FlukeController(), self.make_device()
        commands, original = [], AR488.bus_write

        def record(adapter, command):
            commands.append(command)
            return original(adapter, command)

        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
            patch.object(AR488, "bus_write", record),
        ):
            try:
                device.connect("COM5", 23)
                application_commands = commands[commands.index("++ver\n") :]
                self.assertEqual(
                    application_commands,
                    ["++ver\n", "*\n", "G8\n", "++read\n", *STATUS_COMMANDS],
                )
                self.assertTrue(all(isinstance(command, str) for command in commands))
                commands.clear()
                device.select_function(5)
                self.assertEqual(commands, ["F5\n", "R0\n", *STATUS_COMMANDS])
                device.set_option("trigger", "Bus with Delay")
                commands.clear()
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(commands, ["?\n", "++read\n"])
            finally:
                device.disconnect()

    def test_normal_replies_use_bus_read_not_direct_raw_serial_reads(self):
        controller, device = FlukeController(eot_enable=0), self.make_device()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
            patch.object(
                controller, "read", side_effect=AssertionError("Unexpected raw serial read")
            ),
        ):
            try:
                device.connect("COM5", 23)
                self.assertEqual(device.read_measurement().value, -12)
            finally:
                device.disconnect()

    def test_fragmented_readline_responses_are_reassembled(self):
        controller, device = FlukeController(), self.make_device()
        original = controller.readline

        def fragment():
            if (
                controller.identified
                and controller.input
                and not controller.input.startswith(controller.VERSION)
            ):
                response = bytes(controller.input[:2])
                del controller.input[:2]
                return response
            return original()

        controller.readline = fragment
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                self.assertEqual(device.connect("COM5", 23).configuration, "1100")
                self.assertEqual(device.read_measurement().value, -12)
            finally:
                device.disconnect()

    def test_missing_controller_version_reports_the_correct_stage_and_closes(self):
        controller, device = FlukeController(), self.make_device()
        original = controller._handle
        controller._handle = lambda command: None if command == b"++ver" else original(command)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                TransportError, "while reading the controller version.*timed out"
            ):
                device.connect("COM5", 23)
        self.assertTrue(controller.closed)
        self.assertFalse(device.connected)

    def test_another_instrument_is_not_misidentified_as_an_8840a(self):
        controller, device = FlukeController(), self.make_device()
        original = controller._handle

        def wrong_model(command):
            if command == b"++read" and controller.pending_query == b"G8":
                controller.pending_query = None
                controller.input.extend(b"FLUKE,8842A,0,V4.0\r\n")
            else:
                original(command)

        controller._handle = wrong_model
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            with self.assertRaisesRegex(TransportError, "Fluke identification.*8842A"):
                device.connect("COM5", 23)
        self.assertTrue(controller.closed)

    def test_invalid_ascii_commands_never_reach_bus_write(self):
        controller, device = FlukeController(), self.make_device()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                for invalid in (b"F1\n", "", "F1\nR0", "λ"):
                    before = controller.writes.copy()
                    with self.assertRaises(ValueError):
                        device.transport.write(invalid)
                    self.assertEqual(controller.writes, before)
            finally:
                device.disconnect()

    def test_write_failure_releases_the_connection(self):
        controller, device = FlukeController(), self.make_device()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            device.connect("COM5", 23)
            with patch.object(controller, "write", side_effect=OSError("Injected USB failure")):
                with self.assertRaisesRegex(TransportError, "USB failure"):
                    device.set_option("rate", "Fast")
            self.assertFalse(device.connected)
            self.assertTrue(controller.closed)


if __name__ == "__main__":
    unittest.main()
