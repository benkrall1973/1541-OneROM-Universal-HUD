"""Regression tests for Monitor head-state telemetry."""

import unittest
from unittest.mock import patch
from pathlib import Path
from importlib import import_module

touchscreen = import_module("1541_touchscreen_simulator")
TouchSimulator = touchscreen.TouchSimulator
BoardDescriptor = touchscreen.BoardDescriptor
CdcBoardLink = touchscreen.CdcBoardLink
DriveBinding = touchscreen.DriveBinding
DriveTelemetryParser = touchscreen.DriveTelemetryParser
MAX_RX_BUFFER_BYTES = touchscreen.MAX_RX_BUFFER_BYTES
load_binding = touchscreen.load_binding
save_binding = touchscreen.save_binding


def mock_monotonic(value: float):
    """Patch the in-process touchscreen module despite its numeric filename."""
    return patch.object(touchscreen.time, "monotonic", return_value=value)


class ControllerReplyProtocolTests(unittest.TestCase):
    """Firmware reply records must settle the matching HUD transaction."""

    def reply_target(self, card_id: str):
        target = TouchSimulator.__new__(TouchSimulator)
        target._controller_transactions = {card_id: "timer"}
        target.controller_card_feedback = {}
        target._pending_controller_card = card_id
        target._pending_controller_values = {"startup_rom": 2, "boot_iec": 11}
        target._hud_dirty = False
        target.finish_controller_transaction = lambda current: target._controller_transactions.pop(current, None)
        target.update_live_hud_fields = lambda: None

        class Status:
            def set(self, _value: str) -> None:
                pass

        target.control_status = Status()

        class Var:
            def __init__(self, value: str) -> None:
                self.value = value
            def set(self, value: str) -> None:
                self.value = value
            def get(self) -> str:
                return self.value

        target.rom_choice = Var("Slot 2 — JIFFYDOS")
        target.rom_var = Var("Slot 2 — JIFFYDOS")
        target.iec_choice = Var("Device 11")
        target.iec_var = Var("Device 11")
        return target

    def test_iec_reply_record_confirms_boot_iec_transaction(self) -> None:
        target = self.reply_target("boot_iec")
        reply = "$ROMTEST,IEC,OK,ADDRESS=11,VERIFY=11,SAVED_SLOT=1,VALID=1"
        self.assertTrue(target.handle_controller_setting_reply(reply))
        self.assertEqual(target.controller_card_feedback["boot_iec"], "CONFIRMED BY CONTROL ONEROM")
        self.assertFalse(target._controller_transactions)

    def test_set_reply_record_confirms_startup_rom_transaction(self) -> None:
        target = self.reply_target("startup_rom")
        self.assertTrue(target.handle_controller_setting_reply("$ROMTEST,SET,OK,SLOT=2,VERIFY=2"))
        self.assertEqual(target.controller_card_feedback["startup_rom"], "CONFIRMED BY CONTROL ONEROM")
        self.assertFalse(target._controller_transactions)


class _FakeCdcDevice:
    """Minimal serial-device double for receive-buffer tests."""

    is_open = True

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


