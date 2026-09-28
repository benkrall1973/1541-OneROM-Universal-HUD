"""Regression tests for Monitor head-state telemetry."""

import unittest
from unittest.mock import patch
from pathlib import Path

from onerom_usb import (
    BoardDescriptor, CdcBoardLink, DriveBinding, DriveTelemetryParser,
    MAX_RX_BUFFER_BYTES, load_binding, save_binding,
)


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
        with patch("onerom_usb.time.monotonic", return_value=10.0):
            parser.process("MOTOR state=1")
            parser.process("TRACK_WRITE addr=$0022 data=$12 (18)")
            parser.process("TRACK_WRITE addr=$0022 data=$14 (20)")
            self.assertEqual(parser.state.head, "IN")
            parser.process("TRACK_WRITE addr=$0022 data=$0F (15)")
        self.assertEqual(parser.state.head, "OUT")

    def test_status_target_changes_set_requested_direction(self) -> None:
        parser = DriveTelemetryParser()
        with patch("onerom_usb.time.monotonic", return_value=10.0):
            parser.process("STATE T0.0.16 TV=1 T=18 M=1")
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
            self.assertEqual(parser.state.head, "IN")
            parser.process("STATE T0.0.16 TV=1 T=15 M=1")
        self.assertEqual(parser.state.head, "OUT")

    def test_repeated_status_target_does_not_hide_direction(self) -> None:
        parser = DriveTelemetryParser()
        with patch("onerom_usb.time.monotonic", return_value=10.0):
            parser.process("STATE T0.0.16 TV=1 T=18 M=1")
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
        with patch("onerom_usb.time.monotonic", return_value=10.1):
            parser.process("STATE T0.0.16 TV=1 T=20 M=1")
        self.assertEqual(parser.state.head, "IN")
        with patch("onerom_usb.time.monotonic", return_value=10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_skipped_phase_stays_active_in_the_last_confirmed_direction(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser.state.head = "OUT"
        parser._last_direction = "OUT"
        parser._last_motion = 1.0
        with patch("onerom_usb.time.monotonic", return_value=10.0):
            parser.process("PHASE old=0 new=2 delta=2 motor=1")
        self.assertEqual(parser.state.head, "OUT")
        with patch("onerom_usb.time.monotonic", return_value=10.79):
            self.assertEqual(parser.refresh().head, "OUT")
        with patch("onerom_usb.time.monotonic", return_value=10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_head_stalls_after_eight_tenths_of_a_second_without_events(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser.state.head = "IN"
        parser._last_motion = 10.0
        with patch("onerom_usb.time.monotonic", return_value=10.79):
            self.assertEqual(parser.refresh().head, "IN")
        with patch("onerom_usb.time.monotonic", return_value=10.8):
            self.assertEqual(parser.refresh().head, "STALL")

    def test_first_target_write_resets_the_stall_interval_without_direction(self) -> None:
        parser = DriveTelemetryParser()
        parser.state.motor = True
        parser._last_motion = 1.0
        with patch("onerom_usb.time.monotonic", return_value=20.0):
            parser.process("TRACK_WRITE addr=$0022 data=$12 (18)")
        self.assertEqual(parser._last_motion, 20.0)
        with patch("onerom_usb.time.monotonic", return_value=20.79):
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


class DriveBindingPersistenceTests(unittest.TestCase):
    def test_serial_bindings_and_appearance_share_one_json_file(self) -> None:
        path = Path("onerom_drive_bindings.json")
        binding = DriveBinding(
            controller_serial="CONTROL-123",
            hud_serial="MONITOR-456",
            appearance={"background": "#112233", "accent": "#AABBCC"},
            priorities={"track": 1, "capture_health": None, "sync_rate": 12},
        )
        # Exercise the actual JSON payload without creating an artifact in a
        # test environment that intentionally blocks Python file writes.
        with patch.object(Path, "write_text") as write_text:
            save_binding(path, DriveBinding(
                controller_serial=binding.controller_serial,
                hud_serial=binding.hud_serial,
                appearance=binding.appearance,
                priorities=binding.priorities,
            ))
        payload = write_text.call_args.args[0]
        with patch.object(Path, "read_text", return_value=payload):
            loaded = load_binding(path)

        self.assertEqual(loaded.controller_serial, "CONTROL-123")
        self.assertEqual(loaded.hud_serial, "MONITOR-456")
        self.assertEqual(loaded.appearance, {"background": "#112233", "accent": "#AABBCC"})
        self.assertEqual(loaded.priorities, {"track": 1, "capture_health": None, "sync_rate": 12})

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
