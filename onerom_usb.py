"""OneROM USB discovery, serial-number binding, and CDC telemetry support.

The UI never identifies a board by COM number.  COM ports are transient; the
OneROM USB serial is the stable identifier which is stored against a drive and
role.  The CDC handling follows the proven Monitor GUI sequence: open with
DTR low, then assert DTR after the device has observed a fresh attach.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # Keep the simulator usable before pyserial is installed.
    serial = None
    list_ports = None


BAUD_RATE = 115200
# A healthy OneROM CDC record is a short newline-terminated ASCII line.  A
# finite cap prevents a disconnected/malformed device that never terminates a
# record from accumulating an unbounded Python string in the GUI process.
MAX_RX_BUFFER_BYTES = 16 * 1024
STATE_RE = re.compile(r"STATE\s+([A-Za-z][0-9.]+)")
STATUS_RE = re.compile(r"STATUS\s+([A-Za-z][0-9.]+)")
MOTOR_RE = re.compile(r"MOTOR\s+state=(\d+)")
PHASE_RE = re.compile(r"PHASE\s+old=(\d+)\s+new=(\d+)\s+delta=(\d+)\s+motor=(\d+)")
TRACK_RE = re.compile(r"TRACK_WRITE\s+addr=\$0022\s+data=\$[0-9A-Fa-f]+\s+\((\d+)\)")
DENSITY_RE = re.compile(r"DENSITY\s+state=(\d+)")
WRITE_PROTECT_RE = re.compile(r"WRITE_PROTECT\s+state=(\d+)")
WRITE_GATE_RE = re.compile(r"WRITE_GATE\s+state=(\d+)")
STATUS_WP_RE = re.compile(r"\bWPV=(\d+)\s+WP=(\d+)")
STATUS_TRACK_RE = re.compile(r"\bTV=(\d+)\s+T=(\d+)")
STATUS_POSITION_RE = re.compile(r"\bTPV=(\d+)\s+TP2=(\d+)")
STATUS_DENSITY_RE = re.compile(r"\bDV=(\d+)\s+D=(\d+)")
STATUS_MOTOR_RE = re.compile(r"\bM=(\d+)")
STATUS_WRITE_GATE_RE = re.compile(r"\bWGV=(\d+)\s+WG=(\d+)")
STATUS_CAPTURE_RE = re.compile(
    r"\bCAP=(\d+)\s+PROD=(\d+)\s+CONS=(\d+)\s+ROV=(\d+)\s+QOV=(\d+)"
)
STATUS_CAPTURE_COMPACT_RE = re.compile(r"\bC=(\d+)\s+R=(\d+)\s+Q=(\d+)")
HDRPHY_RE = re.compile(r"\bHDRPHY\b.*?\bT=(\d+)\s+S=(\d+)")
HDRMETA_RE = re.compile(
    r"\bHDRMETA\b.*?\bID1=\$([0-9A-Fa-f]{2})\s+ID2=\$([0-9A-Fa-f]{2})"
    r"\s+CHK=\$([0-9A-Fa-f]{2})\s+OK=([01])"
)
RPM_RE = re.compile(r"\bRPM\b.*?\bRPM=([0-9]+(?:\.[0-9]+)?)")
SYNC_RE = re.compile(r"\bSYNC\b.*?\bCOUNT=(\d+)\s+LEVEL=(\d+)")
HEAD_STALL_TIMEOUT = 0.8


@dataclass(frozen=True)
class BoardDescriptor:
    """A USB CDC board whose stable identity is its USB serial number."""

    serial_number: str
    port: str
    description: str
    vid: int | None = None
    pid: int | None = None

    @property
    def label(self) -> str:
        return f"{self.serial_number}  —  {self.port}"


@dataclass
class DriveBinding:
    """One drive's OneROM role bindings and local display preferences."""

    version: int = 2
    drive_id: str = "1541 Drive"
    controller_serial: str = ""
    hud_serial: str = ""
    appearance: dict[str, str] = field(default_factory=dict)
    priorities: dict[str, int | None] = field(default_factory=dict)
    control_features: dict[str, bool] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("Binding configuration version must be a positive integer.")
        if self.controller_serial and self.controller_serial == self.hud_serial:
            raise ValueError("A OneROM serial can be assigned to only one role on a drive.")
        if not isinstance(self.appearance, dict):
            raise ValueError("Display appearance preferences must be a mapping.")
        if not isinstance(self.priorities, dict):
            raise ValueError("HUD priorities must be a mapping.")
        if not isinstance(self.control_features, dict) or not all(
            isinstance(name, str) and isinstance(enabled, bool)
            for name, enabled in self.control_features.items()
        ):
            raise ValueError("Control feature preferences must be a boolean mapping.")