class DriveTelemetryParserHeadStateTests(unittest.TestCase):
    def test_successive_track_writes_set_requested_direction(self) -> None:
        parser = DriveTelemetryParser()
        with mock_monotonic(10.0):
            parser.process("MOTOR state=1")
            parser.process("TRACK_WRITE addr=$0022 data=$12 (18)")
            parser.process("TRACK_WRITE addr=$0022 data=$14 (20)")
            self.assertEqual(parser.state.head, "IN")
            parser.process("TRACK_WRITE addr=$0022 data=$0F (15)")
        self.assertEqual(parser.state.head, "OUT")

    def test_status_target_changes_set_requested_direction(self) -> None:
        parser = DriveTelemetryParser()
        with mock_monotonic(10.0):
            parser.process("STATE T0.0.16 TV=1 T=18 M=1")
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
            self.assertEqual(parser.state.head, "IN")
            parser.process("STATE T0.0.16 TV=1 T=15 M=1")
        self.assertEqual(parser.state.head, "OUT")

    def test_repeated_status_target_does_not_hide_direction(self) -> None:
        parser = DriveTelemetryParser()
        with mock_monotonic(10.0):
            parser.process("STATE T0.0.16 TV=1 T=18 M=1")
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
        with mock_monotonic(10.1):
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
        self.assertEqual(parser.state.head, "IN")
        with mock_monotonic(10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_skipped_phase_stays_active_in_the_last_confirmed_direction(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser.state.head = "OUT"
        parser._last_direction = "OUT"
        parser._last_motion = 1.0
        with mock_monotonic(10.0):
            parser.process("PHASE old=0 new=2 delta=2 motor=1")
        self.assertEqual(parser.state.head, "OUT")
        with mock_monotonic(10.79):
            self.assertEqual(parser.refresh().head, "OUT")
        with mock_monotonic(10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_head_stalls_after_eight_tenths_of_a_second_without_events(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser.state.head = "IN"
        parser._last_motion = 10.0
        with mock_monotonic(10.79):
            self.assertEqual(parser.refresh().head, "IN")
        with mock_monotonic(10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_first_target_write_resets_the_stall_interval_without_direction(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser._last_motion = 1.0
        with mock_monotonic(20.0):
            parser.process("TRACK_WRITE addr=$0022 data=$12 (18)")
        self.assertEqual(parser._last_motion, 20.0)
        with mock_monotonic(20.79):
            self.assertNotEqual(parser.refresh().head, "PARK")

    def test_motor_off_parks_the_head(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser.state.head = "OUT"
        parser.process("MOTOR state=0")
        self.assertEqual(parser.state.head, "PARK")


class CdcBoardLinkTests(unittest.TestCase):
    def test_unterminated_receive_data_cannot_grow_the_buffer_forever(self) -> None:
        link = CdcBoardLink(BoardDescriptor("TEST", "COM1", "Test device"))
        link.device = _FakeCdcDevice(b"xx")
        link._buffer = "x" * (MAX_RX_BUFFER_BYTES - 1)

        self.assertEqual(link.read_lines(), [])
        self.assertEqual(link._buffer, "")

    def test_complete_burst_records_survive_buffer_guard(self) -> None:
        link = CdcBoardLink(BoardDescriptor("TEST", "COM1", "Test device"))
        payload = (b"SYNC T0.0.16 COUNT=1 LEVEL=0\n" * 1200)
        link.device = _FakeCdcDevice(payload)
        lines = link.read_lines()
        self.assertEqual(len(lines), 1200)
        self.assertTrue(all(line.startswith("SYNC ") for line in lines))


class TelemetrySubscriptionTests(unittest.TestCase):
    def test_background_subscription_keeps_all_evidence_classes_enabled(self) -> None:
        target = TouchSimulator.__new__(TouchSimulator)
        self.assertEqual(target.visible_hud_subscription_mask(), touchscreen.HUD_TELEMETRY_ALL_MASK)


class DriveBindingPersistenceTests(unittest.TestCase):
    def test_serial_bindings_and_appearance_share_one_json_file(self) -> None:
        path = Path("onerom_drive_bindings.json")
        binding = DriveBinding(
            controller_serial="CONTROL-123",
            hud_serial="MONITOR-456",
            appearance={"background": "#112233", "accent": "#AABBCC"},
            priorities={"track": 1, "capture_health": None, "sync_rate": 12},
            control_features={"rom_select": True, "iec_address": False, "write_protect_override": False},
            window_size={"width": 1280, "height": 720},
        )
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "onerom_drive_bindings.json"
            save_binding(path, binding)
            self.assertTrue(path.exists())
            loaded = load_binding(path)

        self.assertEqual(loaded.controller_serial, "CONTROL-123")
        self.assertEqual(loaded.hud_serial, "MONITOR-456")
        self.assertEqual(loaded.appearance, {"background": "#112233", "accent": "#AABBCC"})
        self.assertEqual(loaded.priorities, {"track": 1, "capture_health": None, "sync_rate": 12})
        self.assertEqual(loaded.control_features, {"rom_select": True, "iec_address": False, "write_protect_override": False})
        self.assertEqual(loaded.window_size, {"width": 1280, "height": 720})

    def test_known_onerom_vid_pid_are_the_only_discoverable_boards(self) -> None:
        class Port:
            def __init__(self, serial_number, device, vid, pid):
                self.serial_number = serial_number
                self.device = device
                self.description = "Test"
                self.vid = vid
                self.pid = pid
        class Ports:
            def __init__(self, values):
                self.values = values
            def comports(self):
                return self.values
        good = Port("GOOD", "COM1", touchscreen.ONEROM_USB_VID, touchscreen.ONEROM_USB_PID)
        bad = Port("BAD", "COM2", 0x1234, 0x5678)
        with patch.object(touchscreen, "list_ports", Ports([bad, good])):
            boards = touchscreen.discover_cdc_boards()
        self.assertEqual([board.serial_number for board in boards], ["GOOD"])


class ControllerTransactionTests(unittest.TestCase):
    def test_write_protect_does_not_transmit_while_another_request_is_pending(self) -> None:
        target = TouchSimulator.__new__(TouchSimulator)
        target._controller_transactions = {"settings_refresh": "timer"}
        target._confirmed_writable = False
        sent: list[str] = []
        class Var:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value
        class Status:
            def __init__(self): self.value = ""
            def set(self, value): self.value = value
        target.writable = Var(True)
        target.control_status = Status()
        target.send_controller_command = sent.append
        target.update_protection()
        self.assertEqual(sent, [])
        self.assertFalse(target.writable.value)

    def test_wp_query_is_tracked_as_a_transaction(self) -> None:
        target = TouchSimulator.__new__(TouchSimulator)
        target._controller_transactions = {"write_protect_query": "timer"}
        target._pending_wp_override = None
        target.controller_wp_available = None
        target._confirmed_writable = False
        class Var:
            def __init__(self): self.value = False
            def set(self, value): self.value = value
        class Status:
            def set(self, _value): pass
        target.writable = Var()
        target.control_status = Status()
        target.finish_controller_transaction = lambda current: target._controller_transactions.pop(current, None)
        target.update_live_hud_fields = lambda: None
        target.handle_controller_wp_reply("$ROMTEST,WP,STATE=OFF,AVAILABLE=1")
        self.assertFalse(target._controller_transactions)
        self.assertFalse(target.writable.value)
        self.assertTrue(target.controller_wp_available)


    def test_unknown_config_fields_do_not_discard_known_settings(self) -> None:
        path = Path("onerom_drive_bindings.json")
        payload = (
            '{"version": 99, "controller_serial": "CONTROL-123", '
            '"hud_serial": "MONITOR-456", "future_setting": true}'
        )
        with patch.object(Path, "read_text", return_value=payload):
            loaded = load_binding(path)

        self.assertEqual(loaded.version, 99)
        self.assertEqual(loaded.controller_serial, "CONTROL-123")
        self.assertEqual(loaded.hud_serial, "MONITOR-456")


if __name__ == "__main__":
    unittest.main()
