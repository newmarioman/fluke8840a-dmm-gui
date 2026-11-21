"""Real Tcl variables with fake views verify synchronization and thread ownership."""

import threading
import tkinter as tk
import unittest
from types import SimpleNamespace

from fluke8840a.app import DmmApplication
from fluke8840a.config import FUNCTIONS, TRIGGERS
from fluke8840a.instrument import Fluke8840A
from fluke8840a.layout import LayoutPolicy
from fluke8840a.ports import PortSelection, SerialPort, SerialPortDiscovery
from fluke8840a.status import DiagnosticsDecoder, StatusDecoder
from fluke8840a.tasks import BackgroundExecutor
from fluke8840a.transport import SimulatedTransport
from fluke8840a.ui import InstrumentSettingsPanel


class Widget:
    def __init__(self, **options):
        self.options = options

    def configure(self, **options):
        self.options.update(options)

    def cget(self, name):
        return self.options[name]


class Root:
    def __init__(self):
        self.owner, self.callbacks, self.cancelled = threading.get_ident(), [], []

    def after(self, delay, callback):
        if threading.get_ident() != self.owner:
            raise AssertionError("A worker called root.after")
        self.callbacks.append(callback)
        return len(self.callbacks)

    def after_cancel(self, identifier):
        if threading.get_ident() != self.owner:
            raise AssertionError("A worker called root.after_cancel")
        self.cancelled.append(identifier)