@dataclass
class TelemetryState:
    firmware: str = ""
    motor: bool | None = None
    protected: bool | None = None
    writing: bool | None = None
    density: int | None = None
    position_half_tracks: int | None = None
    head: str = "PARK"
    rpm: float | None = None
    sector: int | None = None
    sync_count: int | None = None
    sync_level: int | None = None
    header_track: int | None = None
    header_id1: int | None = None
    header_id2: int | None = None
    header_checksum: int | None = None
    header_checksum_valid: bool | None = None
    capture_count: int | None = None
    produced_total: int | None = None
    consumed_total: int | None = None
    ring_overrun: int | None = None
    queue_overflow: int | None = None

    @property
    def track(self) -> str:
        if self.position_half_tracks is None:
            return "--.-"
        whole = self.position_half_tracks // 2
        return f"{whole:02d}{'.5' if self.position_half_tracks & 1 else '.0'}"


def discover_cdc_boards() -> list[BoardDescriptor]:
    """List serial-capable USB devices, keyed by serial instead of COM port."""
    if list_ports is None:
        return []
    boards: list[BoardDescriptor] = []
    for port in list_ports.comports():
        # A missing USB serial cannot safely survive a port renumbering.  Show
        # no persistent candidate rather than accidentally binding a drive.
        if not port.serial_number:
            continue
        boards.append(BoardDescriptor(
            serial_number=str(port.serial_number),
            port=str(port.device),
            description=str(port.description or "USB Serial Device"),
            vid=port.vid,
            pid=port.pid,
        ))
    return sorted(boards, key=lambda board: (board.serial_number, board.port))


def load_binding(path: Path) -> DriveBinding:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Binding configuration must be an object.")
        # Read only known fields. Newer configurations can therefore retain
        # harmless unknown data without making an older app discard every
        # saved USB assignment and display preference.
        binding = DriveBinding(
            version=raw.get("version", 1),
            drive_id=raw.get("drive_id", "1541 Drive"),
            controller_serial=raw.get("controller_serial", ""),
            hud_serial=raw.get("hud_serial", ""),
            appearance=raw.get("appearance", {}),
            priorities=raw.get("priorities", {}),
            control_features=raw.get("control_features", {}),
        )
        binding.validate()
        return binding
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return DriveBinding()


