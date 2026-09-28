"""Regression tests for DriveHUD head-state telemetry."""

import unittest
from unittest.mock import patch

from onerom_usb import DriveTelemetryParser


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


if __name__ == "__main__":
    unittest.main()