class SettingsSyncTests(unittest.TestCase):
    def setUp(self):
        self.tcl, self.calls = tk.Tcl(), []
        self.panel = InstrumentSettingsPanel.__new__(InstrumentSettingsPanel)
        self.panel._syncing, self.panel._status = False, None
        self.panel._actions = {"option": lambda key, value: self.calls.append((key, value))}
        keys = ("autorange", "range", "rate", "offset", "trigger", "display")
        self.panel.variables = {key: tk.StringVar(self.tcl) for key in keys}
        self.panel.widgets = {key: Widget(state="readonly") for key in keys}
        self.panel.function_buttons = {int(spec.code): Widget() for spec in FUNCTIONS}
        names = ("Inputs", "Offset", "Rate", "Resolution", "Autorange", "Trigger")
        self.panel.status_labels = {name: Widget() for name in names}
        self.panel.diagnostic_labels = {
            name: Widget()
            for name in (
                "Error (G7)",
                "Calibration (G4)",
                "Verification (G4)",
                "Calibration input (G2)",
                "SRQ mask (G1)",
                "Output suffix (G6)",
                "Terminators (G6)",
            )
        }
        for name in (
            "status_button",
            "reset_button",
            "clear_errors_button",
            "diagnostics_button",
        ):
            setattr(self.panel, name, Widget())

    def test_ascii_status_updates_controls_and_only_actual_function_is_green(self):
        status = StatusDecoder().decode("5524", "1111", display_mode="OFF")
        self.panel.apply_status(status)
        self.assertEqual(self.panel.variables["range"].get(), "2000 mA")
        self.assertEqual(self.panel.variables["rate"].get(), "Fast")
        self.assertEqual(self.panel.variables["trigger"].get(), "Bus without Delay")
        self.assertEqual(self.panel.variables["display"].get(), "OFF")
        self.assertEqual(self.panel.variables["autorange"].get(), "OFF")
        self.assertEqual(self.panel.variables["offset"].get(), "ON")
        green = [
            code
            for code, button in self.panel.function_buttons.items()
            if button.cget("background") == "lawngreen"
        ]
        self.assertEqual(green, [5])
        self.assertEqual(self.calls, [])

    def test_diagnostic_labels_describe_current_registers_and_clear_on_disconnect(self):
        self.panel.apply_diagnostics(DiagnosticsDecoder().decode("33", "1012", "1015", "1031"))
        expected = {
            "Error (G7)": "Error (G7): 31 — Current function requires the front inputs",
            "Calibration (G4)": "Calibration (G4): Offset/gain calibration",
            "Verification (G4)": "Verification (G4): ON",
            "Calibration input (G2)": "Calibration input (G2): Not available during calibration verification",
            "SRQ mask (G1)": "SRQ mask (G1): 33 — Overrange, Any error",
            "Output suffix (G6)": "Output suffix (G6): ON",
            "Terminators (G6)": "Terminators (G6): LF",
        }
        self.assertEqual(
            {key: label.cget("text") for key, label in self.panel.diagnostic_labels.items()},
            expected,
        )
        self.assertEqual(self.calls, [])
        self.panel.apply_diagnostics(None)
        self.assertTrue(
            all(label.cget("text").endswith("—") for label in self.panel.diagnostic_labels.values())
        )

    def test_measurement_information_is_in_settings_and_trigger_follows_autorange(self):
        self.panel.apply_status(StatusDecoder().decode("3411", "1001"))
        self.assertEqual(list(self.panel.status_labels)[-2:], ["Autorange", "Trigger"])
        self.assertEqual(
            {name: label.cget("text") for name, label in self.panel.status_labels.items()},
            {
                "Inputs": "Inputs: Front",
                "Offset": "Offset: ON",
                "Rate": "Rate: Medium",
                "Resolution": "Resolution: 5½ digits",
                "Autorange": "Autorange: ON",
                "Trigger": "Trigger: External with Delay",
            },
        )
        self.panel.apply_status(None)
        self.assertTrue(
            all(label.cget("text").endswith("—") for label in self.panel.status_labels.values())
        )

    def test_trigger_readback_selects_exactly_one_of_the_five_fluke_modes(self):
        for code, expected in enumerate(TRIGGERS):
            self.panel.apply_status(StatusDecoder().decode(f"110{code}", "1000"))
            self.assertEqual(self.panel.widgets["trigger"].cget("values"), TRIGGERS)
            self.assertEqual(self.panel.variables["trigger"].get(), expected)
            self.assertEqual(
                self.panel.status_labels["Trigger"].cget("text"), f"Trigger: {expected}"
            )
        self.assertEqual(self.calls, [])

    def test_status_updates_suppress_variable_trace_feedback(self):
        for key, variable in self.panel.variables.items():
            variable.trace_add("write", lambda *args, key=key: self.panel._changed(key))
        self.panel.apply_status(StatusDecoder().decode("1100", "1000"))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.panel._syncing)

    def test_only_user_selection_sends_one_immediate_setting(self):
        self.panel.variables["rate"].set("Fast")
        self.assertEqual(self.calls, [])
        self.panel._changed("rate")
        self.assertEqual(self.calls, [("rate", "Fast")])
        self.panel.set_state(False)
        self.panel._changed("rate")
        self.assertEqual(len(self.calls), 1)

    def test_function_change_rebuilds_correct_ranges_and_disconnect_clears_controls(self):
        self.panel.apply_status(StatusDecoder().decode("6500", "1000"))
        self.assertEqual(self.panel.widgets["range"].cget("values"), ("2000 mA",))
        self.panel.apply_status(StatusDecoder().decode("4600", "1000"))
        self.assertEqual(
            self.panel.widgets["range"].cget("values"),
            ("200 Ω", "2 kΩ", "20 kΩ", "200 kΩ", "2 MΩ", "20 MΩ"),
        )
        self.panel.apply_status(None)
        self.assertTrue(all(v.get() == "—" for v in self.panel.variables.values()))
        self.assertTrue(
            all(b.cget("background") != "lawngreen" for b in self.panel.function_buttons.values())
        )

    def test_queued_selection_captures_value_before_worker_runs(self):
        jobs, app = [], DmmApplication.__new__(DmmApplication)
        app.instrument = Fluke8840A(
            SimulatedTransport(sample_delay=0), command_delay=0, status_delay=0
        )
        app.instrument.connect("SIMULATED", 23)
        app._run_task = lambda label, operation: jobs.append(operation)
        self.panel._actions["option"] = app.change_option
        self.panel.variables["rate"].set("Fast")
        self.panel._changed("rate")
        self.panel.variables["rate"].set("Slow")
        try:
            self.assertEqual(jobs[0]().rate, "Fast")
        finally:
            app.instrument.disconnect()

    def test_trigger_and_display_share_a_row_like_rate_and_offset(self):
        self.panel._columns = 0
        self.panel.fields = SimpleNamespace(columnconfigure=lambda *args, **options: None)
        self.panel.cards = [Widget() for _ in range(6)]
        for card in self.panel.cards:
            card.grid = lambda card=card, **options: card.configure(**options)
        for columns in (1, 2, 1):
            self.panel._arrange_fields(columns)
            occupied = set()
            for card in self.panel.cards:
                row, column, span = (card.cget(key) for key in ("row", "column", "columnspan"))
                cells = {(row, cell) for cell in range(column, column + span)}
                self.assertTrue(occupied.isdisjoint(cells))
                self.assertTrue(all(cell < columns for _, cell in cells))
                occupied.update(cells)
            self.assertEqual(self.panel.cards[4].cget("columnspan"), 1)
            if columns == 2:
                self.assertEqual(self.panel.cards[4].cget("row"), self.panel.cards[5].cget("row"))
                self.assertEqual(self.panel.cards[4].cget("column"), 0)
                self.assertEqual(self.panel.cards[5].cget("column"), 1)