def save_binding(path: Path, binding: DriveBinding) -> None:
    binding.validate()
    path.write_text(json.dumps(asdict(binding), indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CdcBoardLink:
    """Non-blocking CDC reader for one board connected to one drive role."""

    def __init__(self, board: BoardDescriptor):
        self.board = board
        self.device = None
        self._buffer = ""
        self.opened_at = 0.0
        self.last_close_dtr_ms = 0.0
        self.last_close_handle_ms = 0.0

    @property
    def connected(self) -> bool:
        return self.device is not None and bool(getattr(self.device, "is_open", False))

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.close()
        device = serial.Serial()
        device.port = self.board.port
        device.baudrate = BAUD_RATE
        device.timeout = 0
        device.dtr = False
        device.rts = False
        device.open()
        self.device = device
        self._buffer = ""
        self.opened_at = time.monotonic()

    def assert_dtr(self) -> None:
        """Complete the low→high CDC attach edge after a short settling time."""
        if self.connected:
            self.device.dtr = True

    def read_lines(self) -> list[str]:
        if not self.connected:
            return []
        payload = self.device.read(4096)
        if payload:
            self._buffer += payload.decode("ascii", errors="ignore")
            if len(self._buffer) > MAX_RX_BUFFER_BYTES:
                # There is no valid record to preserve once an unterminated
                # line has exceeded this cap. Drop it and let the next
                # newline-terminated CDC record re-establish framing.
                self._buffer = ""
        lines: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    def write_command(self, command: str) -> None:
        """Send one documented OneROM USB Selector CDC command line."""
        if not self.connected:
            raise RuntimeError("OneROM USB serial link is not connected")
        line = command.strip()
        if not line:
            raise ValueError("OneROM command cannot be empty")
        self.device.write((line + "\r\n").encode("ascii"))
        self.device.flush()

    def close(self) -> None:
        if self.device is not None:
            device = self.device
            dtr_started = time.perf_counter()
            try:
                # Explicitly cancel any outstanding Win32 overlapped read
                # before changing the line state or closing the handle.
                # pyserial also does this internally, but making it explicit
                # lets release timing identify a driver that is stuck here.
                cancel_read = getattr(device, "cancel_read", None)
                if callable(cancel_read):
                    cancel_read()
                device.dtr = False
            except Exception:
                pass
            self.last_close_dtr_ms = (time.perf_counter() - dtr_started) * 1000
            handle_started = time.perf_counter()
            try:
                device.close()
            except Exception:
                pass
            self.last_close_handle_ms = (time.perf_counter() - handle_started) * 1000
        self.device = None


class DriveTelemetryParser:
    """Parser for the hardware-tested Monitor CDC telemetry grammar."""

    def __init__(self) -> None:
        self.state = TelemetryState()
        self._last_motion = 0.0
        self._last_requested_track: int | None = None
        self._last_direction: str | None = None

    def process(self, line: str) -> TelemetryState:
        self._refresh_head_state()
        state_match = STATE_RE.search(line) or STATUS_RE.search(line)
        if state_match:
            self.state.firmware = state_match.group(1)
            self._state_snapshot(line)
            return self.state

        match = DENSITY_RE.search(line)
        if match:
            self.state.density = int(match.group(1)) & 3
            return self.state
        match = WRITE_PROTECT_RE.search(line)
        if match:
            # Physical write testing establishes the installed sensor
            # polarity: state=0 is the asserted no-notch/protected condition;
            # state=1 permits writing.
            self.state.protected = not bool(int(match.group(1)))
            return self.state
        match = WRITE_GATE_RE.search(line)
        if match:
            self.state.writing = bool(int(match.group(1)))
            return self.state
        match = MOTOR_RE.search(line)
        if match:
            self._set_motor(bool(int(match.group(1))))
            return self.state
        match = TRACK_RE.search(line)
        if match:
            self._set_track(int(match.group(1)))
            return self.state
        match = PHASE_RE.search(line)
        if match:
            self._phase(int(match.group(3)))
            return self.state
        match = HDRPHY_RE.search(line)
        if match:
            self.state.header_track = int(match.group(1))
            self.state.sector = int(match.group(2))
            return self.state
        match = HDRMETA_RE.search(line)
        if match:
            self.state.header_id1 = int(match.group(1), 16)
            self.state.header_id2 = int(match.group(2), 16)
            self.state.header_checksum = int(match.group(3), 16)
            self.state.header_checksum_valid = bool(int(match.group(4)))
            return self.state
        match = RPM_RE.search(line)
        if match:
            self.state.rpm = float(match.group(1))
            return self.state
        match = SYNC_RE.search(line)
        if match:
            self.state.sync_count = int(match.group(1))
            self.state.sync_level = int(match.group(2))
        return self.state

    def refresh(self) -> TelemetryState:
        """Refresh time-based state when the CDC stream has no new records."""
        self._refresh_head_state()
        return self.state

    def _state_snapshot(self, line: str) -> None:
        match = STATUS_WP_RE.search(line)
        if match and int(match.group(1)):
            self.state.protected = not bool(int(match.group(2)))
        match = STATUS_DENSITY_RE.search(line)
        if match and int(match.group(1)):
            self.state.density = int(match.group(2)) & 3
        match = STATUS_MOTOR_RE.search(line)
        if match:
            self._set_motor(bool(int(match.group(1))))
        match = STATUS_WRITE_GATE_RE.search(line)
        if match and int(match.group(1)):
            self.state.writing = bool(int(match.group(2)))
        match = STATUS_CAPTURE_RE.search(line)
        if match:
            self.state.capture_count = int(match.group(1))
            self.state.produced_total = int(match.group(2))
            self.state.consumed_total = int(match.group(3))
            self.state.ring_overrun = int(match.group(4))
            self.state.queue_overflow = int(match.group(5))
        else:
            # Compact health records are deliberately capped below the
            # 64-byte TinyUSB TX FIFO; only these three values are rendered.
            match = STATUS_CAPTURE_COMPACT_RE.search(line)
            if match:
                self.state.capture_count = int(match.group(1))
                self.state.ring_overrun = int(match.group(2))
                self.state.queue_overflow = int(match.group(3))
        match = STATUS_POSITION_RE.search(line)
        if match and int(match.group(1)):
            self.state.position_half_tracks = max(2, int(match.group(2)))
        else:
            match = STATUS_TRACK_RE.search(line)
            if match and int(match.group(1)):
                self._set_track(int(match.group(2)))

    def _set_motor(self, motor: bool) -> None:
        self.state.motor = motor
        if not motor:
            self.state.head = "PARK"
            self.state.rpm = 0.0
        elif self._last_motion <= 0:
            self._last_motion = time.monotonic()

    def _refresh_head_state(self) -> None:
        """Report a stall after 0.8 seconds without target or phase activity."""
        if not self.state.motor:
            self.state.head = "PARK"
        elif self._last_motion and time.monotonic() - self._last_motion >= HEAD_STALL_TIMEOUT:
            self.state.head = "STALL"

    def _set_track(self, track: int) -> None:
        # $0022 is a DOS destination-track hint, not a continuously reliable
        # head-position source. It provides the requested direction, but only
        # anchors the displayed position until phase or status data arrives.
        if self.state.position_half_tracks is None:
            self.state.position_half_tracks = max(2, track * 2)
        previous_track = self._last_requested_track
        self._last_requested_track = track
        if not self.state.motor:
            self.state.head = "PARK"
            return
        if previous_track is None:
            # Establish a baseline only. There is no direction until a
            # subsequent target differs.
            self._last_motion = time.monotonic()
            return
        if track == previous_track:
            # STATE records repeat the latest target. They are snapshots, not
            # fresh seek requests, so they must not erase IN/OUT or postpone
            # the stall timer.
            return
        self._last_motion = time.monotonic()
        if track > previous_track:
            self.state.head = "IN"
            self._last_direction = "IN"
        else:
            self.state.head = "OUT"
            self._last_direction = "OUT"

    def _phase(self, delta: int) -> None:
        # Every observed phase transition proves that the head is active. A
        # delta of two can occur when capture skips an intermediate phase, so
        # it must refresh the motion timer instead of falsely reporting STALL.
        if self.state.motor:
            self._last_motion = time.monotonic()
        if delta == 1:
            if self.state.position_half_tracks is not None:
                self.state.position_half_tracks += 1
            self.state.head = "IN" if self.state.motor else "PARK"
            if self.state.motor:
                self._last_direction = "IN"
        elif delta == 3:
            if self.state.position_half_tracks is not None:
                self.state.position_half_tracks = max(2, self.state.position_half_tracks - 1)
            self.state.head = "OUT" if self.state.motor else "PARK"
            if self.state.motor:
                self._last_direction = "OUT"
        elif self.state.motor and self._last_direction is not None:
            # Direction is ambiguous after a skipped sample; retain the last
            # confirmed target/phase direction until a later record resolves
            # it, rather than replacing live movement with STALL.
            self.state.head = self._last_direction

