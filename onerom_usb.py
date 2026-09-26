"""OneROM USB discovery, serial-number binding, and CDC telemetry support.

The UI never identifies a board by COM number.  COM ports are transient; the
OneROM USB serial is the stable identifier which is stored against a drive and
role.  The CDC handling follows the proven DriveHUD GUI sequence: open with
DTR low, then assert DTR after the device has observed a fresh attach.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
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
STATE_RE = re.compile(r"STATE\s+(V[0-9.]+)")
STATUS_RE = re.compile(r"STATUS\s+(V[0-9.]+)")
MOTOR_RE = re.compile(r"MOTOR\s+state=(\d+)")
PHASE_RE = re.compile(r"PHASE\s+old=(\d+)\s+new=(\d+)\s+delta=(\d+)\s+motor=(\d+)")
TRACK_RE = re.compile(r"TRACK_WRITE\s+addr=\$0022\s+data=\$[0-9A-Fa-f]+\s+\((\d+)\)")
DENSITY_RE = re.compile(r"DENSITY\s+state=(\d+)")
WRITE_PROTECT_RE = re.compile(r"WRITE_PROTECT\s+state=(\d+)")
STATUS_WP_RE = re.compile(r"\bWPV=(\d+)\s+WP=(\d+)")
STATUS_TRACK_RE = re.compile(r"\bTV=(\d+)\s+T=(\d+)")
STATUS_POSITION_RE = re.compile(r"\bTPV=(\d+)\s+TP2=(\d+)")
STATUS_DENSITY_RE = re.compile(r"\bDV=(\d+)\s+D=(\d+)")
STATUS_MOTOR_RE = re.compile(r"\bM=(\d+)")


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
    """The two optional OneROM roles attached to one physical drive."""

    drive_id: str = "1541 Drive"
    controller_serial: str = ""
    hud_serial: str = ""

    def validate(self) -> None:
        if self.controller_serial and self.controller_serial == self.hud_serial:
            raise ValueError("A OneROM serial can be assigned to only one role on a drive.")


@dataclass
class TelemetryState:
    firmware: str = ""
    motor: bool | None = None
    protected: bool | None = None
    density: int | None = None
    position_half_tracks: int | None = None
    head: str = "PARK"

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
        binding = DriveBinding(**raw)
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
        lines: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    def close(self) -> None:
        if self.device is not None:
            try:
                self.device.dtr = False
                self.device.close()
            except Exception:
                pass
        self.device = None


class DriveTelemetryParser:
    """Parser for the hardware-tested DriveHUD CDC telemetry grammar."""

    def __init__(self) -> None:
        self.state = TelemetryState()
        self._last_motion = 0.0

    def process(self, line: str) -> TelemetryState:
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
            self.state.protected = bool(int(match.group(1)))
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

    def _state_snapshot(self, line: str) -> None:
        match = STATUS_WP_RE.search(line)
        if match and int(match.group(1)):
            self.state.protected = bool(int(match.group(2)))
        match = STATUS_DENSITY_RE.search(line)
        if match and int(match.group(1)):
            self.state.density = int(match.group(2)) & 3
        match = STATUS_MOTOR_RE.search(line)
        if match:
            self._set_motor(bool(int(match.group(1))))
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

    def _set_track(self, track: int) -> None:
        self.state.position_half_tracks = max(2, track * 2)

    def _phase(self, delta: int) -> None:
        if delta == 1 and self.state.position_half_tracks is not None:
            self.state.position_half_tracks += 1
            self.state.head = "IN" if self.state.motor else "PARK"
            self._last_motion = time.monotonic()
        elif delta == 3 and self.state.position_half_tracks is not None:
            self.state.position_half_tracks = max(2, self.state.position_half_tracks - 1)
            self.state.head = "OUT" if self.state.motor else "PARK"
            self._last_motion = time.monotonic()

