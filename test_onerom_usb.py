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
HOME_OUTWARD_HALF_STEPS = touchscreen.HOME_OUTWARD_HALF_STEPS
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
        target._hud_dirty = False
        target.finish_controller_transaction = lambda current: target._controller_transactions.pop(current, None)
        target.update_live_hud_fields = lambda: None

        class Status:
            def set(self, _value: str) -> None:
                pass

        target.control_status = Status()
        return target

    def test_iec_reply_record_confirms_boot_iec_transaction(self) -> None:
        target = self.reply_target("boot_iec")
        reply = "$ROMTEST,IEC,OK,ADDRESS=11,VERIFY=11,SAVED_SLOT=1,VALID=1"
        self.assertTrue(target.handle_controller_setting_reply(reply))
        self.assertEqual(target.controller_card_feedback["boot_iec"], "CONFIRMED BY CONTROL ONEROM")
        self.assertFalse(target._controller_transactions)

    def test_set_reply_record_confirms_startup_rom_transaction(self) -> None:
        target = self.reply_target("startup_rom")
        self.assertTrue(target.handle_controller_setting_reply("$ROMTEST,SET,OK,SLOT=2"))
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

    def test_full_outward_home_then_first_inward_step_establishes_track_one(self) -> None:
        parser = DriveTelemetryParser()
        parser.process("MOTOR state=1")
        for _ in range(HOME_OUTWARD_HALF_STEPS):
            parser.process("PHASE old=1 new=0 delta=3 motor=1")
        self.assertIsNone(parser.state.position_half_tracks)
        parser.process("PHASE old=0 new=1 delta=1 motor=1")
        self.assertEqual(parser.state.position_half_tracks, 2)
        self.assertEqual(parser.state.track, "01.0")
        self.assertEqual(parser.state.position_source, "HOME EST.")

    def test_short_outward_seek_cannot_claim_track_one(self) -> None:
        parser = DriveTelemetryParser()
        parser.process("MOTOR state=1")
        for _ in range(HOME_OUTWARD_HALF_STEPS - 1):
            parser.process("PHASE old=1 new=0 delta=3 motor=1")
        parser.process("PHASE old=0 new=1 delta=1 motor=1")
        self.assertIsNone(parser.state.position_half_tracks)
        self.assertEqual(parser.state.position_source, "UNANCHORED")

    def test_target_write_and_valid_header_override_home_estimate(self) -> None:
        parser = DriveTelemetryParser()
        parser.process("MOTOR state=1")
        for _ in range(HOME_OUTWARD_HALF_STEPS):
            parser.process("PHASE old=1 new=0 delta=3 motor=1")
        parser.process("PHASE old=0 new=1 delta=1 motor=1")
        parser.process("TRACK_WRITE addr=$0022 data=$12 (18)")
        self.assertEqual(parser.state.track, "18.0")
        self.assertEqual(parser.state.position_source, "TARGET")
        parser.process("HDRPHY T=17 S=4")
        parser.process("HDRMETA ID1=$AA ID2=$BB CHK=$00 OK=1")
        self.assertEqual(parser.state.track, "17.0")
        self.assertEqual(parser.state.position_source, "HEADER")

    def test_new_home_reanchors_an_existing_track_count(self) -> None:
        parser = DriveTelemetryParser()
        parser.process("STATE T0.0.16 TV=1 T=30 M=1")
        for _ in range(HOME_OUTWARD_HALF_STEPS):
            parser.process("PHASE old=1 new=0 delta=3 motor=1")
        parser.process("PHASE old=0 new=1 delta=1 motor=1")
        self.assertEqual(parser.state.track, "01.0")
        self.assertEqual(parser.state.position_source, "HOME EST.")


class CdcBoardLinkTests(unittest.TestCase):
    def test_unterminated_receive_data_cannot_grow_the_buffer_forever(self) -> None:
        link = CdcBoardLink(BoardDescriptor("TEST", "COM1", "Test device"))
        link.device = _FakeCdcDevice(b"xx")
        link._buffer = "x" * (MAX_RX_BUFFER_BYTES - 1)

        self.assertEqual(link.read_lines(), [])
        self.assertEqual(link._buffer, "")


class SyncQualificationTests(unittest.TestCase):
    def qualifier_target(self) -> TouchSimulator:
        target = TouchSimulator.__new__(TouchSimulator)
        target.motor = True
        target.live_density = 2
        target.qualified_sync_count = None
        target._sync_candidate_counts = []
        target.last_stable_rpm = None
        target.rpm_samples = []
        return target

    def test_partial_spinup_sample_stays_acquiring(self) -> None:
        target = self.qualifier_target()
        self.assertIsNone(target.observe_sync_sample(117))
        self.assertIsNone(target.qualified_sync_count)
        self.assertIsNone(target.last_stable_rpm)

    def test_two_consistent_samples_produce_qualified_rpm(self) -> None:
        target = self.qualifier_target()
        self.assertIsNone(target.observe_sync_sample(190))
        self.assertAlmostEqual(target.observe_sync_sample(190), 300.0)
        self.assertEqual(target.qualified_sync_count, 190)

    def test_reset_discards_existing_sync_measurement(self) -> None:
        target = self.qualifier_target()
        target.observe_sync_sample(190)
        target.observe_sync_sample(190)
        target.reset_sync_qualification()
        self.assertIsNone(target.qualified_sync_count)
        self.assertIsNone(target.effective_rpm())


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
        # Exercise the actual JSON payload without creating an artifact in a
        # test environment that intentionally blocks Python file writes.
        with patch.object(Path, "write_text") as write_text:
            save_binding(path, DriveBinding(
                controller_serial=binding.controller_serial,
                hud_serial=binding.hud_serial,
                appearance=binding.appearance,
                priorities=binding.priorities,
                control_features=binding.control_features,
                window_size=binding.window_size,
            ))
        payload = write_text.call_args.args[0]
        with patch.object(Path, "read_text", return_value=payload):
            loaded = load_binding(path)

        self.assertEqual(loaded.controller_serial, "CONTROL-123")
        self.assertEqual(loaded.hud_serial, "MONITOR-456")
        self.assertEqual(loaded.appearance, {"background": "#112233", "accent": "#AABBCC"})
        self.assertEqual(loaded.priorities, {"track": 1, "capture_health": None, "sync_rate": 12})
        self.assertEqual(loaded.control_features, {"rom_select": True, "iec_address": False, "write_protect_override": False})
        self.assertEqual(loaded.window_size, {"width": 1280, "height": 720})

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