class WorkerTests(unittest.TestCase):
    def test_success_and_errors_return_to_owner_thread(self):
        root, values = Root(), []
        executor = BackgroundExecutor(root)
        owner = threading.get_ident()
        try:
            executor.submit(
                lambda: 7,
                lambda value: values.append((value, threading.get_ident())),
                lambda exc: self.fail(str(exc)),
            )

            def fail():
                raise OSError("Injected error")

            executor.submit(
                fail,
                lambda value: self.fail("Unexpected success"),
                lambda exc: values.append((str(exc), threading.get_ident())),
            )
            executor._executor.submit(lambda: None).result(timeout=1)
            self.assertEqual(values, [])
            executor._drain()
            self.assertEqual(values, [(7, owner), ("Injected error", owner)])
        finally:
            executor.shutdown()

    def test_latest_updates_do_not_accumulate_a_stale_console_queue(self):
        root, values = Root(), []
        executor = BackgroundExecutor(root)
        try:
            for index in range(1000):
                executor.post_latest("progress", values.append, index)
            executor._drain()
            self.assertEqual(values, [999])
        finally:
            executor.shutdown()

    def test_close_reasserts_stop_after_a_queued_start_clears_it(self):
        root, submitted = Root(), []
        app = DmmApplication.__new__(DmmApplication)
        stopped = []
        app.root, app.busy, app._port_scan_pending = root, True, False
        app.acquisition = SimpleNamespace(running=False, stop=lambda: stopped.append(True))
        app._wait_for_close()
        self.assertEqual(len(stopped), 1)
        # The queued startup now begins and clears its earlier stop request.
        app.busy, app.acquisition.running = False, True
        root.callbacks.pop(0)()
        self.assertEqual(len(stopped), 2)
        app.acquisition.running = False
        app.instrument = SimpleNamespace(disconnect=lambda: None)
        app.executor = SimpleNamespace(submit=lambda *args: submitted.append(args))
        root.callbacks.pop(0)()
        self.assertEqual(len(submitted), 1)
        self.assertTrue(app.busy)


class PortLayoutTests(unittest.TestCase):
    def test_actual_ports_are_naturally_sorted_deduplicated_and_kept_when_connected(self):
        ports = [
            SimpleNamespace(device=device, description="USB")
            for device in ("COM10", "COM2", "COM2", "")
        ]
        detected = SerialPortDiscovery(lambda: ports).scan()
        self.assertEqual([p.device for p in detected], ["COM2", "COM10"])
        choice = PortSelection()
        self.assertEqual(choice.update(detected), "COM2")
        self.assertEqual(choice.update((SerialPort("COM10"),), connected=True), "COM2")
        self.assertEqual(choice.update((SerialPort("COM10"),)), "COM10")

    def test_compact_layout_and_dpi_use_bounded_controls_and_fonts(self):
        for width, height, dpi in ((640, 480, 1), (1366, 768, 1), (1920, 1080, 2), (3840, 2160, 2)):
            policy = LayoutPolicy(width, height, dpi)
            self.assertEqual(policy.compact, width / dpi < 1000)
            self.assertLessEqual(policy.compact_controls_height, 180 * dpi)
            self.assertGreaterEqual(policy.font_size, 8)
            self.assertLessEqual(policy.font_size, 14)
        self.assertEqual(LayoutPolicy.columns(100, 150, 2), 1)


if __name__ == "__main__":
    unittest.main()
