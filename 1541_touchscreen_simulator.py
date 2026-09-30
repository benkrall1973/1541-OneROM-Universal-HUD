#!/usr/bin/env python3
"""1541 OneROM 7-inch touchscreen layout simulator.

This is a personal Windows design prototype.  It deliberately has no serial,
firmware, or repository connection: its purpose is to refine the Pi display
layout before the hardware arrives.
"""
from __future__ import annotations

import time
import math
import colorsys
from dataclasses import asdict, dataclass, field
import json
import re
import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # Keep GUI layout work usable before pyserial is installed.
    serial = None
    list_ports = None


# The CDC transport, parser, and persistent binding schema live here rather
# than in a companion module.  That makes this one file the complete desktop
# application; its USB protocol behavior remains the same as the tested
# standalone implementation it replaced.
BAUD_RATE = 115200
MAX_RX_BUFFER_BYTES = 16 * 1024
HEAD_STALL_TIMEOUT = 0.8
# A 1541 has no track-zero switch.  A conventional home seek deliberately
# drives the head outward past the complete 42-track envelope, then reverses.
# We need 84 observed half-track transitions before treating that sequence as
# a credible physical home rather than an ordinary outward seek.
HOME_OUTWARD_HALF_STEPS = 84
DOS_MAX_TRACK_HALF_STEPS = 70  # Track 35.0
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
STATUS_CAPTURE_RE = re.compile(r"\bCAP=(\d+)\s+PROD=(\d+)\s+CONS=(\d+)\s+ROV=(\d+)\s+QOV=(\d+)")
STATUS_CAPTURE_COMPACT_RE = re.compile(r"\bC=(\d+)\s+R=(\d+)\s+Q=(\d+)")
HDRPHY_RE = re.compile(r"\bHDRPHY\b.*?\bT=(\d+)\s+S=(\d+)")
HDRMETA_RE = re.compile(r"\bHDRMETA\b.*?\bID1=\$([0-9A-Fa-f]{2})\s+ID2=\$([0-9A-Fa-f]{2})\s+CHK=\$([0-9A-Fa-f]{2})\s+OK=([01])")
RPM_RE = re.compile(r"\bRPM\b.*?\bRPM=([0-9]+(?:\.[0-9]+)?)")
SYNC_RE = re.compile(r"\bSYNC\b.*?\bCOUNT=(\d+)\s+LEVEL=(\d+)")


@dataclass(frozen=True)
class BoardDescriptor:
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
    version: int = 2
    drive_id: str = "1541 Drive"
    controller_serial: str = ""
    hud_serial: str = ""
    appearance: dict[str, str] = field(default_factory=dict)
    priorities: dict[str, int | None] = field(default_factory=dict)
    control_features: dict[str, bool] = field(default_factory=dict)
    window_size: dict[str, int] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("Binding configuration version must be a positive integer.")
        if self.controller_serial and self.controller_serial == self.hud_serial:
            raise ValueError("A OneROM serial can be assigned to only one role on a drive.")
        if not isinstance(self.appearance, dict) or not isinstance(self.priorities, dict):
            raise ValueError("Display preferences must be mappings.")
        if not isinstance(self.control_features, dict) or not all(isinstance(k, str) and isinstance(v, bool) for k, v in self.control_features.items()):
            raise ValueError("Control feature preferences must be a boolean mapping.")
        if not isinstance(self.window_size, dict) or not all(
            name in {"width", "height"} and isinstance(value, int) and not isinstance(value, bool)
            for name, value in self.window_size.items()
        ):
            raise ValueError("Window size must be an integer width/height mapping.")


@dataclass
class TelemetryState:
    firmware: str = ""
    motor: bool | None = None
    protected: bool | None = None
    writing: bool | None = None
    density: int | None = None
    position_half_tracks: int | None = None
    position_source: str = "UNANCHORED"
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
        return f"{self.position_half_tracks // 2:02d}{'.5' if self.position_half_tracks & 1 else '.0'}"


def track_is_over_dos_range(position_half_tracks: int | None) -> bool:
    """Whether a tracked head position is beyond standard 35-track DOS media."""
    return position_half_tracks is not None and position_half_tracks > DOS_MAX_TRACK_HALF_STEPS


def discover_cdc_boards() -> list[BoardDescriptor]:
    if list_ports is None:
        return []
    boards = [BoardDescriptor(str(p.serial_number), str(p.device), str(p.description or "USB Serial Device"), p.vid, p.pid)
              for p in list_ports.comports() if p.serial_number]
    return sorted(boards, key=lambda board: (board.serial_number, board.port))


def load_binding(path: Path) -> DriveBinding:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Binding configuration must be an object.")
        binding = DriveBinding(version=raw.get("version", 1), drive_id=raw.get("drive_id", "1541 Drive"),
                               controller_serial=raw.get("controller_serial", ""), hud_serial=raw.get("hud_serial", ""),
                               appearance=raw.get("appearance", {}), priorities=raw.get("priorities", {}),
                               control_features=raw.get("control_features", {}), window_size=raw.get("window_size", {}))
        binding.validate()
        return binding
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return DriveBinding()


def save_binding(path: Path, binding: DriveBinding) -> None:
    binding.validate()
    path.write_text(json.dumps(asdict(binding), indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CdcBoardLink:
    def __init__(self, board: BoardDescriptor):
        self.board, self.device, self._buffer, self.opened_at = board, None, "", 0.0
        self.last_close_dtr_ms = self.last_close_handle_ms = 0.0

    @property
    def connected(self) -> bool:
        return self.device is not None and bool(getattr(self.device, "is_open", False))

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.close()
        device = serial.Serial()
        device.port, device.baudrate, device.timeout = self.board.port, BAUD_RATE, 0
        device.dtr = device.rts = False
        device.open()
        self.device, self._buffer, self.opened_at = device, "", time.monotonic()

    def assert_dtr(self) -> None:
        if self.connected:
            self.device.dtr = True

    def read_lines(self) -> list[str]:
        if not self.connected:
            return []
        payload = self.device.read(4096)
        if payload:
            self._buffer += payload.decode("ascii", errors="ignore")
            if len(self._buffer) > MAX_RX_BUFFER_BYTES:
                self._buffer = ""
        lines: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                lines.append(line.strip())
        return lines

    def write_command(self, command: str) -> None:
        if not self.connected:
            raise RuntimeError("OneROM USB serial link is not connected")
        line = command.strip()
        if not line:
            raise ValueError("OneROM command cannot be empty")
        self.device.write((line + "\r\n").encode("ascii"))
        self.device.flush()

    def close(self) -> None:
        if self.device is not None:
            device, started = self.device, time.perf_counter()
            try:
                cancel_read = getattr(device, "cancel_read", None)
                if callable(cancel_read):
                    cancel_read()
                device.dtr = False
            except Exception:
                pass
            self.last_close_dtr_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            try:
                device.close()
            except Exception:
                pass
            self.last_close_handle_ms = (time.perf_counter() - started) * 1000
        self.device = None


class DriveTelemetryParser:
    def __init__(self) -> None:
        self.state, self._last_motion = TelemetryState(), 0.0
        self._last_requested_track: int | None = None
        self._last_direction: str | None = None
        self._outward_home_steps = 0
        self._home_assumed = False

    def process(self, line: str) -> TelemetryState:
        self._refresh_head_state()
        state_match = STATE_RE.search(line) or STATUS_RE.search(line)
        if state_match:
            self.state.firmware = state_match.group(1); self._state_snapshot(line); return self.state
        for matcher, setter in ((DENSITY_RE, lambda m: setattr(self.state, "density", int(m.group(1)) & 3)),
                                (WRITE_PROTECT_RE, lambda m: setattr(self.state, "protected", not bool(int(m.group(1))))),
                                (WRITE_GATE_RE, lambda m: setattr(self.state, "writing", bool(int(m.group(1)))))):
            match = matcher.search(line)
            if match:
                setter(match); return self.state
        match = MOTOR_RE.search(line)
        if match: self._set_motor(bool(int(match.group(1)))); return self.state
        match = TRACK_RE.search(line)
        if match: self._set_track(int(match.group(1))); return self.state
        match = PHASE_RE.search(line)
        if match: self._phase(int(match.group(3))); return self.state
        match = HDRPHY_RE.search(line)
        if match: self.state.header_track, self.state.sector = int(match.group(1)), int(match.group(2)); return self.state
        match = HDRMETA_RE.search(line)
        if match:
            self.state.header_id1, self.state.header_id2, self.state.header_checksum = int(match.group(1), 16), int(match.group(2), 16), int(match.group(3), 16)
            self.state.header_checksum_valid = bool(int(match.group(4)))
            # A checksum-valid physical header is genuine on-disk evidence,
            # so it takes precedence over our mechanical home estimate.
            if self.state.header_checksum_valid and self.state.header_track is not None:
                self.state.position_half_tracks = max(2, self.state.header_track * 2)
                self.state.position_source = "HEADER"
                self._home_assumed = False
            return self.state
        match = RPM_RE.search(line)
        if match: self.state.rpm = float(match.group(1)); return self.state
        match = SYNC_RE.search(line)
        if match: self.state.sync_count, self.state.sync_level = int(match.group(1)), int(match.group(2))
        return self.state

    def refresh(self) -> TelemetryState:
        self._refresh_head_state(); return self.state

    def _state_snapshot(self, line: str) -> None:
        match = STATUS_WP_RE.search(line)
        if match and int(match.group(1)): self.state.protected = not bool(int(match.group(2)))
        match = STATUS_DENSITY_RE.search(line)
        if match and int(match.group(1)): self.state.density = int(match.group(2)) & 3
        match = STATUS_MOTOR_RE.search(line)
        if match: self._set_motor(bool(int(match.group(1))))
        match = STATUS_WRITE_GATE_RE.search(line)
        if match and int(match.group(1)): self.state.writing = bool(int(match.group(2)))
        match = STATUS_CAPTURE_RE.search(line)
        if match:
            self.state.capture_count, self.state.produced_total, self.state.consumed_total, self.state.ring_overrun, self.state.queue_overflow = map(int, match.groups())
        else:
            match = STATUS_CAPTURE_COMPACT_RE.search(line)
            if match: self.state.capture_count, self.state.ring_overrun, self.state.queue_overflow = map(int, match.groups())
        match = STATUS_POSITION_RE.search(line)
        if match and int(match.group(1)):
            self.state.position_half_tracks = max(2, int(match.group(2)))
            self.state.position_source = "TARGET"
        else:
            match = STATUS_TRACK_RE.search(line)
            if match and int(match.group(1)): self._set_track(int(match.group(2)))

    def _set_motor(self, motor: bool) -> None:
        self.state.motor = motor
        if not motor: self.state.head, self.state.rpm = "PARK", 0.0
        elif self._last_motion <= 0: self._last_motion = time.monotonic()

    def _refresh_head_state(self) -> None:
        if not self.state.motor: self.state.head = "PARK"
        elif self._last_motion and time.monotonic() - self._last_motion >= HEAD_STALL_TIMEOUT: self.state.head = "STALL"

    def _set_track(self, track: int) -> None:
        # $0022 is explicit drive-code target information.  It is an anchor,
        # not merely a starting hint, and must correct any home estimate.
        self.state.position_half_tracks = max(2, track * 2)
        self.state.position_source = "TARGET"
        self._home_assumed = False
        previous, self._last_requested_track = self._last_requested_track, track
        if not self.state.motor: self.state.head = "PARK"; return
        if previous is None: self._last_motion = time.monotonic(); return
        if track == previous: return
        self._last_motion, self._last_direction = time.monotonic(), "IN" if track > previous else "OUT"
        self.state.head = self._last_direction

    def _phase(self, delta: int) -> None:
        if self.state.motor: self._last_motion = time.monotonic()
        if delta == 1:
            if self._home_assumed:
                # The outward run ends with the head at the Track-1 bump.
                # This inward phase transition then moves it to Track 1.5.
                # The diagnostic cartridge proves this: 34 inward transitions
                # after home land at selected Track 18.0.
                self.state.position_half_tracks = 3
                self.state.position_source = "HOME EST."
                self._home_assumed = False
            elif self.state.position_half_tracks is not None:
                self.state.position_half_tracks += 1
            self.state.head = "IN" if self.state.motor else "PARK"
            if self.state.motor:
                self._last_direction = "IN"
                self._outward_home_steps = 0
        elif delta == 3:
            if self.state.position_half_tracks is not None: self.state.position_half_tracks = max(2, self.state.position_half_tracks - 1)
            self.state.head = "OUT" if self.state.motor else "PARK"
            if self.state.motor:
                self._last_direction = "OUT"
                self._outward_home_steps += 1
                if self._outward_home_steps >= HOME_OUTWARD_HALF_STEPS:
                    self._home_assumed = True
        elif self.state.motor and self._last_direction is not None: self.state.head = self._last_direction


WIDTH, HEIGHT = 1280, 720
APP_VERSION = "V0.0.22"
HUD_VISIBLE_CARD_COUNT = 5
HUD_CARD_TOP = 100
# Five rows exactly fill the same y=100…672 span as the four scroll
# controls. The 4-pixel breathing room remains between adjacent cards.
HUD_CARD_HEIGHT = 111.2
HUD_CARD_PITCH = 115.2
HUD_CARD_RIGHT = 1128
HUD_HELP_LEFT, HUD_HELP_RIGHT = 954, 1026
HUD_PRIORITY_LEFT, HUD_PRIORITY_RIGHT = 1036, 1110
# The override action uses the same 10-pixel gap before Help as Help uses
# before Priority, while remaining wide enough for its full safety label.
HUD_OVERRIDE_LEFT, HUD_OVERRIDE_RIGHT = 720, 944
# Refresh is deliberately compact so its feedback can remain visible in the
# detail lane. It preserves the 10-pixel gap before Help.
HUD_REFRESH_LEFT, HUD_REFRESH_RIGHT = 810, 944
HUD_SCROLL_LEFT, HUD_SCROLL_RIGHT = 1150, 1256
CONTROL_REPLY_TIMEOUT_MS = 3000
# The FIFO has 1,026 virtual pixels from x=230 to the card's right edge.
# 31 "88" entries plus their 30 separators are 92 fixed-width glyphs; at
# the 18px Cascadia Mono HUD font that leaves a safe right-hand margin.
RECENT_SECTOR_FIFO_SIZE = 31
# Bench calibration: 94% on the prior baseline measured as a real 7 inches.
# That physical size is now the user-facing 100% baseline.
SEVEN_INCH_BASE_SCALE = 1.3818
BG = "#101820"
PANEL = "#182632"
PANEL_ALT = "#203442"
TEXT = "#eef6fa"
MUTED = "#a9bbc4"
ACCENT = "#33c3a5"
WARNING = "#f6c85f"
OFFLINE = "#ef6b73"
# A muted red surface makes an overrange Track row unmistakable without
# replacing the whole dashboard with a flashing emergency sign.
TRACK_ALERT_PANEL = "#3a2029"
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

COLOR_PALETTES = {
    "background": ("Background", ("#101820", "#07121d", "#1b1b1f", "#24303a", "#3b2b1e", "#142331", "#222b38", "#2d2638", "#f1e8d5", "#0c2227", "#15261a", "#2a1e26", "#303030", "#22314c", "#3c3322", "#132b3a", "#241d35", "#e7edf0")),
    "card": ("Card Surface", ("#182632", "#203442", "#263945", "#2d3645", "#382f45", "#2e3c34", "#252525", "#3a3025", "#e6e6e6", "#24404a", "#2f4a37", "#4a3542", "#3b3b3b", "#34445b", "#4b4231", "#25495a", "#3e324f", "#f4f4f4")),
    "button_surface": ("Button Surface", ("#203442", "#182632", "#263945", "#2d3645", "#382f45", "#2e3c34", "#252525", "#3a3025", "#24404a", "#2f4a37", "#4a3542", "#3b3b3b", "#34445b", "#4b4231", "#25495a", "#3e324f")),
    "accent": ("Accent / Active", ("#33c3a5", "#ef6b73", "#4ea1ff", "#f6c85f", "#af7bff", "#59c36a", "#e77cb4", "#f08a4b", "#e9eef3", "#00a8a8", "#ff8c42", "#61dafb", "#ffcc4d", "#9370db", "#2ecc71", "#ff6b9a", "#ff7043", "#ffffff")),
    "text_primary": ("Primary Text", ("#eef6fa", "#ffffff", "#c9e4ff", "#ffdf8a", "#5eead4", "#f0b4da", "#d0d9e5", "#f1cfa5", "#dff8f4", "#bffff4", "#e5ffd7", "#e4e4e4", "#d6e5ff", "#ffe1b8", "#c9efff", "#e7d3ff")),
    "text_secondary": ("Secondary Text", ("#a9bbc4", "#d5e2e8", "#91b8d6", "#d4bc86", "#8acfc3", "#d4a8bf", "#b8c0ce", "#d8af82", "#f0f4f7", "#9fd3d6", "#c3d9bc", "#deb4cf", "#c8c8c8", "#b5c7e0", "#e1c59d", "#9ed1e6", "#cfb5e8", "#ffffff")),
    "warning": ("Warning Text", ("#f6c85f", "#ffcc4d", "#ff8c42", "#f08a4b", "#ffdf8a", "#ffd166", "#ffb703", "#f4a261", "#e9c46a", "#f7b267")),
    "offline": ("Offline / Error Text", ("#ef6b73", "#ff6b9a", "#ff7043", "#e77cb4", "#f08a4b", "#ff5c5c", "#ff8a8a", "#ff4d6d", "#d95d8a", "#ff9f1c")),
}

APPEARANCE_CHOICES = (
    ("Background", "background"),
    ("Card Surface", "card"),
    ("Button Surface", "button_surface"),
    ("Accent / Active", "accent"),
    ("Primary Text", "text_primary"),
    ("Secondary Text", "text_secondary"),
    ("Warning Text", "warning"),
    ("Offline / Error Text", "offline"),
)
APPEARANCE_TARGETS = frozenset(target for _label, target in APPEARANCE_CHOICES)
DEFAULT_HUD_CARD_PRIORITIES = {
    "track": 1,
    "rotation": 2,
    "head": 3,
    "activity": 4,
    "write_protect": 5,
    "density": 6,
    "sync_per_rev": 7,
    # Every available card is persisted, even when it starts unpinned. That
    # makes P– an explicit user-editable state rather than an absent setting.
    "physical_header": None,
    "sector_coverage": None,
    "sector_fifo": None,
    "capture_health": None,
    "disk_identity": None,
    "header_rate": None,
    "capture_rate": None,
    "sync_rate": None,
    "mechanism": None,
    "recent_evidence": None,
    "startup_rom": None,
    "boot_iec": None,
}
DEFAULT_CONTROL_FEATURES = {
    "rom_select": True,
    "iec_address": False,
    "write_protect_override": False,
}

# Help text is intentionally concise enough for the fixed touchscreen modal,
# but specific enough to explain provenance, calculation, and limitations.
DETAILED_CARD_HELP = {
    "track": "Estimated head position in whole/half tracks. Source: target-track writes plus phase transitions. A decoded physical header corrects the estimate. HEADER Δ = estimated track − latest header track; it is unavailable until a valid header arrives.",
    "rotation": "Qualified spindle speed. RPM = SYNC pulses/second × 60 ÷ expected marks/revolution: D3=42, D2=38, D1=36, D0=34. Only 240–360 RPM is accepted; FW is the firmware's independent RPM report. The disk graphic appears only when motor telemetry is ON.",
    "activity": "READING means motor ON with no recent write-gate pulse; WRITING means a write-gate pulse was observed; OFF means motor OFF. WRITE PULSES and STEPS are cumulative Monitor-session observations, not DOS file-operation counts.",
    "physical_header": "Newest checksum-decoded on-disk GCR header: physical track and sector. It is stronger evidence than an estimated position. It clears after a seek or motor stop so the HUD never claims an old header describes the current head location.",
    "sector_coverage": "Unique physical sector numbers seen on the current track window. Expected sectors are D3=21, D2=19, D1=18, D0=17. SEEN x/y is coverage, not a bad-sector test: normal drive activity may never encounter every sector.",
    "sector_fifo": "Chronological FIFO of checksum-decoded HDRPHY sector values. It stores 31 real S## observations from one track, including repeats. It clears on seek, track change, or motor stop so sectors from different tracks are never combined.",
    "capture_health": "CAP is the firmware capture counter. ROV is raw capture-ring overrun; any increase means raw events were lost. QOV is diagnostic queue overflow; it can drop display records while raw capture continues. DROPS = (ROV−baseline) + (QOV−baseline). Clear Drops changes only the local baseline.",
    "disk_identity": "Disk ID comes from GCR header metadata. The HUD accepts it only after two checksum-valid headers agree. VERIFYING needs more evidence; ID CONFLICT means valid headers disagree. This checks header metadata only—not directory, DOS errors, files, or media quality.",
    "density": "1541 GCR density zone inferred from Monitor timing/header telemetry. It selects the geometry used elsewhere: sectors/track D3=21, D2=19, D1=18, D0=17; SYNC marks/revolution D3=42, D2=38, D1=36, D0=34.",
    "head": "IN = observed target/phase movement toward higher tracks; OUT = toward lower tracks. STALL = motor ON with no target or phase movement for 0.8 seconds. PARK = motor OFF. Position remains an estimate until a physical header confirms it.",
    "header_rate": "Rolling physical-header decode rate. Calculation: valid decoded headers observed during the last five seconds ÷ elapsed window seconds. It measures what the passive Monitor sees, not guaranteed disk health or a DOS read rate.",
    "capture_rate": "Passive capture-event throughput. Calculation: change in CAP ÷ time between usable STATUS records. The card uses K/S for thousands per second. It measures firmware observation work, not spindle RPM, bytes read, or DOS transfer speed.",
    "sync_rate": "Raw SYNC pulses counted in the latest approximately one-second capture interval. It feeds RPM only when motor state and density geometry are known. A seek, motor transition, or formatting pass can produce a partial interval.",
    "sync_per_rev": "Physical SYNC-mark estimate. Calculation: SYNC/second × 60 ÷ firmware RPM; EST is the unrounded value and the main value is rounded. Compare it with the density expectation. Nonstandard or copy-protected media can intentionally differ.",
    "mechanism": "Cumulative observed phase/step transitions since this Monitor connection. It helps reveal seeking or repeated mechanical activity, but is neither an absolute head-position counter nor a per-disk counter. Reconnect the Monitor to reset it.",
    "recent_evidence": "Compact chronological evidence: decoded headers, seek direction, motor changes, and write-gate activity. It is a passive Monitor trace of what actually arrived over telemetry. DOS errors, retries, directory operations, and file names are not exposed.",
    "startup_rom": "Selects the Control OneROM startup ROM slot. After confirmation the desktop sends ROMSET=<slot> and waits for the firmware's $ROMTEST,SET,OK or FAIL reply. A confirmed save changes boot selection; it does not hot-swap the ROM currently executing.",
    "boot_iec": "Selects the Control OneROM boot IEC device number, 8 through 11. After confirmation the desktop sends ROMIEC=<address> and requires $ROMTEST,IEC,OK with matching ADDRESS and VERIFY fields. The new address is verified by rebooting the drive.",
    "write_protect": "Shows the physical disk-notch sensor and optionally controls the Control OneROM X2 writable override. ENABLE sends ROMWP=ON; DISABLE sends ROMWP=OFF. The card changes only after a matching $ROMTEST,WP verification reply. Override changes permission, not disk contents.",
}


class TouchSimulator(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"1541 OneROM {APP_VERSION} — 7-inch Touchscreen Simulator")
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.minsize(980, 600)
        self.configure(bg=BG)

        # A real drive starts disconnected.  The two role cards on Settings
        # are the only way to attach physical OneROMs to this drive.
        self.ub3 = tk.BooleanVar(value=False)
        self.ub4 = tk.BooleanVar(value=False)
        # Desktop-only layout aid: exposes the optional Control OneROM UI
        # without pretending there is a serial connection.  It is never used
        # by discovery, telemetry, command dispatch, or drive calculations.
        # Preview is explicit and defaults on only for the Windows design
        # host. Production/Pi deployments default to real hardware state;
        # ONEROM_PREVIEW=1 can deliberately re-enable preview there.
        self.controller_gui_preview = os.environ.get(
            "ONEROM_PREVIEW", "1" if os.name == "nt" else "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # These mirror the optional Control OneROM capabilities selected in
        # the board's configuration JSON at compile time.  The touchscreen may
        # show only the controls the finished Control OneROM actually supports.
        self.controller_rom_enabled = tk.BooleanVar(value=DEFAULT_CONTROL_FEATURES["rom_select"])
        self.controller_iec_enabled = tk.BooleanVar(value=DEFAULT_CONTROL_FEATURES["iec_address"])
        self.controller_wp_enabled = tk.BooleanVar(value=DEFAULT_CONTROL_FEATURES["write_protect_override"])
        self.startup = tk.StringVar(value="Automatic")
        self.idle_seconds = tk.IntVar(value=300)
        self.preview_ppi = tk.DoubleVar(value=102.4)
        # Slot 0 is the OneROM bootloader and deliberately not selectable.
        # Present every selectable startup slot, exactly as the real UB3
        # firmware reports them.
        self.rom_choices = (
            "Slot 1 — ORIGINAL", "Slot 2 — JIFFYDOS", "Slot 3 — ORIGINAL",
            "Slot 4 — JIFFYDOS", "Slot 5 — ORIGINAL", "Slot 6 — JIFFYDOS",
            "Slot 7 — ORIGINAL",
        )
        self.iec_choices = ("Device 8", "Device 9", "Device 10", "Device 11")
        # These Control OneROM settings are rendered as dashboard cards. They
        # used to be owned by the now-retired standalone Control page.
        self.rom_var = tk.StringVar(value="Slot 2 — JIFFYDOS")
        self.rom_choice = tk.StringVar(value="Slot 2 — JIFFYDOS")
        self.iec_var = tk.StringVar(value="Device 8")
        self.iec_choice = tk.StringVar(value="Device 8")
        self.writable = tk.BooleanVar(value=False)
        self.control_status = tk.StringVar()
        self.current_page = "hud"
        self.last_input = time.monotonic()
        self.screensaver = False
        self.track = 0
        self.sector = 6
        self.motor = False
        self.head_direction = "IN"
        self.write_protect_prompt: bool | None = None
        self.clear_drops_prompt = False
        # ROM and IEC selection is deliberately a two-step operation: a
        # menu chooses the pending value, then an explicit confirmation is
        # required before a Control OneROM command can be sent.
        self.dashboard_setting_prompt: dict[str, str] | None = None
        self.color_picker: str | None = None
        self.popup_menu: dict | None = None
        self.appearance_target = "background"
        self.gradient_hue = 0.47
        self.hex_keyboard = False
        self.binding_path = Path(__file__).with_name("onerom_drive_bindings.json")
        self.drive_binding = load_binding(self.binding_path)
        try:
            self.drive_binding.validate()
        except ValueError:
            # Never carry a corrupt historical duplicate assignment forward.
            self.drive_binding = DriveBinding(drive_id="1541 Drive")
        self.apply_saved_appearance()
        self.load_saved_control_features()
        self.controller_serial = tk.StringVar(value=self.drive_binding.controller_serial)
        self.hud_serial = tk.StringVar(value=self.drive_binding.hud_serial)
        self.usb_boards = {}
        self.usb_links: dict[str, CdcBoardLink] = {}
        self.telemetry_parser = DriveTelemetryParser()
        self.usb_status = tk.StringVar(value="USB discovery has not run.")
        self.serial_last_error = tk.StringVar(value="")
        self.serial_diagnostic_path = Path(__file__).with_name("onerom_usb_diagnostics.log")
        self.live_track = "--.-"
        self.live_density: int | None = None
        self.live_protected: bool | None = None
        self.live_writing: bool | None = None
        self._writing_display_until = 0.0
        self.firmware_rpm: float | None = None
        self.live_sector: int | None = None
        self.live_sync_count: int | None = None
        self.qualified_sync_count: int | None = None
        self._sync_candidate_counts: list[int] = []
        self.last_stable_rpm: float | None = None
        self.rpm_samples: list[float] = []
        self.recent_sectors: list[int] = []
        self._fifo_track: int | None = None
        self.last_header_track: int | None = None
        self.write_pulse_count = 0
        self.phase_event_count = 0
        self.activity_history: list[str] = []
        self.capture_count: int | None = None
        self.ring_overrun: int | None = None
        self.queue_overflow: int | None = None
        self.capture_rate: float | None = None
        self._capture_rate_count: int | None = None
        self._capture_rate_time: float | None = None
        self.header_timestamps: list[float] = []
        self.header_valid_count = 0
        self.header_invalid_count = 0
        # The firmware counters are lifetime counters.  These baselines make
        # the Health card a resettable diagnostic window without resetting or
        # disturbing the passive capture firmware.
        self.health_ring_overrun_baseline = 0
        self.health_queue_overflow_baseline = 0
        self.disk_id_votes: dict[tuple[int, int], int] = {}
        self.confirmed_disk_id: tuple[int, int] | None = None
        self.disk_id_mismatch = False
        self.header_checksum_valid: bool | None = None
        self.disk_id_reverify_pending = False
        self._hud_dirty = False
        self._confirmed_writable = False
        # Per-card Control OneROM command feedback. This distinguishes a command
        # being queued from an explicit reply received over the CDC link.
        self.controller_card_feedback: dict[str, str] = {}
        self._pending_controller_card: str | None = None
        self._controller_transactions: dict[str, str] = {}
        self.controller_wp_available: bool | None = None
        self._pending_wp_override: bool | None = None
        self._wp_pending_state: bool | None = None
        self._wp_after_id: str | None = None
        self._reconnect_after: dict[str, str] = {}
        self._reconnect_delay_ms = {"controller": 1000, "hud": 1000}
        self._next_usb_presence_check = 0.0
        self._release_pending_role: str | None = None
        self.usb_log_lines: list[str] = []
        self.log_scroll = 0
        self.log_return_page = "settings"
        # The passive Monitor is an expandable card list. These operational
        # measurements are the default first view; everything else follows
        # as an unpinned diagnostic card.
        self.hud_scroll_index = 0
        self.hud_help_card: str | None = None
        self.priority_prompt_card: str | None = None
        self.priority_prompt_value = ""
        self._hud_subscription_mask: int | None = None
        self._hud_telemetry_enabled = False
        self._hud_redraw_after: str | None = None
        self._preview_redraw_after: str | None = None
        self.hud_card_priorities: dict[str, int | None] = dict(DEFAULT_HUD_CARD_PRIORITIES)
        self.load_saved_hud_priorities()
        self.save_hud_priorities()
        self.save_control_features()

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 11))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 22, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 11))
        style.configure("Metric.TLabel", background=PANEL, foreground=TEXT, font=("Consolas", 27, "bold"))
        style.configure("MetricSmall.TLabel", background=PANEL, foreground=TEXT, font=("Consolas", 18, "bold"))
        style.configure("Section.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 11, "bold"), padding=(16, 10))
        style.configure("Nav.TButton", font=("Segoe UI", 11, "bold"), padding=(16, 10))

        self.header = ttk.Frame(self, padding=(22, 12))
        self.header.pack(fill="x")
        ttk.Label(self.header, text="1541 OneROM", style="Title.TLabel").pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(self.header, textvariable=self.status_var, style="Sub.TLabel").pack(side="right", padx=(0, 12))
        ttk.Button(self.header, text="⚙ SETTINGS", style="Nav.TButton", command=lambda: self.show_page("settings")).pack(side="right")

        self.body = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.body.pack(fill="both", expand=True)
        self.pages: dict[str, ttk.Frame] = {}
        self.build_hud()
        self.build_settings()
        self.build_connection_setup("controller")
        self.build_connection_setup("hud")
        self.build_idle()
        self.refresh_usb_boards()
        self.nav = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.nav.pack(fill="x")
        self.hud_button = ttk.Button(self.nav, text="MONITOR", style="Nav.TButton", command=lambda: self.show_page("hud"))
        self.hud_button.pack(side="left")
        ttk.Label(self.nav, text="Touchscreen simulator — USB serial binding ready", style="Sub.TLabel").pack(side="right", pady=12)

        self.bind_all("<Button>", self.register_input, add=True)
        self.bind_all("<Key>", self.register_input, add=True)
        self.apply_device_state(initial=True)
        self.tick()
        self.after(20, self.poll_serial_loop)
        self.after(250, self.restore_saved_role_connections)
        # Start in the calibrated 7-inch interface; Windows is only the host
        # during current bench testing.
        self.after_idle(self.launch_physical_preview)

    def page(self, name: str) -> ttk.Frame:
        frame = ttk.Frame(self.body)
        self.pages[name] = frame
        return frame

    def card(self, parent, title: str, value: str, small=False):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        ttk.Label(frame, text=title.upper(), style="Section.TLabel").pack(anchor="w")
        var = tk.StringVar(value=value)
        ttk.Label(frame, textvariable=var, style="MetricSmall.TLabel" if small else "Metric.TLabel").pack(anchor="w", pady=(6, 0))
        return frame, var

    def build_hud(self) -> None:
        page = self.page("hud")
        ttk.Label(page, text="Monitor", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Live 1541 mechanical telemetry — selected Monitor OneROM", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        grid = ttk.Frame(page); grid.pack(fill="both", expand=True)
        grid.columnconfigure((0, 1, 2), weight=1); grid.rowconfigure((0, 1), weight=1)
        self.track_card, self.track_var = self.card(grid, "Track", "18.0")
        self.motor_card, self.motor_var = self.card(grid, "Motor", "ON")
        self.rpm_card, self.rpm_var = self.card(grid, "RPM", "300.7")
        self.head_card, self.head_var = self.card(grid, "Head", "PARK")
        self.sector_card, self.sector_var = self.card(grid, "Sector", "06")
        self.wp_card, self.wp_var = self.card(grid, "Write Protect", "PROTECTED", small=True)
        for index, card in enumerate((self.track_card, self.motor_card, self.rpm_card, self.head_card, self.sector_card, self.wp_card)):
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=7, pady=7)
        footer = ttk.Frame(page, style="Panel.TFrame", padding=14); footer.pack(fill="x", pady=(12, 0))
        self.hud_connection = tk.StringVar()
        ttk.Label(footer, textvariable=self.hud_connection, style="Panel.TLabel").pack(side="left")
        ttk.Button(footer, text="MONITOR ONEROM CONNECTION SETUP", command=lambda: self.show_connection_setup("hud")).pack(side="right")

    def build_settings(self) -> None:
        page = self.page("settings")
        ttk.Label(page, text="Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Prototype communications and startup behavior", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        devices = ttk.Frame(page); devices.pack(fill="x")
        devices.columnconfigure((0, 1), weight=1)
        controller_card = ttk.Frame(devices, style="Panel.TFrame", padding=18)
        controller_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ttk.Label(controller_card, text="CONTROL ONEROM", style="Section.TLabel").pack(anchor="w")
        ttk.Label(controller_card, text="Choose the OneROM that controls ROM, IEC, and write-protect.", style="Panel.TLabel", wraplength=500).pack(anchor="w", pady=(8, 12))
        ttk.Button(controller_card, text="CONTROL ONEROM CONNECTION SETUP", command=lambda: self.show_connection_setup("controller")).pack(anchor="e")
        hud_card = ttk.Frame(devices, style="Panel.TFrame", padding=18)
        hud_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ttk.Label(hud_card, text="MONITOR ONEROM", style="Section.TLabel").pack(anchor="w")
        ttk.Label(hud_card, text="Choose the OneROM that supplies passive drive telemetry.", style="Panel.TLabel", wraplength=500).pack(anchor="w", pady=(8, 12))
        ttk.Button(hud_card, text="MONITOR ONEROM CONNECTION SETUP", command=lambda: self.show_connection_setup("hud")).pack(anchor="e")
        self.device_summary = tk.StringVar()
        ttk.Label(page, textvariable=self.device_summary, style="Sub.TLabel", wraplength=1100).pack(anchor="w", pady=(14, 0))
        startup = ttk.Frame(page, style="Panel.TFrame", padding=18); startup.pack(fill="x", pady=(14, 0))
        ttk.Label(startup, text="STARTUP AND IDLE BEHAVIOR", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(startup, text="Preferred startup screen:", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(startup, textvariable=self.startup, values=("Automatic", "Monitor"), state="readonly", width=22, font=("Segoe UI", 11)).grid(row=1, column=1, sticky="w", padx=12, pady=(12, 0))
        ttk.Button(startup, text="Apply", command=self.apply_startup).grid(row=1, column=2, padx=8, pady=(12, 0))
        ttk.Label(startup, text="Idle before Wilson screen saver (seconds):", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(startup, from_=10, to=3600, textvariable=self.idle_seconds, width=9, font=("Segoe UI", 11)).grid(row=2, column=1, sticky="w", padx=12, pady=(12, 0))
        ttk.Button(startup, text="PREVIEW IDLE SCREEN", command=self.show_idle).grid(row=2, column=2, padx=8, pady=(12, 0))
        ttk.Label(startup, text="Monitor PPI (V226HQL is 102.4):", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Spinbox(startup, from_=70, to=240, increment=0.1, textvariable=self.preview_ppi, width=9, font=("Segoe UI", 11)).grid(row=3, column=1, sticky="w", padx=12, pady=(16, 0))
        ttk.Button(startup, text="OPEN / APPLY 7-INCH PREVIEW", command=self.open_size_preview).grid(row=4, column=2, padx=8, pady=(8, 0))
        ttk.Button(page, text="← RETURN", style="Nav.TButton", command=lambda: self.show_page(self.default_page())).pack(anchor="w", pady=16)

    def build_connection_setup(self, role: str) -> None:
        """Build one dedicated setup page per physical OneROM role."""
        is_controller = role == "controller"
        page = self.page(f"{role}_connection")
        role_name = "Control OneROM" if is_controller else "Monitor OneROM"
        serial_var = self.controller_serial if is_controller else self.hud_serial
        ttk.Label(page, text=f"{role_name} Connection Setup", style="Title.TLabel").pack(anchor="w")
        description = (
            "Assign the OneROM that performs ROM, IEC address, and write-protect control for this drive. "
            "Either physical OneROM can fill this role."
            if is_controller else
            "Assign the OneROM that passively reads drive telemetry for this drive. "
            "Either physical OneROM can fill this role."
        )
        ttk.Label(page, text=description, style="Sub.TLabel", wraplength=1080).pack(anchor="w", pady=(0, 16))
        card = ttk.Frame(page, style="Panel.TFrame", padding=22); card.pack(fill="x")
        ttk.Label(card, text=f"{role_name.upper()} ONE ROM", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(card, text="USB SERIAL NUMBER", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 0))
        serial_box = ttk.Combobox(card, textvariable=serial_var, state="readonly", width=44, font=("Segoe UI", 12))
        serial_box.grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(16, 0))
        if is_controller:
            self.controller_serial_box = serial_box
        else:
            self.hud_serial_box = serial_box
        ttk.Button(card, text="REFRESH USB DEVICES", command=self.refresh_usb_boards).grid(row=2, column=1, sticky="w", padx=(18, 0), pady=(16, 0))
        ttk.Label(card, textvariable=self.usb_status, style="Panel.TLabel", wraplength=1050).grid(row=3, column=0, columnspan=2, sticky="w", pady=(18, 0))
        ttk.Button(page, text=f"SAVE {role_name.upper()} CONNECTION", command=self.connect_assigned_boards).pack(anchor="e", pady=(16, 0))
        ttk.Button(page, text="← BACK TO SETTINGS", style="Nav.TButton", command=lambda: self.show_page("settings")).pack(anchor="w", pady=16)

    def show_connection_setup(self, role: str) -> None:
        self.refresh_usb_boards()
        self.show_page(f"{role}_connection")

    def open_desktop_connection_setup(self, role: str) -> None:
        """Open the selected role's setup page on the calibrated 7-inch UI."""
        self.refresh_usb_boards()
        self.preview_page = f"{role}_connection"
        self.open_size_preview()

    def build_idle(self) -> None:
        page = self.page("idle")
        page.configure(style="Panel.TFrame")
        content = ttk.Frame(page, style="Panel.TFrame", padding=40); content.place(relx=.5, rely=.5, anchor="center")
        ttk.Label(content, text="WILSON REBORN", style="MetricSmall.TLabel").pack()
        ttk.Label(content, text="Johnny Castaway would run fullscreen here on the Pi.", style="Panel.TLabel").pack(pady=(14, 4))
        ttk.Label(content, text="Touch anywhere to return to the drive display.", style="Panel.TLabel").pack()

    def show_page(self, name: str) -> None:
        # The dashboard is the single operational surface. It can show Monitor
        # telemetry, Control OneROM cards, or both; no separate Control screen.
        if name == "control":
            name = "hud" if (self.ub3.get() or self.ub4.get()) else "settings"
        if name == "hud" and not (self.ub3.get() or self.ub4.get()):
            name = "settings"
        for frame in self.pages.values(): frame.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current_page = name
        self.screensaver = name == "idle"
        self.nav.pack_forget() if self.screensaver else self.nav.pack(fill="x")
        self.register_input()

    def default_page(self) -> str:
        if self.startup.get() in ("DriveHUD", "Monitor") and self.ub4.get(): return "hud"
        if self.ub4.get(): return "hud"
        if self.ub3.get(): return "hud"
        return "settings"

    def refresh_usb_boards(self) -> None:
        """Discover CDC boards and present their stable USB serials for binding."""
        boards = discover_cdc_boards()
        self.usb_boards = {board.serial_number: board for board in boards}
        serials = [""] + [board.serial_number for board in boards]
        self.controller_serial_box["values"] = serials
        self.hud_serial_box["values"] = serials
        if not boards:
            self.usb_status.set("No OneROM CDC board with a USB serial is connected. Connect a OneROM, then refresh this list.")
            self.append_usb_log("SYSTEM", self.usb_status.get())
            return
        details = "; ".join(f"{board.serial_number} on {board.port}" for board in boards)
        self.usb_status.set(f"Found {len(boards)} board(s): {details}")
        self.append_usb_log("SYSTEM", self.usb_status.get())

    def append_usb_log(self, role: str, message: str) -> None:
        """Keep a bounded communications history with millisecond timing."""
        now = time.time()
        timestamp = f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"
        self.usb_log_lines.append(f"{timestamp} [{role}] {message}")
        del self.usb_log_lines[:-500]

    def open_usb_log(self) -> None:
        self.log_return_page = self.preview_page
        self.preview_page = "log"
        self.log_scroll = 0
        self.open_size_preview()

    def copy_usb_log(self) -> None:
        payload = "\n".join(self.usb_log_lines) or "No USB communications recorded."
        self.clipboard_clear()
        self.clipboard_append(payload)
        self.update()
        self.usb_status.set("USB log copied to the Windows clipboard.")
        self.append_usb_log("SYSTEM", self.usb_status.get())
        self.open_size_preview()

    def clear_usb_log(self) -> None:
        """Clear the visible USB log without adding a replacement entry."""
        self.usb_log_lines.clear()
        self.log_scroll = 0
        self.usb_status.set("USB log cleared.")
        self.open_size_preview()

    def connect_assigned_boards(self, selected_role: str | None = None) -> None:
        """Persist serial-to-drive role bindings and open their CDC telemetry links."""
        binding = DriveBinding(
            drive_id="1541 Drive",
            controller_serial=self.controller_serial.get().strip(),
            hud_serial=self.hud_serial.get().strip(),
            appearance=dict(self.drive_binding.appearance),
            priorities=dict(self.drive_binding.priorities),
            control_features=dict(self.drive_binding.control_features),
            window_size=dict(self.drive_binding.window_size),
        )
        try:
            if selected_role == "controller" and binding.controller_serial and binding.controller_serial == binding.hud_serial:
                raise ValueError("This OneROM is currently bound as Monitor OneROM. Release the Monitor binding before assigning it as Control OneROM.")
            if selected_role == "hud" and binding.hud_serial and binding.hud_serial == binding.controller_serial:
                raise ValueError("This OneROM is currently bound as Control OneROM. Release the Control binding before assigning it as Monitor OneROM.")
            binding.validate()
            if not self.usb_boards:
                self.refresh_usb_boards()
            roles = (selected_role,) if selected_role else ("controller", "hud")
            for role in roles:
                serial_number = binding.controller_serial if role == "controller" else binding.hud_serial
                if not serial_number:
                    raise ValueError(f"Select a {'Control OneROM' if role == 'controller' else 'Monitor OneROM'} serial first.")
                board = self.usb_boards.get(serial_number)
                if board is None:
                    raise ValueError(f"{'Control OneROM' if role == 'controller' else 'Monitor OneROM'} serial {serial_number} is not currently connected.")
                existing = self.usb_links.get(role)
                if existing is not None and existing.board.serial_number == serial_number and existing.connected:
                    continue
                if existing is not None:
                    existing.close()
                if role == "hud":
                    self.reset_monitor_state()
                link = CdcBoardLink(board)
                link.open()
                self.usb_links[role] = link
                self.after(150, link.assert_dtr)
                if role == "controller":
                    self.after(300, self.request_controller_wp_state)
                else:
                    # Current HUD firmware starts with its optional telemetry
                    # mask clear. STATUS continues in that mode, but the
                    # MOTOR, PHASE, and TRACK_WRITE records required by the
                    # Head card are suppressed. Enable passive event output
                    # once after the DTR attach edge; this sends no command
                    # to the 1541 or its bus.
                    self.after(300, lambda current_link=link: self.enable_hud_telemetry(current_link))
            self.drive_binding = binding
            save_binding(self.binding_path, binding)
            self.ub3.set("controller" in self.usb_links)
            self.ub4.set("hud" in self.usb_links)
            controller_state = "connected" if "controller" in self.usb_links else "saved / offline"
            hud_state = "connected" if "hud" in self.usb_links else "saved / offline"
            selected_name = "Control OneROM" if selected_role == "controller" else "Monitor OneROM" if selected_role == "hud" else "Roles"
            self.usb_status.set(f"{selected_name} connected — Control OneROM: {controller_state}; Monitor OneROM: {hud_state}.")
            self.append_usb_log("SYSTEM", self.usb_status.get())
            self.apply_device_state()
        except Exception as exc:
            self.usb_status.set(f"USB connection failed: {exc}")
            self.append_usb_log("SYSTEM", self.usb_status.get())

    def enable_hud_telemetry(self, link: CdcBoardLink) -> None:
        """Enable every passive firmware telemetry class after Monitor attach."""
        if self.usb_links.get("hud") is not link or not link.connected:
            return
        try:
            # Start with exactly the classes the active viewport needs. The
            # default Monitor page needs CORE, RPM, and SYNC; later scrolling
            # or reprioritizing adds headers/metadata only when required.
            self._hud_subscription_mask = None
            self.update_hud_subscription(link)
            self._hud_telemetry_enabled = True
            self.append_usb_log("SYSTEM", f"Monitor passive telemetry enabled (M={self._hud_subscription_mask}).")
        except Exception as exc:
            self.append_usb_log("SYSTEM", f"Monitor telemetry setup failed: {exc}")

    def request_release_role_binding(self, role: str) -> None:
        """Paint a release notice before Windows starts its slow CDC teardown."""
        if self._release_pending_role is not None:
            return
        self._release_pending_role = role
        role_name = "Control OneROM" if role == "controller" else "Monitor OneROM"
        self.usb_status.set(f"Releasing {role_name}… Windows may take a few seconds to close its USB port.")
        self.append_usb_log("SYSTEM", f"{role_name} release notice displayed; waiting briefly to paint it.")
        self.open_size_preview()
        self.after(80, lambda current=role: self.finish_requested_release(current))

    def finish_requested_release(self, role: str) -> None:
        try:
            self.release_role_binding(role)
        finally:
            self._release_pending_role = None
            self.open_size_preview()

    def release_role_binding(self, role: str) -> None:
        """Release one persisted role without touching the other OneROM."""
        if role not in ("controller", "hud"):
            raise ValueError(f"Unknown OneROM role: {role}")
        started = time.perf_counter()
        role_name = "Control OneROM" if role == "controller" else "Monitor OneROM"
        self.append_usb_log("SYSTEM", f"{role_name} release requested.")
        link = self.usb_links.pop(role, None)
        if link is not None:
            link.close()
        close_elapsed_ms = (time.perf_counter() - started) * 1000
        close_detail = (
            f"DTR/read cancel {link.last_close_dtr_ms:.0f} ms; handle close {link.last_close_handle_ms:.0f} ms"
            if link is not None else "no active CDC link"
        )
        self.append_usb_log("SYSTEM", f"{role_name} CDC close completed in {close_elapsed_ms:.0f} ms ({close_detail}).")
        if role == "hud":
            self.reset_monitor_state()
        if role == "controller":
            self.controller_serial.set("")
        else:
            self.hud_serial.set("")
        self.drive_binding = DriveBinding(
            drive_id="1541 Drive",
            controller_serial=self.controller_serial.get().strip(),
            hud_serial=self.hud_serial.get().strip(),
            appearance=dict(self.drive_binding.appearance),
            priorities=dict(self.drive_binding.priorities),
            control_features=dict(self.drive_binding.control_features),
            window_size=dict(self.drive_binding.window_size),
        )
        save_started = time.perf_counter()
        save_binding(self.binding_path, self.drive_binding)
        save_elapsed_ms = (time.perf_counter() - save_started) * 1000
        self.append_usb_log("SYSTEM", f"{role_name} JSON save completed in {save_elapsed_ms:.0f} ms.")
        self.ub3.set("controller" in self.usb_links)
        self.ub4.set("hud" in self.usb_links)
        total_elapsed_ms = (time.perf_counter() - started) * 1000
        self.usb_status.set(f"{role_name} binding released in {total_elapsed_ms:.0f} ms. The other OneROM role was left unchanged.")
        self.append_usb_log("SYSTEM", self.usb_status.get())
        self.apply_device_state()

    def reset_monitor_state(self) -> None:
        """Reset transient Monitor readings without discarding a tracked head."""
        # A CDC reconnect does not move the stepper.  Retain a position already
        # established from phase traffic, then let the next home/$0022/header
        # observation correct it.  Throwing away a good mechanical count on
        # every USB hiccup was needlessly making the HUD forgetful.
        prior_state = getattr(self, "telemetry_parser", None)
        prior_position = (
            prior_state.state.position_half_tracks
            if prior_state is not None else None
        )
        self.telemetry_parser = DriveTelemetryParser()
        if prior_position is not None:
            self.telemetry_parser.state.position_half_tracks = prior_position
            self.telemetry_parser.state.position_source = "CARRIED EST."
        self.live_track = self.telemetry_parser.state.track
        self.live_density = None
        self.live_protected = None
        self.live_writing = None
        self._writing_display_until = 0.0
        self.firmware_rpm = None
        self.live_sector = None
        self.live_sync_count = None
        self.qualified_sync_count = None
        self._sync_candidate_counts.clear()
        self.motor = False
        self.head_direction = "PARK"
        self.last_stable_rpm = None
        self.rpm_samples.clear()
        self.recent_sectors.clear()
        self._fifo_track = None
        self.last_header_track = None
        self.write_pulse_count = 0
        self.phase_event_count = 0
        self.activity_history.clear()
        self.capture_count = None
        self.ring_overrun = None
        self.queue_overflow = None
        self.capture_rate = None
        self._capture_rate_count = None
        self._capture_rate_time = None
        self.header_timestamps.clear()
        self.header_valid_count = 0
        self.header_invalid_count = 0
        self.health_ring_overrun_baseline = 0
        self.health_queue_overflow_baseline = 0
        self.clear_disk_identity()
        self._hud_subscription_mask = None
        self._hud_telemetry_enabled = False
        for name, value in (
            ("track_var", self.live_track), ("motor_var", "OFF"),
            ("rpm_var", "--.--"), ("sector_var", "--"), ("head_var", "PARK"),
        ):
            variable = getattr(self, name, None)
            if variable is not None:
                variable.set(value)

    def poll_usb_telemetry(self) -> None:
        """Apply passive Monitor CDC telemetry without emitting control commands."""
        link = self.usb_links.get("hud")
        if link is None:
            return
        try:
            changed = False
            motor_visual_changed = False
            # Keep the UI responsive even if firmware emits a burst of
            # diagnostics. Preserve every state-changing record before
            # trimming high-rate headers: a seek can otherwise occur just
            # before the newest 96 records and lose its IN/OUT evidence.
            received_lines = link.read_lines()
            # Process every complete CDC record. The visible history is
            # intentionally throttled, never the measurement stream.
            first_log_index = max(0, len(received_lines) - 96)
            for index, line in enumerate(received_lines):
                if index >= first_log_index:
                    self.append_usb_log("MONITOR", line)
                state = self.telemetry_parser.process(line)
                changed = True
                self.live_track = state.track
                self.track_var.set(state.track)
                if state.motor is not None:
                    motor_changed = state.motor != self.motor
                    self.motor = state.motor
                    self.motor_var.set("ON" if state.motor else "OFF")
                    if motor_changed:
                        motor_visual_changed = True
                        self.record_activity("MOTOR ON" if state.motor else "MOTOR OFF")
                        self.reset_sync_qualification()
                        if state.motor:
                            self.begin_disk_identity_verification()
                            self.header_timestamps.clear()
                            self.header_valid_count = 0
                            self.header_invalid_count = 0
                if state.density is not None:
                    self.live_density = state.density
                    # Density is rendered directly on the fixed 7-inch HUD;
                    # the desktop card layout has no matching Tk variable.
                    if hasattr(self, "density_var"):
                        self.density_var.set(f"D{state.density}")
                if state.rpm is not None:
                    self.firmware_rpm = state.rpm
                    self.rpm_var.set(f"{state.rpm:.2f}")
                # The parser retains its last sector between CDC records.
                # Promote it to the HUD only when this specific record is a
                # newly decoded physical header; otherwise it can be stale
                # after a seek, homing pass, or motor stop.
                if line.startswith("HDRPHY ") and state.sector is not None and self.motor:
                    self.live_sector = state.sector
                    self.sector_var.set(f"{state.sector:02d}")
                if line.startswith("SYNC ") and state.sync_count is not None:
                    self.live_sync_count = state.sync_count
                    sample = self.observe_sync_sample(state.sync_count)
                    if sample is not None:
                        self.rpm_samples.append(sample)
                        del self.rpm_samples[:-5]
                if state.capture_count is not None:
                    self.capture_count = state.capture_count
                    self.ring_overrun = state.ring_overrun
                    self.queue_overflow = state.queue_overflow
                    if line.startswith("STATUS "):
                        self.observe_capture_rate(state.capture_count)
                if line.startswith("HDRMETA "):
                    # A running copy of the desktop can retain an older
                    # telemetry parser during an in-place update.  Metadata
                    # is optional diagnostic enrichment; it must never tear
                    # down the CDC link if those newly added fields are not
                    # present yet.
                    self.observe_header_metadata(
                        getattr(state, "header_id1", None),
                        getattr(state, "header_id2", None),
                        getattr(state, "header_checksum_valid", None),
                    )
                if state.motor is False:
                    self.last_stable_rpm = None
                    self._writing_display_until = 0.0
                    self.rpm_samples.clear()
                    self.header_timestamps.clear()
                    self.clear_current_header()
                    self.clear_recent_sectors()
                elif line.startswith("PHASE "):
                    # A physical head step makes earlier sectors unrelated to
                    # the current position, just as in the proven HUD GUI.
                    self.phase_event_count += 1
                    self.record_activity(f"SEEK {state.head}")
                    self.reset_sync_qualification()
                    self.clear_current_header()
                    self.clear_recent_sectors()
                elif line.startswith("HDRPHY ") and state.header_track is not None and state.sector is not None:
                    self.last_header_track = state.header_track
                    self.observe_header_rate()
                    self.push_recent_sector(state.header_track, state.sector)
                    self.record_activity(f"READ T{state.header_track:02d} S{state.sector:02d}")
                # ``state.protected`` is deliberately retained by the parser
                # between records.  Debouncing that retained value on every
                # header/SYNC record kept cancelling the 250 ms timer during
                # active reads, so a real disk-notch change never reached the
                # display.  Only a fresh physical WP record (or the initial
                # authoritative STATE snapshot) may start the debounce.
                if state.protected is not None and (
                    line.startswith("WRITE_PROTECT ")
                    or ("WPV=" in line and " WP=" in line)
                ):
                    self.schedule_write_protect_update(state.protected)
                if state.writing is not None:
                    self.live_writing = state.writing
                    if line.startswith("WRITE_GATE ") and state.writing:
                        self.write_pulse_count += 1
                        self.record_activity("WRITE GATE")
                        # The physical CB2 write-gate pulse can be shorter
                        # than one CDC receive batch. Hold it briefly so the
                        # operator can actually see a confirmed write.
                        self._writing_display_until = time.monotonic() + 1.0
                        self.after(1010, self.update_live_hud_fields)
                if state.head in ("IN", "OUT", "STALL", "PARK"):
                    self.head_var.set(state.head)
                    if state.head in ("IN", "OUT"):
                        self.head_direction = state.head
            # A stalled head must update even when the CDC stream is quiet.
            # The parser records the last target/phase event independently
            # from incoming status records, so refresh it every 20 ms poll.
            previous_head = self.head_var.get()
            state = self.telemetry_parser.refresh()
            if state.head != previous_head:
                self.head_var.set(state.head)
                changed = True
            # Never rebuild the entire Canvas from the CDC receive loop.
            # Only existing live HUD items are updated in place.
            displayed_rpm = self.effective_rpm()
            if displayed_rpm is not None:
                self.rpm_var.set(f"{displayed_rpm:.2f}")
            self._hud_dirty = self._hud_dirty or changed
            if changed:
                self.serial_last_error.set("")
                self.update_live_hud_fields()
                # The platter switches between stationary and animated modes
                # only on a motor transition, never on routine telemetry.
                if motor_visual_changed and self.preview_page == "hud":
                    self.open_size_preview()
        except Exception as exc:
            self._hud_subscription_mask = None
            self.reset_monitor_state()
            self.record_serial_diagnostic("Monitor", link, exc)
            # A removed Windows CDC handle may block in close().  Clear and
            # paint the role first; otherwise this exception path leaves the
            # visible dashboard claiming that Monitor is still connected.
            self.usb_links.pop("hud", None)
            self.ub4.set(False)
            self.usb_status.set(f"Monitor link interrupted; reconnecting automatically: {exc}")
            self.append_usb_log("MONITOR", self.usb_status.get())
            self.apply_device_state()
            self.request_preview_redraw()
            self.after(80, lambda stale_link=link: self.finish_unplugged_role("hud", stale_link))

    def poll_controller_feedback(self) -> None:
        """Drain Control OneROM CDC output so its USB log/reply FIFO cannot back up."""
        link = self.usb_links.get("controller")
        if link is None:
            return
        try:
            for line in link.read_lines()[-96:]:
                self.append_usb_log("CONTROL", line)
                # Selector replies are the only controller lines surfaced to
                # the operator. Other CDC log lines are still drained.
                if line.startswith("$ROMTEST,"):
                    if line.startswith("$ROMTEST,WP,"):
                        self.handle_controller_wp_reply(line)
                    elif line.startswith("$ROMTEST,SAVED,"):
                        self.handle_controller_settings_refresh_reply(line)
                    elif ",OK," in line or line.endswith(",OK"):
                        if not self.handle_controller_setting_reply(line):
                            self.control_status.set(f"Control OneROM verified: {line}")
                    elif ",FAIL" in line or ",ERROR," in line:
                        if not self.handle_controller_setting_reply(line):
                            self.control_status.set(f"Control OneROM rejected request: {line}")
        except Exception as exc:
            self.record_serial_diagnostic("Control OneROM", link, exc)
            # See the Monitor failure path above: close() can block after a
            # USB pull, so state and Canvas redraw must happen before it.
            self.usb_links.pop("controller", None)
            self.ub3.set(False)
            self.usb_status.set(f"Control OneROM serial link interrupted; reconnecting automatically: {exc}")
            self.append_usb_log("CONTROL", self.usb_status.get())
            self.apply_device_state()
            self.request_preview_redraw()
            self.after(80, lambda stale_link=link: self.finish_unplugged_role("controller", stale_link))

    def schedule_role_reconnect(self, role: str) -> None:
        """Retry one role with backoff after a transient Windows CDC failure."""
        if role in self._reconnect_after:
            return
        delay = self._reconnect_delay_ms[role]
        self._reconnect_after[role] = self.after(delay, lambda current=role: self.attempt_role_reconnect(current))
        self._reconnect_delay_ms[role] = min(delay * 2, 8000)

    def check_connected_board_presence(self) -> None:
        """Detect USB removal even when Windows leaves a CDC handle open."""
        boards = discover_cdc_boards()
        self.usb_boards = {board.serial_number: board for board in boards}
        present_serials = set(self.usb_boards)
        for role, link in tuple(self.usb_links.items()):
            if link.board.serial_number in present_serials:
                continue
            # Remove the role from live state before close(). On Windows a
            # vanished CDC handle can take several seconds to close, but the
            # dashboard must stop claiming that its hardware is present now.
            self.usb_links.pop(role, None)
            role_name = "Control OneROM" if role == "controller" else "Monitor OneROM"
            if role == "controller":
                self.ub3.set(False)
                self.writable.set(False)
            else:
                self.ub4.set(False)
                self.reset_monitor_state()
            self.usb_status.set(f"{role_name} unplugged; waiting for its saved USB serial to reappear.")
            self.append_usb_log("SYSTEM", self.usb_status.get())
            self.apply_device_state()
            self.request_preview_redraw()
            self.after(80, lambda current=role, stale_link=link: self.finish_unplugged_role(current, stale_link))

    def request_preview_redraw(self) -> None:
        """Rebuild the visible Canvas after the current USB callback returns."""
        if self._preview_redraw_after is not None:
            return

        def redraw() -> None:
            self._preview_redraw_after = None
            preview = getattr(self, "preview", None)
            if preview is not None and preview.winfo_exists():
                self.open_size_preview()

        # Rebuilding/destroying Canvas items inside the CDC callback itself
        # can leave the existing surface on screen until another input event.
        # Tk's idle turn is the correct hand-off from transport state to UI.
        self._preview_redraw_after = self.after_idle(redraw)

    def finish_unplugged_role(self, role: str, link: CdcBoardLink) -> None:
        """Close a removed handle, then confirm the offline layout is painted."""
        link.close()
        # Closing a vanished Windows CDC handle can take several seconds. The
        # role state was cleared before that wait, but repaint once more after
        # teardown so the active Canvas cannot retain an old card layout.
        self.request_preview_redraw()
        self.schedule_role_reconnect(role)

    def restore_saved_role_connections(self) -> None:
        """Immediately reconnect every saved role after program startup."""
        for index, (role, serial_number) in enumerate((
            ("controller", self.drive_binding.controller_serial),
            ("hud", self.drive_binding.hud_serial),
        )):
            if serial_number:
                self.append_usb_log("SYSTEM", f"Startup reconnecting {role.title()} {serial_number}.")
                # Do not make the operator open a setup screen or wait for
                # the normal failure-backoff interval.  Stagger the two CDC
                # opens slightly so Windows can settle each COM port.
                self.after(index * 150, lambda current=role: self.attempt_role_reconnect(current))

    def attempt_role_reconnect(self, role: str) -> None:
        self._reconnect_after.pop(role, None)
        serial_number = self.drive_binding.controller_serial if role == "controller" else self.drive_binding.hud_serial
        if not serial_number or role in self.usb_links:
            return
        try:
            boards = discover_cdc_boards()
            self.usb_boards = {board.serial_number: board for board in boards}
            board = self.usb_boards.get(serial_number)
            if board is None:
                self.usb_status.set(f"Waiting for {role.title()} serial {serial_number} to reappear.")
                self.schedule_role_reconnect(role)
                return
            link = CdcBoardLink(board)
            if role == "hud":
                self.reset_monitor_state()
            link.open()
            self.usb_links[role] = link
            self.after(150, link.assert_dtr)
            if role == "controller":
                self.after(300, self.request_controller_wp_state)
            else:
                # Startup/reconnect takes this path rather than the Settings
                # save path, so it needs the same one-time passive telemetry
                # subscription.
                self.after(300, lambda current_link=link: self.enable_hud_telemetry(current_link))
            self._reconnect_delay_ms[role] = 1000
            if role == "controller":
                self.ub3.set(True)
            else:
                self.ub4.set(True)
            self.serial_last_error.set("")
            self.usb_status.set(f"{role.title()} {serial_number} automatically reconnected.")
            self.append_usb_log("SYSTEM", self.usb_status.get())
            self.apply_device_state()
            # Settings is a Canvas screen, so its connection cards do not
            # automatically reflect changed Tk variables.  Repaint it once
            # after a successful background reconnect.
            preview = getattr(self, "preview", None)
            if preview is not None and preview.winfo_exists():
                self.open_size_preview()
        except Exception as exc:
            self.usb_status.set(f"{role.title()} reconnect retry failed: {exc}")
            self.schedule_role_reconnect(role)

    def record_serial_diagnostic(self, role: str, link: CdcBoardLink, exc: Exception) -> None:
        """Preserve the raw Windows/pyserial failure for bench debugging."""
        message = f"{role} {link.board.serial_number} on {link.board.port}: {type(exc).__name__}: {exc}"
        self.serial_last_error.set(message)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.serial_diagnostic_path.open("a", encoding="utf-8") as log:
                log.write(f"{timestamp} {message}\n")
        except OSError:
            pass

    def clear_recent_sectors(self) -> None:
        """Clear the decoded-sector history after motor-off or a seek."""
        if not self.recent_sectors and self._fifo_track is None:
            return
        self.recent_sectors.clear()
        self._fifo_track = None

    def clear_current_header(self) -> None:
        """Forget a header after movement or motor-off makes it stale.

        A sector is physically confirmed only while its own header is being
        decoded.  Retaining it through a seek, homing pass, or parked motor
        would make the passive HUD claim a position it cannot guarantee.
        """
        self.live_sector = None
        self.last_header_track = None
        self.sector_var.set("--")

    def clear_disk_identity(self) -> None:
        """Discard all cached identity evidence when a link is reset."""
        self.disk_id_votes.clear()
        self.confirmed_disk_id = None
        self.disk_id_mismatch = False
        self.header_checksum_valid = None
        self.disk_id_reverify_pending = False

    def begin_disk_identity_verification(self) -> None:
        """Keep the displayed ID, but require fresh headers after motor-on."""
        self.disk_id_votes.clear()
        self.disk_id_mismatch = False
        self.header_checksum_valid = None
        self.disk_id_reverify_pending = True

    def observe_header_metadata(
        self, id1: int | None, id2: int | None, checksum_valid: bool | None
    ) -> None:
        """Accept an ID only after two checksum-valid physical headers agree."""
        self.header_checksum_valid = checksum_valid
        if checksum_valid is True:
            self.header_valid_count += 1
        elif checksum_valid is False:
            self.header_invalid_count += 1
        if id1 is None or id2 is None or not checksum_valid:
            return
        identity = (id1, id2)
        self.disk_id_votes[identity] = self.disk_id_votes.get(identity, 0) + 1
        if len(self.disk_id_votes) > 1:
            self.disk_id_mismatch = True
            self.confirmed_disk_id = None
        elif self.disk_id_votes[identity] >= 2:
            self.confirmed_disk_id = identity
            self.disk_id_reverify_pending = False

    def disk_identity_label(self) -> str:
        if self.disk_id_mismatch:
            return "ID CONFLICT"
        if self.confirmed_disk_id is not None:
            return f"ID {self.confirmed_disk_id[0]:02X} {self.confirmed_disk_id[1]:02X}"
        return "VERIFYING ID" if self.header_checksum_valid else "WAITING FOR ID"

    def disk_identity_detail(self) -> str:
        if self.disk_id_mismatch:
            return "VALID HEADERS CONFLICT"
        if self.disk_id_reverify_pending and self.confirmed_disk_id is not None:
            return "LAST CONFIRMED · REVERIFYING"
        if self.header_checksum_valid is False:
            return "HEADER CHECK FAILED"
        if self.confirmed_disk_id is not None:
            return "HEADER CHECK OK · TWO MATCHES" if self.motor else "LAST CONFIRMED · HEADER CHECK OK"
        if self.header_checksum_valid:
            return "HEADER CHECK OK · NEED ONE MORE"
        return "PHYSICAL HEADER METADATA PENDING"

    def record_activity(self, event: str) -> None:
        """Keep a compact, evidence-only activity history for Diagnostics."""
        if not self.activity_history or self.activity_history[-1] != event:
            self.activity_history.append(event)
            del self.activity_history[:-10]

    def diagnostic_history_lines(self) -> tuple[str, str]:
        """Return two evidence rows that fit the fixed Diagnostics card."""
        if not self.activity_history:
            return "WAITING FOR DRIVE ACTIVITY", ""
        compact: list[str] = []
        # Five compact entries fit per 816-pixel row while preserving a
        # right-hand margin.  A second row doubles useful recent evidence.
        for event in self.activity_history[-10:]:
            if event.startswith("READ "):
                compact.append(f"R {event[5:]}")
            elif event == "WRITE GATE":
                compact.append("WRITE")
            else:
                compact.append(event)
        return "  ·  ".join(compact[:5]), "  ·  ".join(compact[5:])

    def diagnostic_history_text(self) -> str:
        """Compatibility helper for callers that only need the newest row."""
        return self.diagnostic_history_lines()[1] or self.diagnostic_history_lines()[0]

    def push_recent_sector(self, track: int, sector: int) -> None:
        """Keep a full-width FIFO of recent physical header sectors."""
        if self._fifo_track is None:
            self._fifo_track = track
        elif track != self._fifo_track:
            self.recent_sectors.clear()
            self._fifo_track = track
        self.recent_sectors.append(sector)
        del self.recent_sectors[:-RECENT_SECTOR_FIFO_SIZE]

    def sector_fifo_lines(self) -> tuple[str, str]:
        """Return the current track's decoded sectors in chronological FIFO order."""
        if self._fifo_track is None or not self.recent_sectors:
            return "WAITING FOR PHYSICAL HEADERS", "FIFO RESETS AFTER SEEK OR MOTOR STOP"
        entries = [f"S{sector:02d}" for sector in self.recent_sectors]
        # Fifteen values fit on each compact HUD row with a deliberate right
        # margin before the Help and Priority controls.
        return " · ".join(entries[:15]), " · ".join(entries[15:])

    def reset_sync_qualification(self) -> None:
        """Discard samples that cannot describe one stable spindle state."""
        self.qualified_sync_count = None
        self._sync_candidate_counts.clear()
        self.last_stable_rpm = None
        self.rpm_samples.clear()

    def observe_sync_sample(self, count: int) -> float | None:
        """Accept only consecutive plausible one-second SYNC measurements."""
        expected = {3: 42, 2: 38, 1: 36, 0: 34}.get(self.live_density)
        if not self.motor or expected is None:
            self.reset_sync_qualification()
            return None
        rpm = count * 60.0 / expected
        if not 240.0 <= rpm <= 360.0:
            # A partial start/stop/seek window is not a measurement. Do not
            # turn it into a dramatic but meaningless dashboard flicker.
            self.qualified_sync_count = None
            self._sync_candidate_counts.clear()
            self.last_stable_rpm = None
            return None
        self._sync_candidate_counts.append(count)
        del self._sync_candidate_counts[:-2]
        if len(self._sync_candidate_counts) < 2:
            self.qualified_sync_count = None
            self.last_stable_rpm = None
            return None
        first, second = self._sync_candidate_counts
        if abs(second - first) > max(4, round(max(first, second) * 0.04)):
            self._sync_candidate_counts[:] = [second]
            self.qualified_sync_count = None
            self.last_stable_rpm = None
            return None
        self.qualified_sync_count = round((first + second) / 2)
        self.last_stable_rpm = self.qualified_sync_count * 60.0 / expected
        return self.last_stable_rpm

    def sync_revolution_reading(self) -> tuple[float | None, int | None, int | None]:
        """Return the measured ratio, its physical-count display, and zone target."""
        expected_by_density = {3: 42, 2: 38, 1: 36, 0: 34}
        expected = expected_by_density.get(self.live_density)
        if (
            not self.motor
            or self.qualified_sync_count is None
            or self.firmware_rpm is None
            or self.firmware_rpm <= 0
        ):
            return None, None, expected
        estimate = self.qualified_sync_count * 60 / self.firmware_rpm
        # Individual SYNC pulses are discrete.  The fraction belongs to the
        # one-second-window/RPM diagnostic, while the primary HUD value is
        # the nearest physical count per revolution.
        return estimate, round(estimate), expected

    def expected_sector_count(self) -> int | None:
        return {3: 21, 2: 19, 1: 18, 0: 17}.get(self.live_density)

    def header_offset(self) -> float | None:
        """Mechanical position minus the latest physical-header track."""
        position = self.telemetry_parser.state.position_half_tracks
        if position is None or self.last_header_track is None:
            return None
        return position / 2.0 - self.last_header_track

    def rpm_quality_detail(self) -> str:
        if not self.motor:
            return "MOTOR OFF"
        firmware = f" · FW {self.firmware_rpm:.2f}" if self.firmware_rpm is not None else ""
        if not self.rpm_samples:
            return f"MOTOR ON · SYNC ACQUIRING{firmware}"
        spread = max(self.rpm_samples) - min(self.rpm_samples)
        return f"MOTOR ON · SYNC ±{spread / 2:.2f}{firmware}"

    def capture_health_detail(self) -> tuple[str, str]:
        if self.capture_count is None:
            return "WAITING FOR STATUS", MUTED
        ring_overrun, queue_overflow = self.diagnostic_drop_counts()
        dropped = ring_overrun + queue_overflow
        return ("CAPTURE OK" if dropped == 0 else f"DROPS {dropped}", ACCENT if dropped == 0 else WARNING)

    def observe_capture_rate(self, capture_count: int) -> None:
        """Derive passive sample throughput from the periodic health record."""
        now = time.monotonic()
        if self._capture_rate_count is not None and self._capture_rate_time is not None:
            elapsed = now - self._capture_rate_time
            advanced = capture_count - self._capture_rate_count
            if elapsed > 0.2 and advanced >= 0:
                self.capture_rate = advanced / elapsed
        self._capture_rate_count = capture_count
        self._capture_rate_time = now

    def observe_header_rate(self) -> None:
        """Keep a rolling five-second physical-header arrival window."""
        now = time.monotonic()
        self.header_timestamps.append(now)
        cutoff = now - 5.0
        while self.header_timestamps and self.header_timestamps[0] < cutoff:
            del self.header_timestamps[0]

    def header_rate(self) -> float | None:
        if len(self.header_timestamps) < 2:
            return None
        elapsed = self.header_timestamps[-1] - self.header_timestamps[0]
        return (len(self.header_timestamps) - 1) / elapsed if elapsed > 0 else None

    def header_validation_detail(self) -> str:
        total = self.header_valid_count + self.header_invalid_count
        if not total:
            return "WAITING FOR HEADER CHECK"
        return f"CHECKS {self.header_valid_count}/{total} VALID"

    def diagnostic_drop_counts(self) -> tuple[int, int]:
        """Return capture-drop counters relative to the last Clear action."""
        ring_overrun = self.ring_overrun or 0
        queue_overflow = self.queue_overflow or 0
        # A HUD reboot resets firmware counters; treat that as a new window.
        if ring_overrun < self.health_ring_overrun_baseline:
            self.health_ring_overrun_baseline = 0
        if queue_overflow < self.health_queue_overflow_baseline:
            self.health_queue_overflow_baseline = 0
        return (
            ring_overrun - self.health_ring_overrun_baseline,
            queue_overflow - self.health_queue_overflow_baseline,
        )

    def clear_diagnostic_drops(self) -> None:
        """Start a new Health-card diagnostic window without altering capture."""
        self.health_ring_overrun_baseline = self.ring_overrun or 0
        self.health_queue_overflow_baseline = self.queue_overflow or 0
        self.append_usb_log("SYSTEM", "Cleared Monitor Health drop counters (new diagnostic window).")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def confirm_clear_diagnostic_drops(self) -> None:
        """Ask before resetting the local Capture Health diagnostic window."""
        self.clear_drops_prompt = True
        self.open_size_preview()

    def sync_track_to_18(self) -> None:
        """Manually anchor the passive step counter at the known directory track."""
        state = self.telemetry_parser.state
        state.position_half_tracks = 36
        state.position_source = "MANUAL SYNC 18"
        self.live_track = state.track
        self.track_var.set(self.live_track)
        self.clear_current_header()
        self.clear_recent_sectors()
        self.record_activity("MANUAL SYNC T18")
        self.append_usb_log("SYSTEM", "Track counter manually synchronized to Track 18.0 (display only).")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def effective_rpm(self) -> float | None:
        """Return the RPM established by stable, plausible SYNC windows."""
        return self.last_stable_rpm

    def disk_activity_label(self) -> str:
        """Render the disk write-gate state without mistaking CPU R/W for I/O."""
        if not self.motor:
            return "OFF"
        return "WRITING" if time.monotonic() < self._writing_display_until else "READING"

    def scroll_hud_cards(self) -> list[dict[str, str | int | None]]:
        """Return the capability-gated telemetry and Control OneROM dashboard cards."""
        rpm = self.effective_rpm()
        offset = self.header_offset()
        expected = self.expected_sector_count()
        coverage = f"SEEN {len(set(self.recent_sectors))}/{expected}" if expected else "WAITING"
        header = (f"T{self.last_header_track:02d} S{self.live_sector:02d}"
                  if self.last_header_track is not None and self.live_sector is not None else "WAITING")
        capture, _ = self.capture_health_detail()
        ring_overrun, queue_overflow = self.diagnostic_drop_counts()
        sync_estimate, sync_per_rev, sync_expected = self.sync_revolution_reading()
        header_rate = self.header_rate()
        track_overrange = track_is_over_dos_range(self.telemetry_parser.state.position_half_tracks)
        history_first, history_second = self.diagnostic_history_lines()
        fifo_first, fifo_second = self.sector_fifo_lines()
        position_source = self.telemetry_parser.state.position_source
        track_detail = (
            f"HEADER Δ {offset:+.1f} · {position_source}"
            if offset is not None
            else f"{position_source} · HEADER WAITING"
        )
        if track_overrange:
            track_detail = f"OVER 35.0 · {position_source}"
        cards = [
            ("track", "Track / Position", self.live_track, track_detail, "SYNC 18 manually anchors the display at directory Track 18.0 and sends no drive command; use it only after an initialize or directory operation has placed the head there. UNANCHORED means no physical starting point is known. CARRIED EST. retains the last phase-tracked position across a Monitor USB reconnect. HOME EST. is established only after 84 observed outward half-steps; the first inward transition then advances from the Track-1 bump to Track 1.5. TARGET ($0022) and a checksum-valid physical HEADER override any estimate."),
            ("rotation", "Motor Status", f"{rpm:.2f}" if rpm is not None else "--.--", self.rpm_quality_detail(), "Primary RPM is SYNC-derived: pulses per second × 60 ÷ expected SYNC marks per revolution. Expected marks are D3=42, D2=38, D1=36, D0=34. FW is the firmware-reported RPM used independently by SYNC / Revolution. Readings outside 240–360 RPM are rejected; the platter arrows appear only while motor telemetry is ON."),
            ("activity", "Activity", self.disk_activity_label(), f"WRITE PULSES {self.write_pulse_count} · STEPS {self.phase_event_count}", "WRITING is an observed write-gate pulse. Otherwise a spinning disk is shown as READING; OFF means motor telemetry is off. WRITE PULSES and STEPS are cumulative observations since this Monitor connection began, not DOS file-operation counts."),
            ("physical_header", "Physical Header", header, "CONFIRMED HEADER" if self.last_header_track is not None else "NO CONFIRMED HEADER", "This is the newest decoded on-disk GCR header: physical track and sector, not a software estimate. It clears after a seek or motor stop because that old header would no longer describe the current head location."),
            ("sector_coverage", "Sector Coverage", coverage, f"D{self.live_density} ZONE" if self.live_density is not None else "DENSITY UNKNOWN", "Counts unique sector numbers decoded on the current track observation window. Normal drive activity does not read every sector, so incomplete coverage is not a bad-sector report. Expected sectors are D3=21, D2=19, D1=18, D0=17."),
            ("sector_fifo", "Sector FIFO", fifo_first, fifo_second, "Chronological FIFO of the most recent checksum-decoded physical sector headers on one track. Values are not invented from track geometry: each S## arrived in an HDRPHY record. The FIFO holds 31 entries and clears after a seek, track change, or motor stop so it never mixes different tracks."),
            ("capture_health", "Capture Health", capture, f"CAP {self.capture_count} · ROV {ring_overrun} · QOV {queue_overflow}" if self.capture_count is not None else "STATUS PENDING", "CAP is the firmware capture count. ROV is raw capture-ring overrun; QOV is diagnostic queue overflow. DROPS is ROV + QOV since the last local Clear Drops action. Clearing changes only this display baseline, never firmware counters or telemetry."),
            ("disk_identity", "Disk Identity", self.disk_identity_label(), self.header_validation_detail(), "Disk ID is accepted only after two matching, checksum-valid physical headers. VERIFYING means more evidence is needed; ID CONFLICT means valid headers disagreed. This validates header metadata only and does not inspect the DOS directory or files."),
            ("density", "Density Zone", f"D{self.live_density}" if self.live_density is not None else "--", f"EXPECTED {sync_expected} SYNC / REV" if sync_expected else "WAITING FOR DENSITY", "Density is inferred from Monitor timing/header telemetry. It selects the expected sector and SYNC geometry used by coverage and RPM calculations: D3=42, D2=38, D1=36, D0=34 SYNC marks per revolution."),
            ("head", "Head", self.head_var.get(), f"POSITION {self.live_track} · {self.phase_event_count} STEPS", "IN means track/phase evidence moved toward higher tracks; OUT means lower tracks. STALL means the motor is running with no target or phase movement for 0.8 seconds. PARK means motor telemetry is off. Position remains an estimate until a physical header confirms it."),
            ("header_rate", "Header Rate", f"{header_rate:.1f}/S" if header_rate is not None else "WAITING", "Rate of checksum-decoded physical headers over a rolling five-second window. It is a passive observation rate, not a guarantee of disk health. WAITING means fewer than two valid header timestamps are available."),
            ("capture_rate", "Capture Rate", f"{self.capture_rate / 1000:.0f}K/S" if self.capture_rate is not None else "WAITING", "Passive firmware capture events per second, calculated from the change in CAP between periodic status records. WAITING means the Monitor has not yet received two usable capture-count samples."),
            ("sync_rate", "SYNC Rate", "MOTOR OFF" if not self.motor else f"{self.qualified_sync_count}/S" if self.qualified_sync_count is not None else "ACQUIRING", "A stable two-window SYNC rate. Start, stop, and seek intervals are intentionally shown as ACQUIRING rather than reported as a misleading partial measurement."),
            ("sync_per_rev", "SYNC / REV EST.", str(sync_per_rev) if sync_per_rev is not None else "--", f"RAW {sync_estimate:.2f}" if sync_estimate is not None else "WAITING FOR SYNC", "Calculated as SYNC/sec × 60 ÷ RPM, then rounded to a physical count. This is an estimate until it is validated over a stable multi-revolution window. RAW is the unrounded ratio. Compare the result with the density expectation; nonstandard or copy-protected media may intentionally differ."),
            ("mechanism", "Mechanism", f"{self.phase_event_count} STEPS", f"TRACK {self.live_track} · HEAD {self.head_var.get()}", "Cumulative observed phase/step transitions since Monitor connection. It is useful for seeing mechanical activity and repeated seeking, but it does not reset per disk and is not an absolute head-position counter."),
            ("recent_evidence", "Recent Evidence", history_first, history_second or "PASSIVE EVENT HISTORY", "A compact chronological trace of decoded headers, seek events, write-gate activity, and motor changes. It is passive evidence for what the Monitor observed most recently; DOS errors, retries, and directory activity are not exposed by this telemetry."),
        ]
        # Passive cards require a real Monitor OneROM session.  The dashboard
        # can still open with only Control connected, but it must not present
        # stale or invented Monitor measurements as if that board existed.
        if not self.ub4.get():
            cards = []
        # Control OneROM cards join the same sortable dashboard rather than
        # living on a separate, dead-end screen.  The desktop preview uses
        # the same list for layout work, but does not fake a USB connection
        # or transmit a command.  A physical Control OneROM is still required
        # before a real control action can be applied.
        if self.ub3.get():
            if self.controller_rom_enabled.get():
                cards.append(("startup_rom", "Control OneROM · ROM Selection", self.rom_choice.get(), self.controller_card_detail("startup_rom", "TAP TO CHOOSE STARTUP ROM"), "Selects the startup ROM slot used by the Control OneROM. Choose a slot, then confirm the save. With a connected Control OneROM, the UI sends ROMSET=<slot> and waits for its explicit OK/FAIL reply; preview mode changes only the local display."))
            if self.controller_iec_enabled.get():
                cards.append(("boot_iec", "Control OneROM · IEC Address", self.iec_choice.get(), self.controller_card_detail("boot_iec", "TAP TO CHOOSE IEC ADDRESS"), "Selects the IEC device address used at boot. Choose Device 8–11, then confirm the save. With a connected Control OneROM, the UI sends ROMIEC=<address> and waits for an explicit OK/FAIL reply; preview mode sends nothing."))
            if self.controller_wp_enabled.get():
                cards.append(("write_protect", "Write Protect Override", "OVERRIDE ON" if self._confirmed_writable else "NORMAL PROTECTION", self.physical_disk_write_status(), "The physical disk sensor reports PROTECTED or WRITABLE. This Control OneROM action can override that sensor through X2 and force writable mode. Confirmation is required; the UI sends ROMWP=ON/OFF and changes state only after a matching verification reply. Preview mode is local only."))
        result = []
        # Older saved/custom card definitions used a four-field tuple.  Keep
        # the dashboard usable if one of those leaks in while a newer build
        # expects the expanded help field.
        for card in cards:
            if len(card) == 4:
                card_id, title, value, legacy_help = card
                # The old four-field shape was (id, title, value, help),
                # not (id, title, value, detail).  Never paint that
                # long-form help paragraph into the one-line card detail.
                detail = "STATUS / CALCULATION · TAP ?"
                help_text = legacy_help
            else:
                card_id, title, value, detail, help_text = card
            help_text = DETAILED_CARD_HELP.get(card_id, help_text)
            result.append({"id": card_id, "title": title, "value": value, "detail": detail,
                           "help": help_text, "priority": self.hud_card_priorities.get(card_id),
                           "kind": "control" if card_id in {"startup_rom", "boot_iec", "write_protect"} else "telemetry",
                           "alert": (
                               (card_id == "track" and track_overrange)
                               or (card_id == "write_protect" and self._confirmed_writable)
                           )})
        return sorted(
            result,
            key=lambda card: (
                (0, int(card["priority"]), str(card["title"]))
                if card["priority"] is not None
                # Unpinned controls deliberately follow all telemetry. This
                # keeps the two optional setup cards together at the bottom
                # rather than scattering them through diagnostic readings.
                else (1, 1 if card["kind"] == "control" else 0, str(card["title"]))
            ),
        )

    def controller_card_detail(self, card_id: str, idle_text: str) -> str:
        """Return the latest visible Control OneROM outcome for a control card."""
        return self.controller_card_feedback.get(card_id, idle_text)

    def dashboard_connection_status(self) -> tuple[str, str]:
        """Describe actual board connections without mistaking preview for USB."""
        hud = (
            "MONITOR: CONNECTED" if self.ub4.get() else
            "MONITOR: ASSIGNED / OFFLINE" if self.drive_binding.hud_serial else
            "MONITOR: UNASSIGNED"
        )
        controller = (
            "CONTROL: CONNECTED" if self.ub3.get() else
            "CONTROL: ASSIGNED / OFFLINE" if self.drive_binding.controller_serial else
            "CONTROL: PREVIEW" if self.controller_gui_preview else
            "CONTROL: UNASSIGNED"
        )
        color = ACCENT if self.ub4.get() and self.ub3.get() else MUTED
        return f"{hud} · {controller}", color

    @staticmethod
    def scroll_card_paint_text(card: dict[str, str | int | None]) -> tuple[str, str]:
        """Fit special long-form cards into the shared scrolling row."""
        value, detail = str(card["value"]), str(card["detail"])
        # The card detail is a single status line.  Help paragraphs belong in
        # the modal, where they receive a bounded, wrapped text area.
        def single_line(text: str, limit: int = 58) -> str:
            text = " ".join(text.split())
            return text if len(text) <= limit else f"{text[:limit - 3]}..."

        if card["id"] in {"recent_evidence", "sector_fifo"}:
            # Evidence is an event trace, not a primary measurement. Keep a
            # useful leading portion on its own compact line and reserve the
            # lower line for its description.
            return single_line(value, 90), single_line(detail, 90)
        return value, single_line(detail)

    def cycle_hud_card_priority(self, card_id: str) -> None:
        """Move one card through P1…P99, then return it to unpinned."""
        current = self.hud_card_priorities.get(card_id)
        # Duplicate priorities are intentional: the user can group related
        # cards, and alphabetical title order breaks those ties consistently.
        next_priority = 1 if current is None else (None if current >= 99 else current + 1)
        self.hud_card_priorities[card_id] = next_priority
        self.save_hud_priorities()
        self.hud_scroll_index = 0
        self.refresh_hud_subscription()

    def load_saved_hud_priorities(self) -> None:
        """Overlay valid saved priorities without losing defaults for new cards."""
        for card_id, priority in self.drive_binding.priorities.items():
            if (
                isinstance(card_id, str)
                and (priority is None or (isinstance(priority, int) and not isinstance(priority, bool) and 1 <= priority <= 99))
            ):
                self.hud_card_priorities[card_id] = priority

    def save_hud_priorities(self) -> None:
        """Persist card ordering together with the drive's USB bindings and colors."""
        self.drive_binding.priorities = dict(self.hud_card_priorities)
        try:
            save_binding(self.binding_path, self.drive_binding)
        except OSError as exc:
            self.usb_status.set(f"HUD priorities could not be saved: {exc}")

    def load_saved_control_features(self) -> None:
        """Restore local Control-card visibility preferences from the binding."""
        features = self.drive_binding.control_features
        self.controller_rom_enabled.set(features.get("rom_select", DEFAULT_CONTROL_FEATURES["rom_select"]))
        self.controller_iec_enabled.set(features.get("iec_address", DEFAULT_CONTROL_FEATURES["iec_address"]))
        self.controller_wp_enabled.set(features.get("write_protect_override", DEFAULT_CONTROL_FEATURES["write_protect_override"]))

    def save_control_features(self) -> None:
        """Persist the three Control-card visibility settings with this drive."""
        self.drive_binding.control_features = {
            "rom_select": self.controller_rom_enabled.get(),
            "iec_address": self.controller_iec_enabled.get(),
            "write_protect_override": self.controller_wp_enabled.get(),
        }
        try:
            save_binding(self.binding_path, self.drive_binding)
        except OSError as exc:
            self.usb_status.set(f"Control feature preferences could not be saved: {exc}")

    def scroll_hud_by(self, amount: int) -> None:
        """Move the visible card viewport while retaining a valid final page."""
        max_start = max(0, len(self.scroll_hud_cards()) - HUD_VISIBLE_CARD_COUNT)
        self.hud_scroll_index = max(0, min(max_start, self.hud_scroll_index + amount))
        self.refresh_hud_subscription()

    def visible_hud_subscription_mask(self) -> int:
        """Return the firmware telemetry classes needed by the visible cards."""
        core, headers, metadata, rpm, sync = 1, 2, 4, 8, 16
        needed = {
            "track": core, "rotation": core | rpm | sync, "activity": core,
            "physical_header": headers, "sector_coverage": headers, "sector_fifo": headers,
            "capture_health": core, "disk_identity": headers | metadata,
            "write_protect": core, "density": core, "head": core,
            "header_rate": headers, "capture_rate": core, "sync_rate": sync,
            "sync_per_rev": sync, "mechanism": core,
            "recent_evidence": core | headers,
        }
        cards = self.scroll_hud_cards()
        visible = cards[self.hud_scroll_index:self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT]
        # These are bit flags, not quantities.  Arithmetic addition breaks
        # when two visible cards need the same class: for example two CORE
        # cards made 1 + 1 == 2 (HEADERS), which accidentally turned CORE
        # telemetry off.  Combine each required class exactly once.
        # Keep compact STATUS/core records alive even on a control-only
        # viewport. They preserve connection state and avoid a transition
        # from "quiet" to an apparently dead monitor.
        mask = core
        for card in visible:
            mask |= needed.get(str(card["id"]), 0)
        return mask

    def update_hud_subscription(self, link: CdcBoardLink) -> None:
        """Tell the Monitor firmware to emit only telemetry used by this viewport."""
        mask = self.visible_hud_subscription_mask()
        if mask != self._hud_subscription_mask:
            link.write_command(f"HUDCFG M={mask}")
            self._hud_subscription_mask = mask

    def refresh_hud_subscription(self) -> None:
        """Apply a changed viewport subscription without disturbing the link."""
        link = self.usb_links.get("hud")
        if link is None or not link.connected:
            return
        try:
            self.update_hud_subscription(link)
        except Exception as exc:
            # A failed optional configuration write must not tear down a
            # healthy read-only telemetry session.
            self._hud_subscription_mask = None
            self.append_usb_log("SYSTEM", f"Monitor viewport subscription update failed: {exc}")

    def request_hud_redraw(self) -> None:
        """Rebuild the visible six cards at a bounded desktop-only cadence."""
        if self._hud_redraw_after is not None:
            return

        def redraw() -> None:
            self._hud_redraw_after = None
            if self.preview_page == "hud":
                self.open_size_preview()

        self._hud_redraw_after = self.after(150, redraw)

    def preview_icon_tooltip(self, x: float, y: float) -> str | None:
        """Return the plain-language label for a hovered canvas icon."""
        if 14 <= y <= 70:
            top_icons = {
                "hud": ((1136, 1192, "Open OneROM Activity Log"), (1200, 1256, "Open settings")),
                "settings": ((1136, 1192, "Open OneROM Activity Log"), (1200, 1256, "Open Monitor list")),
                "log": (
                    (1008, 1064, "Clear log entries"),
                    (1072, 1128, "Copy log entries"),
                    (1136, 1192, "Open Monitor list"),
                    (1200, 1256, "Open settings"),
                ),
                "controller_connection": ((1136, 1192, "Open Monitor list"), (1200, 1256, "Open settings")),
                "hud_connection": ((1136, 1192, "Open Monitor list"), (1200, 1256, "Open settings")),
            }
            for left, right, label in top_icons.get(self.preview_page, ()):
                if left <= x <= right:
                    return label
        if HUD_SCROLL_LEFT <= x <= HUD_SCROLL_RIGHT:
            scroll_labels = (
                (100, 240, "First page" if self.preview_page == "log" else "Back five cards"),
                (244, 384, "Previous page" if self.preview_page == "log" else "Previous card"),
                (388, 528, "Next page" if self.preview_page == "log" else "Next card"),
                (532, 672, "Last page" if self.preview_page == "log" else "Forward five cards"),
            )
            for top, bottom, label in scroll_labels:
                if top <= y <= bottom:
                    return label
        if self.preview_page == "settings" and 1178 <= x <= 1246 and 470 <= y <= 530:
            return "Customize appearance"
        if self.preview_page in ("controller_connection", "hud_connection") and 24 <= x <= 314 and 620 <= y <= 672:
            return "Back to settings"
        if self.preview_page == "hud" and HUD_HELP_LEFT <= x <= HUD_HELP_RIGHT:
            index = int((y - HUD_CARD_TOP) // HUD_CARD_PITCH)
            row_y = HUD_CARD_TOP + index * HUD_CARD_PITCH
            if 0 <= index < HUD_VISIBLE_CARD_COUNT and row_y + 23 <= y <= row_y + 87:
                return "Card help"
        return None

    def update_preview_tooltip(self, event: tk.Event, canvas: tk.Canvas, sx: float, sy: float) -> None:
        """Draw a small bounded tooltip for hoverable touchscreen icons."""
        canvas.delete("icon_tooltip")
        if any((self.hud_help_card, self.priority_prompt_card, self.write_protect_prompt,
                self.clear_drops_prompt, self.dashboard_setting_prompt, self.popup_menu)):
            return
        label = self.preview_icon_tooltip(event.x / sx, event.y / sy)
        if not label:
            return
        label_id = canvas.create_text(event.x + 14, event.y + 18, text=label, anchor="nw",
                                      fill=TEXT, font=("Segoe UI", 12, "bold"), tags="icon_tooltip")
        left, top, right, bottom = canvas.bbox(label_id)
        # Keep the tooltip inside the fixed 7-inch canvas rather than
        # letting it disappear off the right/bottom edge.
        shift_x = min(0, canvas.winfo_width() - right - 8)
        shift_y = min(0, canvas.winfo_height() - bottom - 8)
        if shift_x or shift_y:
            canvas.move(label_id, shift_x, shift_y)
            left, top, right, bottom = canvas.bbox(label_id)
        background = canvas.create_rectangle(left - 7, top - 5, right + 7, bottom + 5,
                                             fill=BG, outline=ACCENT, tags="icon_tooltip")
        canvas.tag_lower(background, label_id)

    def update_live_hud_fields(self) -> None:
        """Update existing Monitor Canvas items without rebuilding the screen."""
        if self.preview_page != "hud":
            return
        preview = getattr(self, "preview", None)
        canvas = getattr(self, "preview_canvas", None)
        if preview is None or canvas is None or not preview.winfo_exists():
            return
        try:
            # Only configure canvas tags for visible cards. Updating every
            # hidden card on every CDC batch is pointless work and was one
            # contributor to the old slow/frozen-looking display behavior.
            cards = self.scroll_hud_cards()
            visible = cards[self.hud_scroll_index:self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT]
            for card in visible:
                card_id = str(card["id"])
                value, detail = self.scroll_card_paint_text(card)
                if card_id != "recent_evidence" and len(value) > 24:
                    value = f"{value[:21]}..."
                canvas.itemconfigure(f"scroll_{card_id}_value", text=value)
                canvas.itemconfigure(f"scroll_{card_id}_detail", text=detail)
                if card_id in {"track", "write_protect"}:
                    alert = bool(card.get("alert"))
                    canvas.itemconfigure(
                        f"scroll_{card_id}_background",
                        fill=TRACK_ALERT_PANEL if alert else PANEL,
                        outline=OFFLINE if alert else ACCENT,
                    )
                    canvas.itemconfigure(
                        f"scroll_{card_id}_title",
                        fill=OFFLINE if alert else MUTED,
                    )
                    canvas.itemconfigure(
                        f"scroll_{card_id}_value",
                        fill=OFFLINE if alert else TEXT,
                    )
                    canvas.itemconfigure(
                        f"scroll_{card_id}_detail",
                        fill=OFFLINE if alert else MUTED,
                    )
                if card_id == "write_protect":
                    canvas.itemconfigure(
                        f"scroll_{card_id}_value",
                        fill=OFFLINE if self._confirmed_writable else TEXT,
                    )
                    action_color = OFFLINE if self._confirmed_writable else ACCENT
                    canvas.itemconfigure(f"scroll_{card_id}_action_box", outline=action_color)
                    canvas.itemconfigure(
                        f"scroll_{card_id}_action_text",
                        text="DISABLE OVERRIDE" if self._confirmed_writable else "ENABLE OVERRIDE",
                        fill=action_color,
                    )
            # Paint the existing visible rows only.  Recreating the whole
            # Canvas while the CDC stream is active can keep Windows/Tk in a
            # perpetual redraw cycle, making a healthy link look frozen.
            canvas.update_idletasks()
        except tk.TclError:
            # The screen may have changed between CDC receive and paint.
            pass

    def schedule_write_protect_update(self, protected: bool) -> None:
        """Apply the proven GUI-only 250 ms last-event-wins WP debounce."""
        self._wp_pending_state = protected
        if self._wp_after_id is not None:
            try:
                self.after_cancel(self._wp_after_id)
            except tk.TclError:
                pass
        self._wp_after_id = self.after(250, self.commit_write_protect_update)

    def commit_write_protect_update(self) -> None:
        self._wp_after_id = None
        if self._wp_pending_state is None:
            return
        self.live_protected = self._wp_pending_state
        self.wp_var.set("PROTECTED" if self.live_protected else "WRITABLE")
        # The fixed right-side card is updated in place: no full Canvas
        # rebuild is needed merely because a disk was inserted or removed.
        self._hud_dirty = True
        self.update_live_hud_fields()

    def hud_write_protect_label(self) -> str:
        """Return effective write permission, not only the passive sensor bit."""
        # X2 overrides the physical sensor.  The HUD continues to read that
        # sensor passively, but the operator needs the state the drive will
        # actually obey when the Control OneROM has confirmed X2 ON.
        if self._confirmed_writable:
            return "FORCES WRITABLE"
        if self.live_protected is None:
            return "WAITING FOR SENSOR"
        return "PROTECTED" if self.live_protected else "WRITABLE"

    def hud_write_protect_detail(self) -> str:
        """Explain whether the displayed permission is sensor or override led."""
        if self._confirmed_writable:
            return "CONTROL OVERRIDE ACTIVE"
        if self.live_protected is None:
            return "PHYSICAL SENSOR PENDING"
        return "PHYSICAL WRITE-PROTECT SENSOR"

    def physical_disk_write_status(self) -> str:
        """Return the observed media notch/sensor state, independent of X2."""
        if self.live_protected is None:
            return "DISK STATUS WAITING"
        return "DISK PROTECTED" if self.live_protected else "DISK WRITABLE"

    def apply_device_state(self, initial=False) -> None:
        # A maintained override must never survive loss of the control board.
        # This models the real application rule for a drive power-off / USB
        # disconnect: re-query before any later change is permitted.
        if not self.ub3.get():
            self.writable.set(False)
        self.hud_button.configure(state="normal" if self.ub4.get() else "disabled")
        controller_id = self.drive_binding.controller_serial or "Not connected"
        hud_id = self.drive_binding.hud_serial or "Not connected"
        self.hud_connection.set(f"Monitor OneROM {hud_id} connected • Passive telemetry running" if self.ub4.get() else "Monitor OneROM not detected • Monitor unavailable")
        self.control_status.set(f"Control OneROM {controller_id} connected • Ready for Control commands" if self.ub3.get() else "Control OneROM not detected • Control actions unavailable")
        state = []
        state.append(f"Control OneROM: {controller_id}" if self.ub3.get() else "Control OneROM: Not connected")
        state.append(f"Monitor OneROM: {hud_id}" if self.ub4.get() else "Monitor OneROM: Not connected")
        self.status_var.set("   •   ".join(state))
        self.device_summary.set("   •   ".join(state) + "\nBoard roles are assigned by USB serial number; COM ports may change without changing the drive binding.")
        if not initial and self.current_page not in ("settings", "idle"):
            self.show_page(self.default_page())
        elif initial:
            self.show_page(self.default_page())

    def apply_startup(self) -> None:
        self.show_page(self.default_page())

    def save_rom(self) -> None:
        choice = self.rom_choice.get()
        try:
            slot = int(choice.split()[1])
            self.send_controller_command(f"ROMSET={slot}")
            self.rom_var.set(choice)
            self._pending_controller_card = "startup_rom"
            self.begin_controller_transaction("startup_rom")
            self.controller_card_feedback["startup_rom"] = "SENT · WAITING FOR CONFIRMATION"
            self.control_status.set(f"Startup ROM slot {slot} sent to the connected Control OneROM.")
        except Exception as exc:
            self._pending_controller_card = None
            self.controller_card_feedback["startup_rom"] = "SAVE FAILED"
            self.control_status.set(f"Startup ROM was not changed: {exc}")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def save_iec(self) -> None:
        choice = self.iec_choice.get()
        try:
            address = int(choice.split()[-1])
            self.send_controller_command(f"ROMIEC={address}")
            self.iec_var.set(choice)
            self._pending_controller_card = "boot_iec"
            self.begin_controller_transaction("boot_iec")
            self.controller_card_feedback["boot_iec"] = "SENT · WAITING FOR CONFIRMATION"
            self.control_status.set(f"Boot IEC address {address} sent to the connected Control OneROM.")
        except Exception as exc:
            self._pending_controller_card = None
            self.controller_card_feedback["boot_iec"] = "SAVE FAILED"
            self.control_status.set(f"IEC address was not changed: {exc}")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def refresh_control_startup_settings(self) -> None:
        """Read the saved ROM slot and boot IEC address without changing either."""
        if self._controller_transactions:
            self.control_status.set("Waiting for the prior Control OneROM response.")
            return
        try:
            self.send_controller_command("ROMGET")
            self.begin_controller_transaction("settings_refresh")
            self.controller_card_feedback["startup_rom"] = "REFRESHING FROM CONTROL ONEROM"
            self.controller_card_feedback["boot_iec"] = "REFRESHING FROM CONTROL ONEROM"
            self.control_status.set("Reading saved ROM slot and boot IEC address from Control OneROM.")
        except Exception as exc:
            self.controller_card_feedback["startup_rom"] = "REFRESH FAILED"
            self.controller_card_feedback["boot_iec"] = "REFRESH FAILED"
            self.control_status.set(f"Control settings were not refreshed: {exc}")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def handle_controller_settings_refresh_reply(self, line: str) -> bool:
        """Apply the firmware's read-only ROMGET response to both cards."""
        if "settings_refresh" not in self._controller_transactions:
            return False
        slot_match = re.search(r"\bSLOT=(\d+)", line, re.IGNORECASE)
        iec_match = re.search(r"\bBOOT_IEC=(\d+)", line, re.IGNORECASE)
        if slot_match is None or iec_match is None:
            return False
        slot, address = int(slot_match.group(1)), int(iec_match.group(1))
        if not 1 <= slot <= len(self.rom_choices) or not 8 <= address <= 11:
            return False
        self.rom_choice.set(self.rom_choices[slot - 1])
        self.rom_var.set(self.rom_choice.get())
        self.iec_choice.set(f"Device {address}")
        self.iec_var.set(self.iec_choice.get())
        self.controller_card_feedback["startup_rom"] = "REFRESHED FROM CONTROL ONEROM"
        self.controller_card_feedback["boot_iec"] = "REFRESHED FROM CONTROL ONEROM"
        self.control_status.set("Saved ROM slot and boot IEC address refreshed from Control OneROM.")
        self.finish_controller_transaction("settings_refresh")
        self._hud_dirty = True
        self.update_live_hud_fields()
        return True

    def handle_controller_setting_reply(self, line: str) -> bool:
        """Paint an explicit Control OneROM OK/FAIL reply onto its source card."""
        upper = line.upper()
        # Commands intentionally use the terse CDC command names (ROMSET and
        # ROMIEC), while the firmware replies with its stable record types:
        # $ROMTEST,SET,... and $ROMTEST,IEC,....  Treating the command text as
        # the reply discriminator made a verified IEC save look like a timeout.
        card_id = (
            "startup_rom" if ",SET," in upper or "ROMSET" in upper else
            "boot_iec" if ",IEC," in upper or "ROMIEC" in upper else None
        )
        if card_id is None or card_id not in self._controller_transactions:
            return False
        if ",OK," in upper or upper.endswith(",OK"):
            self.controller_card_feedback[card_id] = "CONFIRMED BY CONTROL ONEROM"
            self.control_status.set(f"Control OneROM confirmed {card_id.replace('_', ' ')}.")
        elif ",FAIL" in upper or ",ERROR," in upper:
            self.controller_card_feedback[card_id] = "CONTROL ONEROM REJECTED SAVE"
            self.control_status.set(f"Control OneROM rejected {card_id.replace('_', ' ')}.")
        else:
            return False
        self.finish_controller_transaction(card_id)
        self._pending_controller_card = None
        self._hud_dirty = True
        self.update_live_hud_fields()
        return True

    def update_protection(self) -> None:
        desired = self.writable.get()
        try:
            self.send_controller_command("ROMWP=ON" if desired else "ROMWP=OFF")
            self._pending_wp_override = desired
            self.begin_controller_transaction("write_protect")
            self.control_status.set("Verifying X2 write-protect override…")
        except Exception as exc:
            self.writable.set(self._confirmed_writable)
            self.control_status.set(f"Write-protect override was not changed: {exc}")

    def request_controller_wp_state(self) -> None:
        """Ask the Control OneROM to report whether its X2 override is usable."""
        try:
            self.send_controller_command("ROMWP?")
            self.append_usb_log("SYSTEM", "Queried Control OneROM X2 write-protect override state.")
        except Exception as exc:
            self.append_usb_log("SYSTEM", f"Control OneROM X2 write-protect query was not sent: {exc}")

    def handle_controller_wp_reply(self, line: str) -> None:
        """Accept only the prior selector's explicit X2 verification reply."""
        fields: dict[str, str] = {}
        for part in line.split(",")[2:]:
            if "=" in part:
                key, value = part.split("=", 1)
                fields[key] = value
            elif part:
                fields.setdefault("STATUS", part)
        available = fields.get("AVAILABLE") == "1"
        reported_on = fields.get("STATE") == "ON"
        self.controller_wp_available = available
        if self._pending_wp_override is None:
            if available:
                self._confirmed_writable = reported_on
                self.writable.set(reported_on)
                self.control_status.set(
                    "Control OneROM X2 override is ON — forces writable."
                    if reported_on else "Control OneROM X2 override is OFF — normal protection."
                )
            else:
                self.control_status.set(fields.get("ERROR", "Control OneROM reports X2 write-protect override unavailable."))
            self.update_live_hud_fields()
            return
        wanted = self._pending_wp_override
        verified = fields.get("VERIFY") == ("ON" if wanted else "OFF")
        if fields.get("STATUS") == "OK" and available and reported_on == wanted and verified:
            self._confirmed_writable = wanted
            self.writable.set(wanted)
            self.control_status.set(
                "Control OneROM verified X2 override ON — drive forced writable."
                if wanted else "Control OneROM verified X2 override OFF — normal protection restored."
            )
        else:
            self.writable.set(self._confirmed_writable)
            self.control_status.set(f"Control OneROM did not verify X2 override: {line}")
        self._pending_wp_override = None
        self.finish_controller_transaction("write_protect")
        self.update_live_hud_fields()

    def begin_controller_transaction(self, card_id: str) -> None:
        """Start a bounded UI transaction for one explicit Control command."""
        self.finish_controller_transaction(card_id)
        self._controller_transactions[card_id] = self.after(
            CONTROL_REPLY_TIMEOUT_MS,
            lambda current=card_id: self.controller_transaction_timeout(current),
        )

    def finish_controller_transaction(self, card_id: str) -> None:
        """Cancel a transaction timer once its matching reply is received."""
        after_id = self._controller_transactions.pop(card_id, None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass

    def controller_transaction_timeout(self, card_id: str) -> None:
        """Make missing Control replies visible instead of waiting forever."""
        if self._controller_transactions.pop(card_id, None) is None:
            return
        if card_id == "write_protect":
            self._pending_wp_override = None
            self.writable.set(self._confirmed_writable)
        if self._pending_controller_card == card_id:
            self._pending_controller_card = None
        if card_id == "settings_refresh":
            self.controller_card_feedback["startup_rom"] = "CONTROL REFRESH TIMEOUT"
            self.controller_card_feedback["boot_iec"] = "CONTROL REFRESH TIMEOUT"
        self.controller_card_feedback[card_id] = "CONTROL RESPONSE TIMEOUT"
        self.control_status.set(f"Control OneROM response timeout: {card_id.replace('_', ' ')}.")
        self.append_usb_log("CONTROL", self.control_status.get())
        self._hud_dirty = True
        self.update_live_hud_fields()

    def send_controller_command(self, command: str) -> None:
        """Send only the documented USB Selector commands to Control OneROM."""
        link = self.usb_links.get("controller")
        if link is None or not link.connected:
            raise RuntimeError("Control OneROM is not connected")
        link.write_command(command)

    def confirm_write_protect_toggle(self) -> None:
        """Show an in-display confirmation before changing it through UB3."""
        if self._controller_transactions:
            self.control_status.set("Waiting for the prior Control OneROM confirmation.")
            self.open_size_preview()
            return
        if not self.controller_preview_available():
            self.control_status.set("Write-protect control unavailable: Control OneROM is disconnected.")
            self.open_size_preview()
            return
        if not self.controller_wp_enabled.get():
            self.control_status.set("Write-protect control is disabled in this Control OneROM build.")
            self.open_size_preview()
            return
        self.write_protect_prompt = not self.writable.get()
        self.open_size_preview()

    def apply_write_protect_toggle(self, desired: bool) -> None:
        """Apply confirmed override, preserving desktop preview isolation."""
        self.writable.set(desired)
        if self.ub3.get():
            self.update_protection()
        else:
            # Preview proves the interaction and card state without creating
            # a serial command or pretending a physical Control OneROM replied.
            self._confirmed_writable = desired
            self.control_status.set(
                "Write-protect override preview enabled." if desired
                else "Write-protect override preview disabled."
            )
            self._hud_dirty = True
            self.update_live_hud_fields()

    def save_preview_rom(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: Control OneROM connection is unavailable.")
        elif not self.controller_rom_enabled.get():
            self.control_status.set("Save failed: Startup ROM control is disabled in this build.")
        else:
            self.save_rom()
        self.open_size_preview()

    def save_preview_iec(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: Control OneROM connection is unavailable.")
        elif not self.controller_iec_enabled.get():
            self.control_status.set("Save failed: IEC address control is disabled in this build.")
        else:
            self.save_iec()
        self.open_size_preview()

    def choose_from_menu(self, event, choices: tuple[str, ...], variable: tk.StringVar, message: str,
                         *, title: str = "Choose value", on_select=None) -> None:
        self.popup_menu = {
            "kind": "choice",
            "title": title,
            "choices": choices,
            "variable": variable,
            "message": message,
            "on_select": on_select,
        }
        self.open_size_preview()

    def choose_dashboard_control(self, card_id: str, event) -> None:
        """Open the appropriate compact menu for a Control OneROM dashboard card."""
        if self._controller_transactions:
            self.control_status.set("Waiting for the prior Control OneROM confirmation.")
            self.open_size_preview()
            return
        if card_id == "startup_rom":
            self.choose_from_menu(
                event, self.rom_choices, self.rom_choice,
                "Startup ROM selected: {value}.", title="Startup ROM",
                on_select=self.request_dashboard_rom_choice,
            )
        elif card_id == "boot_iec":
            self.choose_from_menu(
                event, self.iec_choices, self.iec_choice,
                "Boot IEC address selected: {value}.", title="IEC Address",
                on_select=self.request_dashboard_iec_choice,
            )
        elif card_id == "write_protect":
            self.confirm_write_protect_toggle()

    def request_dashboard_rom_choice(self, choice: str) -> None:
        """Require confirmation before changing the Control OneROM startup ROM."""
        self.dashboard_setting_prompt = {"kind": "rom", "choice": choice, "label": "STARTUP ROM"}

    def request_dashboard_iec_choice(self, choice: str) -> None:
        """Require confirmation before changing the Control OneROM IEC address."""
        self.dashboard_setting_prompt = {"kind": "iec", "choice": choice, "label": "IEC ADDRESS"}

    def apply_dashboard_setting(self) -> None:
        """Commit the confirmed dashboard setting, then return to the Monitor."""
        prompt = self.dashboard_setting_prompt
        if prompt is None:
            return
        choice = prompt["choice"]
        if prompt["kind"] == "rom":
            self.rom_choice.set(choice)
            if self.ub3.get():
                self.save_rom()
            else:
                self.controller_card_feedback["startup_rom"] = "PREVIEW SAVED · NO CONTROL ONEROM"
                self.control_status.set("Startup ROM preview saved; connect a Control OneROM to apply it.")
        else:
            self.iec_choice.set(choice)
            if self.ub3.get():
                self.save_iec()
            else:
                self.controller_card_feedback["boot_iec"] = "PREVIEW SAVED · NO CONTROL ONEROM"
                self.control_status.set("IEC address preview saved; connect a Control OneROM to apply it.")
        self.dashboard_setting_prompt = None
        self.preview_page = "hud"

    def choose_appearance_menu(self, event) -> None:
        """Choose which visual color family to edit from one touch menu."""
        self.popup_menu = {
            "kind": "appearance",
            "title": "Appearance",
            "choices": APPEARANCE_CHOICES,
        }
        self.open_size_preview()

    def return_to_appearance_menu(self) -> None:
        """Close Custom Color and restore its parent Appearance menu."""
        self.color_picker = None
        self.hex_keyboard = False
        self.popup_menu = {
            "kind": "appearance",
            "title": "Appearance",
            "choices": APPEARANCE_CHOICES,
        }
        self.open_size_preview()

    def popup_menu_layout(self) -> tuple[int, int, int, int, int]:
        """Virtual coordinates for a touch menu that always fits the display."""
        option_count = len(self.popup_menu["choices"]) if self.popup_menu else 0
        row_height = 60
        height = 92 + option_count * row_height
        x1, x2 = 236, 1044
        y1 = (720 - height) // 2
        return x1, y1, x2, y1 + 92, row_height

    @staticmethod
    def is_hex_color(color: str) -> bool:
        return bool(HEX_COLOR_RE.fullmatch(color))

    def apply_preview_color_value(self, target: str, color: str) -> None:
        """Apply one validated display color without changing persistence."""
        global BG, PANEL, PANEL_ALT, ACCENT, MUTED, TEXT, WARNING, OFFLINE
        if target == "background":
            BG = color
        elif target == "card":
            PANEL = color
        elif target == "button_surface":
            PANEL_ALT = color
        elif target == "accent":
            ACCENT = color
        elif target == "text_primary":
            TEXT = color
        elif target == "text_secondary":
            MUTED = color
        elif target == "warning":
            WARNING = color
        elif target == "offline":
            OFFLINE = color

    def apply_saved_appearance(self) -> None:
        """Restore valid local display preferences from the drive binding."""
        for target, color in self.drive_binding.appearance.items():
            if target in APPEARANCE_TARGETS and isinstance(color, str) and self.is_hex_color(color):
                self.apply_preview_color_value(target, color.upper())

    def set_preview_color(self, target: str, color: str) -> None:
        """Apply and persist one touchscreen color alongside the USB bindings."""
        if target not in APPEARANCE_TARGETS or not self.is_hex_color(color):
            raise ValueError(f"Invalid appearance color: {target}={color!r}")
        normalized = color.upper()
        self.apply_preview_color_value(target, normalized)
        self.drive_binding.appearance[target] = normalized
        try:
            save_binding(self.binding_path, self.drive_binding)
        except OSError as exc:
            self.usb_status.set(f"Display preference could not be saved: {exc}")
        self.color_picker = None
        self.hex_keyboard = False
        self.open_size_preview()

    def preview_color(self, target: str) -> str:
        """Return the currently active color for one appearance area."""
        return {
            "background": BG,
            "card": PANEL,
            "button_surface": PANEL_ALT,
            "accent": ACCENT,
            "text_primary": TEXT,
            "text_secondary": MUTED,
            "warning": WARNING,
            "offline": OFFLINE,
        }[target]

    def toggle_motor(self) -> None:
        self.motor = not self.motor
        self.motor_var.set("ON" if self.motor else "OFF")
        self.head_var.set("IN" if self.motor else "PARK")
        self.rpm_var.set("300.7" if self.motor else "---.-")

    def simulated_density(self) -> str:
        """1541 GCR density zone for the simulator's current whole track."""
        if self.track <= 17:
            return "D3"
        if self.track <= 24:
            return "D2"
        if self.track <= 30:
            return "D1"
        return "D0"

    def normalize_preview_page(self) -> None:
        """Keep the compact UI on a screen supported by installed boards."""
        if self.preview_page in ("settings", "controller_connection", "hud_connection", "log"):
            return
        if self.preview_page == "control":
            self.preview_page = "hud" if self.hud_preview_available() else "setup"
        elif self.preview_page == "hud" and not self.hud_preview_available():
            self.preview_page = "setup"
        elif self.preview_page == "setup" and self.hud_preview_available():
            self.preview_page = "hud"

    def hud_preview_available(self) -> bool:
        """Whether the standalone Monitor layout may be rendered in GUI preview."""
        return self.ub4.get() or self.ub3.get() or self.controller_gui_preview

    def controller_preview_available(self) -> bool:
        """Whether Control OneROM navigation may be rendered in this GUI only."""
        return self.ub3.get() or self.controller_gui_preview

    def toggle_simulated_board(self, board: str) -> None:
        variable = self.ub3 if board == "ub3" else self.ub4
        variable.set(not variable.get())
        if board == "ub3" and not variable.get():
            self.writable.set(False)
        self.apply_device_state()

    def show_idle(self) -> None:
        self.show_page("idle")

    def launch_physical_preview(self) -> None:
        # The physical 7-inch screen is the primary user interface.  The
        # hidden Tk root only owns this calibrated touch window.
        self.withdraw()
        self.preview_page = "settings"
        self.open_size_preview()

    def close_preview(self) -> None:
        """Persist the last usable touchscreen client size before exiting."""
        preview = getattr(self, "preview", None)
        if preview is not None and preview.winfo_exists():
            width, height = preview.winfo_width(), preview.winfo_height()
            if width >= 640 and height >= 360:
                self.drive_binding.window_size = {"width": width, "height": height}
                try:
                    save_binding(self.binding_path, self.drive_binding)
                except OSError:
                    # A failed preference write must never prevent a clean
                    # close of the hardware-monitor application.
                    pass
        self.destroy()

    def toggle_preview_fullscreen(self, _event=None) -> str:
        """Toggle a borderless Windows test view without changing saved size."""
        preview = getattr(self, "preview", None)
        if preview is not None and preview.winfo_exists():
            preview.attributes("-fullscreen", not bool(preview.attributes("-fullscreen")))
        return "break"

    def handle_preview_escape(self, _event=None) -> str:
        """Leave fullscreen first; otherwise close through geometry persistence."""
        preview = getattr(self, "preview", None)
        if preview is not None and preview.winfo_exists() and bool(preview.attributes("-fullscreen")):
            preview.attributes("-fullscreen", False)
        else:
            self.close_preview()
        return "break"

    def open_size_preview(self, _restore_workspace: bool = False, _window_size: tuple[int, int] | None = None) -> None:
        """Render the simulator without replacing its on-screen window."""
        self.normalize_preview_page()
        old_hex_entry = getattr(self, "hex_entry", None)
        if old_hex_entry is not None and old_hex_entry.winfo_exists():
            old_hex_entry.destroy()
        self.hex_entry = None
        preview = getattr(self, "preview", None)
        position: tuple[int, int] | None = None
        reusing_preview = bool(preview and preview.winfo_exists())
        if reusing_preview:
            position = (preview.winfo_x(), preview.winfo_y())
        # A VM commonly reports a generic logical DPI rather than the physical
        # monitor DPI.  The Settings values therefore take precedence.  At
        # 102.4 PPI and 100%, this is calibrated for the V226HQL.
        ppi = float(self.preview_ppi.get()) * SEVEN_INCH_BASE_SCALE
        width, height = round((155 / 25.4) * ppi), round((88 / 25.4) * ppi)
        if _window_size is not None:
            width, height = _window_size
        elif not reusing_preview:
            saved_size = self.drive_binding.window_size
            saved_width, saved_height = saved_size.get("width"), saved_size.get("height")
            if isinstance(saved_width, int) and isinstance(saved_height, int) and saved_width >= 640 and saved_height >= 360:
                width, height = saved_width, saved_height
        elif reusing_preview and getattr(self, "_preview_manual_size", False):
            # Preserve an operator's mouse/maximize resize through ordinary
            # card redraws instead of snapping back to calibrated size.
            width, height = preview.winfo_width(), preview.winfo_height()
        geometry = f"{width}x{height}"
        if position:
            geometry += f"+{position[0]}+{position[1]}"
        if not reusing_preview:
            preview = tk.Toplevel(self)
            preview.title("1541 OneROM - 7-inch Touchscreen Simulator")
            # The unused host-window area around the centered Pi HUD is a
            # neutral black letterbox, not another part of the dashboard.
            preview.configure(bg="#000000")
            # A conventional desktop window should expose Windows' maximize
            # control and permit mouse resizing. The Canvas redraw below uses
            # the actual client size, so the HUD remains proportionate.
            preview.resizable(True, True)
            preview.minsize(640, 360)
            # Match the native 1280×720 Pi/touchscreen canvas. Windows then
            # changes width and height together during edge/corner resizing
            # instead of distorting the dashboard's touch geometry.
            self.preview = preview
            canvas = tk.Canvas(preview, width=width, height=height, bg=BG, highlightthickness=0)
            self.preview_canvas = canvas
        else:
            canvas = self.preview_canvas
            # Keep the existing native surface alive. Repainting this canvas
            # is instant and avoids the white flash from window destruction.
            canvas.configure(bg=BG)
            canvas.delete("all")
        if _window_size is None:
            preview.geometry(geometry)
        self._preview_render_size = (width, height)
        # The drawing surface is always the largest centered 16:9 area in
        # the Windows client area. A maximized ultrawide or 4:3 desktop gains
        # clean dark margins instead of stretching the Pi touchscreen UI.
        if width / height >= WIDTH / HEIGHT:
            viewport_height = height
            viewport_width = round(height * WIDTH / HEIGHT)
        else:
            viewport_width = width
            viewport_height = round(width * HEIGHT / WIDTH)
        canvas.configure(width=viewport_width, height=viewport_height)
        canvas.place(x=(width - viewport_width) // 2, y=(height - viewport_height) // 2)
        sx, sy = viewport_width / WIDTH, viewport_height / HEIGHT
        def text(x, y, value, size=16, fill=TEXT, bold=False, anchor="w", tag=None, width=None):
            canvas.create_text(x*sx, y*sy, text=value, fill=fill, anchor=anchor,
                               font=("Segoe UI Semibold" if bold else "Segoe UI", max(4, round(size*sy)), "normal"), tags=tag,
                               width=width*sx if width is not None else 0)
        def box(x1, y1, x2, y2, label, value="", value_size=28, value_y=None, value_tag=None):
            canvas.create_rectangle(x1*sx, y1*sy, x2*sx, y2*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            # 26 virtual pixels equals roughly 13 physical pixels at the
            # calibrated 7-inch scale: enough breathing room for touch UI.
            text(x1+26, y1+30, label.upper(), 18, MUTED, True)
            if value:
                canvas.create_text((x1+26)*sx, (value_y if value_y is not None else y1+78)*sy, text=value, fill=TEXT, anchor="w", font=("Cascadia Mono", max(7, round(value_size*sy)), "normal"), tags=value_tag)
        def draw_spinning_disk(phase: float, center_y: int) -> None:
            """A tiny 5.25-inch floppy with deliberately subtle motion marks."""
            canvas.delete("disk")
            # The platter belongs to the Rotation card: it explains the RPM
            # reading rather than consuming a redundant standalone card.
            # With the motor stopped there is deliberately no platter glyph
            # at all: an empty area is the clearest OFF indication.
            if not self.motor:
                return
            cx, cy, radius = 860, center_y, 28
            canvas.create_oval((cx-radius)*sx, (cy-radius)*sy, (cx+radius)*sx, (cy+radius)*sy,
                               fill="#28343b", outline="#82959b", width=max(1, round(2*sy)), tags="disk")
            canvas.create_oval((cx-15)*sx, (cy-15)*sy, (cx+15)*sx, (cy+15)*sy,
                               fill="#101820", outline="#a9bbc4", width=max(1, round(sy)), tags="disk")
            canvas.create_oval((cx-4)*sx, (cy-4)*sy, (cx+4)*sx, (cy+4)*sy,
                               fill="#d5e5e9", outline="", tags="disk")
            # Three curved arrows orbit the platter only while it is
            # running. Their absence makes a stopped disk unambiguous.
            active = ACCENT if int(phase * 5) % 2 == 0 else "#76ded0"
            arrow_radius = 39
            arrow_sweep = math.radians(52)
            for offset in (0, 2 * math.pi / 3, 4 * math.pi / 3):
                start_angle = phase * 2.2 + offset
                end_angle = start_angle + arrow_sweep
                arc_points = []
                for step in range(9):
                    angle = start_angle + arrow_sweep * step / 8
                    arc_points.extend(((cx + math.cos(angle) * arrow_radius) * sx,
                                       (cy + math.sin(angle) * arrow_radius) * sy))
                canvas.create_line(*arc_points, fill=active,
                                   width=max(1, round(3*sy)), smooth=True,
                                   capstyle="round", tags="disk")
                tip_x = cx + math.cos(end_angle) * arrow_radius
                tip_y = cy + math.sin(end_angle) * arrow_radius
                tangent_x, tangent_y = -math.sin(end_angle), math.cos(end_angle)
                normal_x, normal_y = -tangent_y, tangent_x
                base_x, base_y = tip_x - tangent_x * 10, tip_y - tangent_y * 10
                canvas.create_polygon(
                    tip_x*sx, tip_y*sy,
                    (base_x + normal_x * 5)*sx, (base_y + normal_y * 5)*sy,
                    (base_x - normal_x * 5)*sx, (base_y - normal_y * 5)*sy,
                    fill=active, outline="", tags="disk",
                )
        def draw_head_motion(phase: float, center_x: int, card_top: int) -> None:
            """Use foreshortened chevrons to show head motion in depth."""
            canvas.delete("head")
            # Chevrons represent current physical travel only.  A stalled
            # head is not moving, and a parked head has no active direction.
            if self.head_var.get() not in ("IN", "OUT"):
                return
            # Add one chevron per beat.  Four downward-pointing marks make
            # depth visible without changing the physical direction glyph.
            count = int(phase * (4 / 1.5)) % 4 + 1
            for index in range(count):
                if self.head_direction == "IN":
                    y = card_top + 28 + index * 17
                    # IN approaches: small at the top, large at the bottom.
                    scale = (0.45, 0.63, 0.81, 1.0)[index]
                    points = (
                        (center_x - 20 * scale, y - 11 * scale),
                        (center_x, y + 9 * scale),
                        (center_x + 20 * scale, y - 11 * scale),
                    )
                else:
                    # OUT leaves: a large upward chevron begins at the
                    # bottom and shrinks as it moves upward into distance.
                    y = card_top + 82 - index * 17
                    scale = (1.0, 0.81, 0.63, 0.45)[index]
                    points = (
                        (center_x - 20 * scale, y + 11 * scale),
                        (center_x, y - 9 * scale),
                        (center_x + 20 * scale, y + 11 * scale),
                    )
                canvas.create_line(*(coordinate * (sx if pos % 2 == 0 else sy) for pos, coordinate in enumerate(sum((list(point) for point in points), []))),
                                   fill=ACCENT, width=max(1, round((2 + 3 * scale)*sy)), joinstyle="round", tags="head")
        text(26, 34, f"1541 OneROM {APP_VERSION}", 22, TEXT, True)
        # Context-aware top navigation.  Show destinations, never an icon
        # for the screen already open: Monitor list (☷), log (▤), setup (⚙).
        # Each icon sits inside a 56-pixel touch target even though the
        # glyph itself remains pleasantly compact.
        def top_icon(x1, glyph):
            canvas.create_rectangle(x1*sx, 14*sy, (x1 + 56)*sx, 70*sy, fill=PANEL_ALT, outline=ACCENT)
            text(x1 + 28, 42, glyph, 28, ACCENT, True, "center")
        if self.preview_page == "hud":
            top_icon(1136, "▤")
            top_icon(1200, "⚙")
        elif self.preview_page == "settings":
            top_icon(1136, "▤")
            top_icon(1200, "☷")
        elif self.preview_page == "log":
            # Log-specific actions come first; navigation remains at the
            # far right. ⌫ clears the local display history, ⧉ copies it.
            top_icon(1008, "⌫")
            top_icon(1072, "⧉")
            top_icon(1136, "☷")
            top_icon(1200, "⚙")
        elif self.preview_page in ("controller_connection", "hud_connection"):
            top_icon(1136, "☷")
            top_icon(1200, "⚙")
        if self.preview_page == "hud":
            text(26, 76, "Monitor · passive measurements · tap P# to set display priority", 16, MUTED)
            # A compact list leaves a dedicated right-hand status column.
            cards = self.scroll_hud_cards()
            max_start = max(0, len(cards) - HUD_VISIBLE_CARD_COUNT)
            self.hud_scroll_index = min(self.hud_scroll_index, max_start)
            visible = cards[self.hud_scroll_index:self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT]
            if not cards:
                canvas.create_rectangle(24*sx, HUD_CARD_TOP*sy, HUD_CARD_RIGHT*sx, 672*sy, fill=PANEL, outline=ACCENT)
                text(48, 364, "NO ONEROM CARDS AVAILABLE", 24, TEXT, True)
                text(48, 402, "Connect a Monitor OneROM for telemetry or a Control OneROM for setup cards.", 16, MUTED)
            for index, card in enumerate(visible):
                # Five compact rows retain large touch targets while exposing
                # one more operating measurement in the default viewport.
                y1 = HUD_CARD_TOP + index * HUD_CARD_PITCH
                y2 = y1 + HUD_CARD_HEIGHT
                alert = bool(card.get("alert"))
                card_color = OFFLINE if alert else ACCENT
                canvas.create_rectangle(
                    24*sx, y1*sy, HUD_CARD_RIGHT*sx, y2*sy,
                    fill=TRACK_ALERT_PANEL if alert else PANEL,
                    outline=card_color, width=1,
                    tags=f"scroll_{card['id']}_background",
                )
                text(48, y1 + 20, str(card["title"]).upper(), 14,
                     OFFLINE if alert else MUTED, True,
                     tag=f"scroll_{card['id']}_title")
                value, detail = self.scroll_card_paint_text(card)
                display_value = value if len(value) <= 24 else f"{value[:21]}..."
                # Values retain a consistent visual weight across every
                # card.  Supporting text begins well to their right.
                if card["id"] in {"recent_evidence", "sector_fifo"}:
                    text(48, y1 + 50, value, 15, TEXT, True, tag=f"scroll_{card['id']}_value")
                    text(48, y1 + 80, detail, 15, TEXT, True, tag=f"scroll_{card['id']}_detail")
                else:
                    value_color = OFFLINE if alert else TEXT
                    text(48, y1 + 64, display_value, 24, value_color, True, tag=f"scroll_{card['id']}_value")
                    text(400, y1 + 64, detail, 14, OFFLINE if alert else MUTED, True, tag=f"scroll_{card['id']}_detail")
                priority = card["priority"]
                # Help precedes the display priority, matching the natural
                # left-to-right reading order: what it means, then its rank.
                # Large, finger-friendly actions occupy the right edge of
                # every card without reducing the primary value area.
                canvas.create_rectangle(HUD_HELP_LEFT*sx, (y1 + 23)*sy, HUD_HELP_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=ACCENT)
                text((HUD_HELP_LEFT + HUD_HELP_RIGHT) / 2, y1 + 55, "?", 25, ACCENT, True, "center")
                canvas.create_rectangle(HUD_PRIORITY_LEFT*sx, (y1 + 23)*sy, HUD_PRIORITY_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=ACCENT)
                text((HUD_PRIORITY_LEFT + HUD_PRIORITY_RIGHT) / 2, y1 + 55, f"P{priority}" if priority else "P–", 18, TEXT, True, "center")
                if card["id"] == "write_protect":
                    # This is a deliberately explicit action rather than a
                    # hidden whole-card gesture: write protection matters.
                    action = "DISABLE OVERRIDE" if self._confirmed_writable else "ENABLE OVERRIDE"
                    action_color = OFFLINE if self._confirmed_writable else ACCENT
                    canvas.create_rectangle(HUD_OVERRIDE_LEFT*sx, (y1 + 23)*sy, HUD_OVERRIDE_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=action_color, tags=f"scroll_{card['id']}_action_box")
                    text((HUD_OVERRIDE_LEFT + HUD_OVERRIDE_RIGHT) / 2, y1 + 55, action, 13, action_color, True, "center", tag=f"scroll_{card['id']}_action_text")
                elif card["id"] == "track":
                    # This is a local calibration marker, never a drive
                    # command.  It mirrors the dedicated Sync control found
                    # on physical step-counting track displays.
                    canvas.create_rectangle(HUD_OVERRIDE_LEFT*sx, (y1 + 23)*sy, HUD_OVERRIDE_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=ACCENT)
                    text((HUD_OVERRIDE_LEFT + HUD_OVERRIDE_RIGHT) / 2, y1 + 55, "SYNC 18", 14, ACCENT, True, "center")
                elif card["id"] == "capture_health":
                    # Health reset is local to the diagnostic window and is
                    # safe to expose as a direct, finger-sized card action.
                    canvas.create_rectangle(HUD_OVERRIDE_LEFT*sx, (y1 + 23)*sy, HUD_OVERRIDE_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=ACCENT)
                    text((HUD_OVERRIDE_LEFT + HUD_OVERRIDE_RIGHT) / 2, y1 + 55, "CLEAR DROPS", 14, ACCENT, True, "center")
                elif card["id"] in {"startup_rom", "boot_iec"}:
                    canvas.create_rectangle(HUD_REFRESH_LEFT*sx, (y1 + 23)*sy, HUD_REFRESH_RIGHT*sx, (y1 + 87)*sy, fill=PANEL_ALT, outline=ACCENT)
                    text((HUD_REFRESH_LEFT + HUD_REFRESH_RIGHT) / 2, y1 + 55, "REFRESH", 14, ACCENT, True, "center")
            # Four finger-sized scrolling controls. The symbols intentionally
            # omit their former 5/1 labels: direction alone is clearer.
            if cards:
                for y1, label in ((100, "⇑"), (244, "↑"), (388, "↓"), (532, "⇓")):
                    canvas.create_rectangle(HUD_SCROLL_LEFT*sx, y1*sy, HUD_SCROLL_RIGHT*sx, (y1 + 140)*sy, fill=PANEL_ALT, outline=ACCENT)
                    text((HUD_SCROLL_LEFT + HUD_SCROLL_RIGHT) / 2, y1 + 70, label, 38, TEXT, True, "center")
            rotation_index = next((index for index, card in enumerate(visible) if card["id"] == "rotation"), None)
            if rotation_index is not None:
                draw_spinning_disk(time.monotonic(), HUD_CARD_TOP + rotation_index * HUD_CARD_PITCH + 55)
            head_index = next((index for index, card in enumerate(visible) if card["id"] == "head"), None)
            if head_index is not None:
                draw_head_motion(time.monotonic(), 860, HUD_CARD_TOP + head_index * HUD_CARD_PITCH)
            telemetry_label = "TELEMETRY ON" if self._hud_telemetry_enabled else "TELEMETRY WAITING"
            list_status = (f"SHOWING {self.hud_scroll_index + 1}–{min(self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT, len(cards))} OF {len(cards)}" if cards else "NO ACTIVE CARDS")
            text(24, 698, f"{list_status} · {telemetry_label} · ? HELP", 14, ACCENT, True)
            connection_status, connection_color = self.dashboard_connection_status()
            text(1256, 698, connection_status, 14, connection_color, True, "e")
            if self.hud_help_card:
                card = next((entry for entry in cards if entry["id"] == self.hud_help_card), None)
                if card:
                    # Help is a real reference panel: current reading, state,
                    # calculation/qualification notes, and provenance all fit
                    # inside one opaque boundary.
                    canvas.create_rectangle(88*sx, 112*sy, 1192*sx, 608*sy, fill=BG, outline=ACCENT, width=max(1, round(2*sy)))
                    text(128, 154, str(card["title"]).upper(), 24, TEXT, True)
                    text(128, 194, f"CURRENT: {card['value']}", 18, TEXT, True)
                    text(128, 224, f"STATUS: {card['detail']}", 15, MUTED, True)
                    # Fixed inner width prevents detailed calculations from
                    # painting through the popup boundary.
                    text(128, 266, str(card["help"]), 15, MUTED, False, "nw", width=1015)
                    source = (
                        "SOURCE: CONTROL ONEROM SETTINGS · TAP ANYWHERE TO CLOSE"
                        if card.get("kind") == "control" else
                        "SOURCE: PASSIVE MONITOR TELEMETRY · TAP ANYWHERE TO CLOSE"
                    )
                    text(128, 570, source, 15, ACCENT, True)
            if self.priority_prompt_card:
                card = next((entry for entry in cards if entry["id"] == self.priority_prompt_card), None)
                canvas.create_rectangle(330*sx, 152*sy, 950*sx, 570*sy, fill=BG, outline=ACCENT, width=max(1, round(2*sy)))
                text(640, 188, "SET DISPLAY PRIORITY", 22, TEXT, True, "center")
                text(640, 218, str(card["title"]).upper() if card else "CARD", 15, MUTED, True, "center")
                shown_value = self.priority_prompt_value or "—"
                # Keep the selected value clearly separated from the first
                # keypad row while retaining the balanced modal layout.
                text(640, 252, shown_value, 36, ACCENT, True, "center")
                # Digits are deliberately large touch targets. Two digits
                # permit priorities 0–99 and duplicate values are allowed.
                for row, digits in enumerate((("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"))):
                    for column, digit in enumerate(digits):
                        x1, y1 = 424 + column * 150, 288 + row * 58
                        canvas.create_rectangle(x1*sx, y1*sy, (x1 + 132)*sx, (y1 + 46)*sy, fill=PANEL_ALT, outline=ACCENT)
                        text(x1 + 66, y1 + 23, digit, 20, TEXT, True, "center")
                canvas.create_rectangle(574*sx, 462*sy, 706*sx, 508*sy, fill=PANEL_ALT, outline=ACCENT)
                text(640, 485, "0", 20, TEXT, True, "center")
                for x1, label, fill, foreground in (
                    (424, "CLEAR", PANEL_ALT, TEXT),
                    (574, "CLOSE", PANEL_ALT, TEXT),
                    (724, "ENTER", ACCENT, BG),
                ):
                    canvas.create_rectangle(x1*sx, 522*sy, (x1 + 132)*sx, 558*sy, fill=fill, outline=ACCENT)
                    text(x1 + 66, 540, label, 15, foreground, True, "center")
        elif self.preview_page == "settings":
            text(26, 76, "Settings", 16, MUTED)
            # Setup uses five full-width rows, matching the dashboard list.
            # Control OneROM feature previews share their own row so this
            # remains a complete no-scroll setup screen.
            setup_rows = (
                (100, "CONTROL ONEROM", "CONNECTED" if self.ub3.get() else "NOT CONNECTED", "TAP TO ASSIGN CONTROL ONEROM", ACCENT if self.ub3.get() else OFFLINE),
                (215.2, "MONITOR ONEROM", "CONNECTED" if self.ub4.get() else "NOT CONNECTED", "TAP TO ASSIGN MONITOR ONEROM", ACCENT if self.ub4.get() else OFFLINE),
            )
            for y1, title, value, detail, color in setup_rows:
                canvas.create_rectangle(24*sx, y1*sy, 1256*sx, (y1 + HUD_CARD_HEIGHT)*sy, fill=PANEL, outline=ACCENT, width=1)
                text(50, y1 + 20, title, 14, MUTED, True)
                text(50, y1 + 64, value, 24, color, True)
                text(400, y1 + 64, detail, 14, MUTED, True)

            y1 = 330.4
            canvas.create_rectangle(24*sx, y1*sy, 1256*sx, (y1 + HUD_CARD_HEIGHT)*sy, fill=PANEL, outline=ACCENT, width=1)
            text(50, y1 + 20, "CONTROL ONEROM FEATURES · GUI PREVIEW", 14, MUTED, True)
            # Fit each control to its *rendered* label, with the same 24px
            # right inset. That makes the trailing whitespace consistent,
            # even though the three labels are radically different lengths.
            option_font = tkfont.Font(family="Segoe UI Semibold", size=max(4, round(14*sy)))
            self.settings_option_bounds: dict[str, tuple[float, float]] = {}
            for option_id, x1, label, variable in (
                ("rom", 350, "ROM SELECT", self.controller_rom_enabled),
                ("iec", 620, "IEC ADDRESS", self.controller_iec_enabled),
                ("wp", 890, "WRITE PROTECT OVERRIDE", self.controller_wp_enabled),
            ):
                # Same 52-pixel action-button height and 20-pixel gaps as
                # the Control/Monitor setup actions; these need finger room.
                button_width = 58 + option_font.measure(label) / sx + 24
                self.settings_option_bounds[option_id] = (x1, x1 + button_width)
                # Leave the row title its own band; the feature actions sit
                # below it with a clear visual and touch margin.
                canvas.create_rectangle(x1*sx, (y1 + 48)*sy, (x1 + button_width)*sx, (y1 + 100)*sy, fill=PANEL_ALT, outline=ACCENT)
                canvas.create_rectangle((x1 + 14)*sx, (y1 + 60)*sy, (x1 + 42)*sx, (y1 + 88)*sy, fill=ACCENT if variable.get() else PANEL, outline=ACCENT)
                if variable.get():
                    text(x1 + 28, y1 + 74, "✓", 17, BG, True, "center")
                text(x1 + 58, y1 + 74, label, 14, TEXT, True)

            y1 = 445.6
            canvas.create_rectangle(24*sx, y1*sy, 1256*sx, (y1 + HUD_CARD_HEIGHT)*sy, fill=PANEL, outline=ACCENT, width=1)
            text(50, y1 + 20, "APPEARANCE", 14, MUTED, True)
            text(50, y1 + 64, "CUSTOMIZE DISPLAY", 24, TEXT, True)
            text(400, y1 + 64, "COLORS · CARD STYLE · TEXT · TAP TO OPEN MENU", 14, MUTED, True)
            canvas.create_polygon(1192*sx, (y1 + 36)*sy, 1232*sx, (y1 + 36)*sy, 1212*sx, (y1 + 66)*sy, fill=ACCENT, outline="")

        elif self.preview_page in ("controller_connection", "hud_connection"):
            role = "controller" if self.preview_page == "controller_connection" else "hud"
            role_title = "CONTROL ONEROM" if role == "controller" else "MONITOR ONEROM"
            serial_var = self.controller_serial if role == "controller" else self.hud_serial
            text(26, 76, "Control OneROM Setup" if role == "controller" else "Monitor OneROM Setup", 16, MUTED)
            canvas.create_rectangle(24*sx, 100*sy, 1256*sx, 211*sy, fill=PANEL, outline=ACCENT)
            text(50, 120, role_title, 14, MUTED, True)
            text(50, 164, serial_var.get() or "NO ONEROM SELECTED", 24, TEXT if serial_var.get() else OFFLINE, True)
            role_detail = (
                "SELECT THE ONEROM THAT CONTROLS ROM, IEC, AND WRITE PROTECT"
                if role == "controller" else
                "SELECT THE ONEROM THAT PROVIDES PASSIVE DRIVE TELEMETRY"
            )
            text(400, 164, role_detail, 14, MUTED, True)
            text(50, 197, "ASSIGNMENT IS SAVED BY USB SERIAL NUMBER", 13, MUTED, True)
            # Keep discovery results in an obvious, dedicated list box.
            # Refresh joins the bottom action row, rather than impersonating
            # a heading above an otherwise unexplained empty region.
            canvas.create_rectangle(24*sx, 235*sy, 1256*sx, 566*sy, fill=PANEL, outline=ACCENT)
            text(50, 258, "AVAILABLE ONEROM DEVICES", 16, MUTED, True)
            # Assigned serials remain visible in the role card above, but are
            # intentionally removed from every picker list.  Releasing the
            # role makes the physical board available again.
            # Use the saved drive binding, not the currently highlighted row.
            # A touch selection is only a pending choice until CONNECT saves
            # it; otherwise the board vanishes before it can be connected.
            assigned_serials = {
                serial for serial in (self.drive_binding.controller_serial, self.drive_binding.hud_serial) if serial
            }
            boards = [board for board in self.usb_boards.values() if board.serial_number not in assigned_serials]
            if boards:
                for index, board in enumerate(boards[:4]):
                    y = 278 + index * 60
                    selected = board.serial_number == serial_var.get()
                    canvas.create_rectangle(40*sx, y*sy, 1240*sx, (y+52)*sy, fill=ACCENT if selected else PANEL_ALT, outline=ACCENT)
                    text(50, y+26, board.serial_number, 18, BG if selected else TEXT, True)
                    text(1220, y+26, board.port, 16, BG if selected else MUTED, False, "e")
            else:
                text(50, 306, "NO UNASSIGNED ONEROM FOUND", 22, TEXT, True)
                text(50, 346, "Connect a board, then tap REFRESH DEVICES — discovered serials appear here.", 16, MUTED)
            release_notice = self._release_pending_role is not None
            text(24, 592, self.usb_status.get(), 17 if release_notice else 16,
                 TEXT if release_notice else (ACCENT if boards else MUTED), release_notice)
            # Four equal, evenly spaced actions make the connection workflow
            # clear: return, scan, release the assigned role, or connect.
            canvas.create_rectangle(24*sx, 620*sy, 314*sx, 672*sy, fill=PANEL_ALT, outline=ACCENT)
            # Segoe's triangle glyph carries extra descent; lift its anchor
            # slightly so the *visible* triangle centers in the 52px button.
            text(169, 640, "◀", 34, ACCENT, True, "center")
            for x1, label, color in (
                (338, "REFRESH DEVICES", ACCENT),
                (652, f"RELEASE {role_title}", OFFLINE),
                (966, f"CONNECT {role_title}", ACCENT),
            ):
                canvas.create_rectangle(x1*sx, 620*sy, (x1 + 290)*sx, 672*sy, fill=PANEL_ALT, outline=color)
                text(x1 + 145, 646, label, 15, color, True, "center")
        elif self.preview_page == "log":
            text(26, 70, "OneROM Activity Log", 16, MUTED)
            # The log uses the same right-edge scrolling lane as the HUD.
            canvas.create_rectangle(24*sx, 96*sy, HUD_CARD_RIGHT*sx, 672*sy, fill=PANEL, outline=PANEL_ALT)
            text(48, 112, "USB COMMUNICATIONS · HARDWARE TELEMETRY · SYSTEM EVENTS", 16, MUTED, True)
            visible_lines = 23
            max_scroll = max(0, len(self.usb_log_lines) - visible_lines)
            self.log_scroll = max(0, min(self.log_scroll, max_scroll))
            start = max(0, len(self.usb_log_lines) - visible_lines - self.log_scroll)
            end = start + visible_lines
            lines = self.usb_log_lines[start:end]
            if not lines:
                text(48, 350, "No USB communications recorded yet.", 20, MUTED)
            else:
                for index, line in enumerate(lines):
                    color = OFFLINE if "interrupted" in line or "failed" in line or "ERROR" in line else TEXT
                    text(48, 150 + index * 22, line[:136], 15, color)
            text(48, 650, f"{len(self.usb_log_lines)} entries · scroll {self.log_scroll}/{max_scroll}", 14, MUTED)
            for y1, label in ((100, "⇤"), (244, "↑"), (388, "↓"), (532, "⇥")):
                canvas.create_rectangle(HUD_SCROLL_LEFT*sx, y1*sy, HUD_SCROLL_RIGHT*sx, (y1 + 140)*sy, fill=PANEL_ALT, outline=ACCENT)
                text((HUD_SCROLL_LEFT + HUD_SCROLL_RIGHT) / 2, y1 + 70, label, 38, TEXT, True, "center")
        else:
            text(26, 76, "OneROM Setup", 16, MUTED)
            box(24, 120, 1256, 410, "No OneROM role configured", "OPEN SETTINGS")
            text(50, 330, "Open Settings to connect the Control and Monitor OneROMs for this drive.", 18, MUTED)
        # Settings shares the HUD/log's slim y=672…720 footer instead of
        # reserving a separate button bar beneath its content.
        if self.preview_page == "hud":
            footer_status = "● Monitor connected"
        elif self.preview_page == "settings":
            footer_status = "● Hardware setup"
        elif self.preview_page in ("controller_connection", "hud_connection"):
            controller_state = "connected" if self.ub3.get() else "unassigned"
            hud_state = "connected" if self.ub4.get() else "unassigned"
            footer_status = f"● Control {controller_state} · Monitor {hud_state}"
        elif self.preview_page == "log":
            footer_status = "● USB Log"
        else:
            footer_status = "● No boards configured"
        if self.preview_page not in ("hud", "log", "settings", "controller_connection", "hud_connection"):
            diagnostic = self.serial_last_error.get()
            if diagnostic:
                text(1240, 680, f"USB ERROR: {diagnostic[:88]}", 14, OFFLINE, True, "e")
            else:
                text(1240, 680, footer_status, 20, ACCENT if self.preview_page != "setup" else MUTED, True, "e")
        if self.preview_page in ("settings", "log", "controller_connection", "hud_connection"):
            connection_status, connection_color = self.dashboard_connection_status()
            text(1256, 698, connection_status, 14, connection_color, True, "e")
        if self.popup_menu is not None:
            if self.hex_entry is not None and self.hex_entry.winfo_exists():
                self.hex_entry.destroy()
                self.hex_entry = None
            x1, y1, x2, rows_y, row_height = self.popup_menu_layout()
            choices = self.popup_menu["choices"]
            canvas.create_rectangle(0, 0, width, height, fill="#081017", outline="")
            canvas.create_rectangle(x1*sx, y1*sy, x2*sx, (rows_y + len(choices) * row_height)*sy,
                                    fill=PANEL, outline=ACCENT, width=max(1, round(2*sy)))
            text(x1+28, y1+32, self.popup_menu["title"], 24, TEXT, True)
            text(x1+28, y1+62, "Tap a choice to apply it.", 16, MUTED)
            for index, choice in enumerate(choices):
                label = choice[0] if self.popup_menu["kind"] == "appearance" else choice
                row_y = rows_y + index * row_height
                canvas.create_rectangle((x1+16)*sx, (row_y+4)*sy, (x2-16)*sx, (row_y+row_height-4)*sy,
                                        fill=PANEL_ALT, outline=ACCENT)
                text(x1+42, row_y + row_height / 2, label, 17, TEXT, True)
                if self.popup_menu["kind"] == "appearance":
                    # A live swatch makes it clear which exact style is
                    # being edited before the color picker opens.
                    target = choice[1]
                    current = self.preview_color(target)
                    canvas.create_rectangle((x2-92)*sx, (row_y+14)*sy, (x2-42)*sx, (row_y+46)*sy,
                                            fill=current, outline=TEXT)
            canvas.create_rectangle((x2-182)*sx, (y1+20)*sy, (x2-24)*sx, (y1+72)*sy, fill=PANEL_ALT, outline=ACCENT)
            text(x2-103, y1+46, "CANCEL", 17, TEXT, True, "center")
        if self.color_picker is not None:
            if self.hex_entry is not None and self.hex_entry.winfo_exists():
                self.hex_entry.destroy()
                self.hex_entry = None
            picker_target = self.color_picker.removeprefix("gradient:")
            picker_title, _colors = COLOR_PALETTES[picker_target]
            canvas.create_rectangle(0, 0, width, height, fill="#081017", outline="")
            text(32, 40, f"Custom Color - {picker_title.title()}", 28, TEXT, True)
            text(32, 70, "Tap a color to apply it immediately.", 17, MUTED)
            canvas.create_rectangle(1050*sx, 30*sy, 1248*sx, 82*sy, fill=PANEL_ALT, outline=ACCENT)
            text(1149, 56, "CANCEL", 17, TEXT, True, "center")
            text(770, 56, "HEX", 13, MUTED, True, "e")

            def apply_hex_color(_event=None) -> None:
                value = self.hex_entry.get().strip().upper()
                if not value.startswith("#"):
                    value = f"#{value}"
                if len(value) == 7 and all(character in "0123456789ABCDEF" for character in value[1:]):
                    self.set_preview_color(picker_target, value)
                else:
                    self.hex_entry.configure(highlightbackground=OFFLINE, highlightcolor=OFFLINE)
                    self.hex_entry.selection_range(0, "end")

            def open_hex_keyboard(_event=None):
                self.hex_keyboard = True
                self.open_size_preview()
                return "break"

            def validate_hex_entry(proposed: str) -> bool:
                digits = proposed[1:] if proposed.startswith("#") else proposed
                return len(digits) <= 6 and all(character in "0123456789abcdefABCDEF" for character in digits)

            self.hex_entry = tk.Entry(
                canvas, font=("Cascadia Mono", max(6, round(16*sy))), justify="center",
                bg=PANEL_ALT, fg=TEXT, insertbackground=TEXT, relief="flat",
                highlightthickness=max(1, round(2*sy)), highlightbackground=ACCENT, highlightcolor=ACCENT,
                validate="key", validatecommand=(self.register(validate_hex_entry), "%P"),
            )
            self.hex_entry.insert(0, self.preview_color(picker_target).upper())
            self.hex_entry.bind("<Return>", apply_hex_color)
            self.hex_entry.bind("<Button-1>", open_hex_keyboard)
            canvas.create_window(910*sx, 56*sy, window=self.hex_entry, width=240*sx, height=52*sy)
            text(32, 100, "Custom gradient", 18, MUTED, True)
            text(1030, 100, "Hue", 18, MUTED, True)
            # Saturation runs left-to-right and brightness runs top-to-bottom,
            # matching the familiar Windows custom-color control.
            for y1 in range(120, 680, 16):
                value = 1 - (y1 - 120) / 560
                for x1 in range(24, 1000, 16):
                    saturation = (x1 - 24) / 976
                    red, green, blue = colorsys.hsv_to_rgb(self.gradient_hue, saturation, value)
                    color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
                    canvas.create_rectangle(x1*sx, y1*sy, (x1+16)*sx, (y1+16)*sy, fill=color, outline="")
            canvas.create_rectangle(24*sx, 120*sy, 1000*sx, 680*sy, outline=TEXT, width=max(1, round(2*sy)))
            for y1 in range(120, 680, 10):
                hue = (y1 - 120) / 560
                red, green, blue = colorsys.hsv_to_rgb(hue, 1, 1)
                color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
                canvas.create_rectangle(1030*sx, y1*sy, 1256*sx, (y1+10)*sy, fill=color, outline="")
            hue_y = 120 + self.gradient_hue * 560
            canvas.create_rectangle(1024*sx, (hue_y-6)*sy, 1262*sx, (hue_y+6)*sy, outline=TEXT, width=max(1, round(3*sy)))
            if self.hex_keyboard:
                canvas.create_rectangle(180*sx, 130*sy, 1100*sx, 584*sy, fill=BG, outline=ACCENT, width=max(1, round(2*sy)))
                text(220, 170, "HEX KEYPAD", 22, TEXT, True)
                key_rows = (
                    ("0", "1", "2", "3"),
                    ("4", "5", "6", "7"),
                    ("8", "9", "A", "B"),
                    ("C", "D", "E", "F"),
                    ("CLEAR", "⌫", "CANCEL", "APPLY"),
                )
                for row_index, row in enumerate(key_rows):
                    y1 = 198 + row_index * 70
                    for column, key in enumerate(row):
                        x1, key_width = 220 + column * 210, 190
                        fill = ACCENT if key == "APPLY" else PANEL_ALT
                        canvas.create_rectangle(x1*sx, y1*sy, (x1+key_width)*sx, (y1+52)*sy, fill=fill, outline=ACCENT)
                        text(x1+key_width/2, y1+26, key, 17, BG if key == "APPLY" else TEXT, True, "center")
        if self.write_protect_prompt is not None:
            enabling = self.write_protect_prompt
            # Opaque modal backdrop: no HUD control or graphic can visually
            # compete with a destructive-action confirmation.
            canvas.create_rectangle(0, 0, width, height, fill="#081017", outline="")
            canvas.create_rectangle(196*sx, 190*sy, 1084*sx, 510*sy, fill=PANEL, outline=ACCENT, width=max(1, round(2*sy)))
            text(640, 250, "Are you sure?", 34, TEXT, True, "center")
            prompt_action = "enable the writable override" if enabling else "disable the writable override"
            prompt_detail = "This will force the drive writable." if enabling else "This will restore normal write protection."
            text(640, 310, f"Do you want to {prompt_action}?", 21, TEXT, False, "center")
            text(640, 350, prompt_detail, 18, MUTED, False, "center")
            canvas.create_rectangle(340*sx, 414*sy, 600*sx, 466*sy, fill=PANEL_ALT, outline=ACCENT)
            canvas.create_rectangle(680*sx, 414*sy, 940*sx, 466*sy, fill=WARNING if enabling else ACCENT, outline=WARNING if enabling else ACCENT)
            text(470, 440, "CANCEL", 17, TEXT, True, "center")
            text(810, 440, "YES — APPLY", 17, BG, True, "center")
        if self.dashboard_setting_prompt is not None:
            prompt = self.dashboard_setting_prompt
            canvas.create_rectangle(0, 0, width, height, fill="#081017", outline="")
            canvas.create_rectangle(196*sx, 190*sy, 1084*sx, 510*sy, fill=PANEL, outline=ACCENT, width=max(1, round(2*sy)))
            text(640, 250, "Confirm setting", 34, TEXT, True, "center")
            text(640, 304, prompt["label"], 17, MUTED, True, "center")
            text(640, 346, prompt["choice"], 24, ACCENT, True, "center")
            text(640, 382, "Save this selection to the Control OneROM?", 18, MUTED, False, "center")
            canvas.create_rectangle(340*sx, 426*sy, 600*sx, 478*sy, fill=PANEL_ALT, outline=ACCENT)
            canvas.create_rectangle(680*sx, 426*sy, 940*sx, 478*sy, fill=ACCENT, outline=ACCENT)
            text(470, 452, "CANCEL", 17, TEXT, True, "center")
            text(810, 452, "YES — SAVE", 17, BG, True, "center")
        if self.clear_drops_prompt:
            canvas.create_rectangle(0, 0, width, height, fill="#081017", outline="")
            canvas.create_rectangle(196*sx, 190*sy, 1084*sx, 510*sy, fill=PANEL, outline=ACCENT, width=max(1, round(2*sy)))
            text(640, 250, "Clear Capture Drops?", 34, TEXT, True, "center")
            text(640, 310, "Reset the local ROV and QOV diagnostic window?", 21, TEXT, False, "center")
            text(640, 350, "The Monitor capture counters and telemetry will not be changed.", 18, MUTED, False, "center")
            canvas.create_rectangle(340*sx, 414*sy, 600*sx, 466*sy, fill=PANEL_ALT, outline=ACCENT)
            canvas.create_rectangle(680*sx, 414*sy, 940*sx, 466*sy, fill=ACCENT, outline=ACCENT)
            text(470, 440, "CANCEL", 17, TEXT, True, "center")
            text(810, 440, "YES — CLEAR", 17, BG, True, "center")
        def clicked(event):
            x, y = event.x / sx, event.y / sy
            if self.popup_menu is not None:
                x1, y1, x2, rows_y, row_height = self.popup_menu_layout()
                choices = self.popup_menu["choices"]
                if x2-182 <= x <= x2-24 and y1+20 <= y <= y1+72:
                    self.popup_menu = None
                    self.open_size_preview()
                    return
                for index, choice in enumerate(choices):
                    row_y = rows_y + index * row_height
                    if x1+16 <= x <= x2-16 and row_y+4 <= y <= row_y+row_height-4:
                        if self.popup_menu["kind"] == "appearance":
                            self.appearance_target = choice[1]
                            # Appearance choices go straight to the completed
                            # custom-color workflow; there is no intermediate
                            # button to press on the settings card.
                            self.color_picker = f"gradient:{self.appearance_target}"
                            self.hex_keyboard = False
                        else:
                            on_select = self.popup_menu.get("on_select")
                            if on_select is not None:
                                on_select(choice)
                            else:
                                self.popup_menu["variable"].set(choice)
                                self.control_status.set(self.popup_menu["message"].format(value=choice))
                        self.popup_menu = None
                        self.open_size_preview()
                        return
                return
            if self.color_picker is not None:
                if 1050 <= x <= 1248 and 30 <= y <= 82:
                    self.return_to_appearance_menu()
                    return
                if self.hex_keyboard:
                    key_rows = (
                        ("0", "1", "2", "3"),
                        ("4", "5", "6", "7"),
                        ("8", "9", "A", "B"),
                        ("C", "D", "E", "F"),
                        ("CLEAR", "⌫", "CANCEL", "APPLY"),
                    )
                    for row_index, row in enumerate(key_rows):
                        y1 = 198 + row_index * 70
                        for column, key in enumerate(row):
                            x1, key_width = 220 + column * 210, 190
                            if x1 <= x <= x1 + key_width and y1 <= y <= y1 + 52:
                                value = self.hex_entry.get().upper()
                                if key == "⌫":
                                    self.hex_entry.delete(max(0, len(value) - 1), "end")
                                elif key == "CLEAR":
                                    self.hex_entry.delete(0, "end")
                                    self.hex_entry.insert(0, "#")
                                elif key == "CANCEL":
                                    self.hex_keyboard = False
                                    self.open_size_preview()
                                elif key == "APPLY":
                                    apply_hex_color()
                                elif len(value) < 7:
                                    self.hex_entry.insert("end", key)
                                return
                    return
                if 1030 <= x <= 1256 and 120 <= y <= 680:
                    self.gradient_hue = max(0.0, min(1.0, (y - 120) / 560))
                    self.open_size_preview()
                    return
                if 24 <= x <= 1000 and 120 <= y <= 680:
                    saturation = (x - 24) / 976
                    value = 1 - (y - 120) / 560
                    red, green, blue = colorsys.hsv_to_rgb(self.gradient_hue, saturation, value)
                    self.set_preview_color(picker_target, f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}")
                    return
                return
            if self.dashboard_setting_prompt is not None:
                if 680 <= x <= 940 and 426 <= y <= 478:
                    self.apply_dashboard_setting()
                elif 340 <= x <= 600 and 426 <= y <= 478:
                    self.dashboard_setting_prompt = None
                self.open_size_preview()
                return
            if self.clear_drops_prompt:
                if 680 <= x <= 940 and 414 <= y <= 466:
                    self.clear_diagnostic_drops()
                self.clear_drops_prompt = False
                self.open_size_preview()
                return
            if self.write_protect_prompt is not None:
                if 680 <= x <= 940 and 414 <= y <= 466:
                    self.apply_write_protect_toggle(self.write_protect_prompt)
                    self.write_protect_prompt = None
                elif 340 <= x <= 600 and 414 <= y <= 466:
                    self.write_protect_prompt = None
                self.open_size_preview()
                return
            if self.preview_page == "hud" and self.priority_prompt_card is not None:
                selected_digit: str | None = None
                for row, digits in enumerate((("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"))):
                    for column, digit in enumerate(digits):
                        x1, y1 = 424 + column * 150, 288 + row * 58
                        if x1 <= x <= x1 + 132 and y1 <= y <= y1 + 46:
                            selected_digit = digit
                if 574 <= x <= 706 and 462 <= y <= 508:
                    selected_digit = "0"
                if selected_digit is not None and len(self.priority_prompt_value) < 2:
                    self.priority_prompt_value += selected_digit
                elif 424 <= x <= 556 and 522 <= y <= 558:
                    self.priority_prompt_value = ""
                elif 574 <= x <= 706 and 522 <= y <= 558:
                    self.priority_prompt_card = None
                    self.priority_prompt_value = ""
                elif 724 <= x <= 856 and 522 <= y <= 558:
                    # Clear + Enter explicitly removes a display priority;
                    # 0 is a convenient keypad synonym for the same result.
                    priority = int(self.priority_prompt_value) if self.priority_prompt_value else None
                    self.hud_card_priorities[self.priority_prompt_card] = priority or None
                    self.save_hud_priorities()
                    self.hud_scroll_index = 0
                    self.refresh_hud_subscription()
                    self.priority_prompt_card = None
                    self.priority_prompt_value = ""
                self.open_size_preview()
                return
            if self.preview_page == "hud" and self.hud_help_card is not None:
                self.hud_help_card = None
                self.open_size_preview()
                return
            if self.preview_page in ("hud", "settings", "log", "controller_connection", "hud_connection") and 14 <= y <= 70:
                if self.preview_page == "log" and 1008 <= x <= 1064:
                    self.clear_usb_log()
                    self.open_size_preview()
                    return
                if self.preview_page == "log" and 1072 <= x <= 1128:
                    self.copy_usb_log()
                    return
                if 1136 <= x <= 1192:
                    self.preview_page = "log" if self.preview_page not in ("log", "controller_connection", "hud_connection") else "hud"
                elif 1200 <= x <= 1256:
                    self.preview_page = "hud" if self.preview_page == "settings" else "settings"
                else:
                    return
                self.open_size_preview()
                return
            elif self.preview_page == "log":
                visible_lines = 23
                max_scroll = max(0, len(self.usb_log_lines) - visible_lines)
                if HUD_SCROLL_LEFT <= x <= HUD_SCROLL_RIGHT:
                    if 100 <= y <= 240:
                        self.log_scroll = max_scroll
                    elif 244 <= y <= 384:
                        self.log_scroll = min(max_scroll, self.log_scroll + visible_lines)
                    elif 388 <= y <= 528:
                        self.log_scroll = max(0, self.log_scroll - visible_lines)
                    elif 532 <= y <= 672:
                        self.log_scroll = 0
                self.open_size_preview()
                return
            elif self.preview_page in ("controller_connection", "hud_connection"):
                role = "controller" if self.preview_page == "controller_connection" else "hud"
                serial_var = self.controller_serial if role == "controller" else self.hud_serial
                if 24 <= x <= 314 and 620 <= y <= 672:
                    self.preview_page = "settings"
                elif 338 <= x <= 628 and 620 <= y <= 672:
                    self.refresh_usb_boards()
                elif 652 <= x <= 942 and 620 <= y <= 672:
                    self.request_release_role_binding(role)
                elif 966 <= x <= 1256 and 620 <= y <= 672:
                    self.connect_assigned_boards(role)
                elif 40 <= x <= 1240 and 278 <= y <= 510:
                    index = int((y - 278) // 60)
                    assigned_serials = {
                        serial for serial in (self.drive_binding.controller_serial, self.drive_binding.hud_serial) if serial
                    }
                    boards = [board for board in self.usb_boards.values() if board.serial_number not in assigned_serials]
                    if 0 <= index < len(boards) and y <= 278 + index * 60 + 52:
                        serial_var.set(boards[index].serial_number)
                        role_name = "CONTROL ONEROM" if role == "controller" else "MONITOR ONEROM"
                        self.usb_status.set(f"Selected {boards[index].serial_number}. Tap CONNECT {role_name} to save and open the link.")
                self.open_size_preview()
                return
            elif self.preview_page == "setup" and 24 <= x <= 1256 and 120 <= y <= 410:
                self.preview_page = "settings"
            elif self.preview_page == "settings" and 24 <= x <= 1256 and 100 <= y <= 211:
                self.open_desktop_connection_setup("controller")
                return
            elif self.preview_page == "settings" and 24 <= x <= 1256 and 215 <= y <= 326:
                self.open_desktop_connection_setup("hud")
                return
            elif self.preview_page == "settings" and 24 <= x <= 1256 and 445 <= y <= 556:
                self.choose_appearance_menu(event)
                return
            elif self.preview_page == "hud" and HUD_SCROLL_LEFT <= x <= HUD_SCROLL_RIGHT:
                if 100 <= y <= 240:
                    self.scroll_hud_by(-HUD_VISIBLE_CARD_COUNT)
                elif 244 <= y <= 384:
                    self.scroll_hud_by(-1)
                elif 388 <= y <= 528:
                    self.scroll_hud_by(1)
                elif 532 <= y <= 672:
                    self.scroll_hud_by(HUD_VISIBLE_CARD_COUNT)
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 24 <= x <= HUD_CARD_RIGHT and 100 <= y <= 672:
                visible = self.scroll_hud_cards()[self.hud_scroll_index:self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT]
                index = int((y - HUD_CARD_TOP) // HUD_CARD_PITCH)
                if 0 <= index < len(visible):
                    card = visible[index]
                    row_y = HUD_CARD_TOP + index * HUD_CARD_PITCH
                    # Do not let the narrow visual gap between cards act as
                    # part of the preceding row's touch target.
                    if y > row_y + HUD_CARD_HEIGHT:
                        self.open_size_preview()
                        return
                    # Help and priority remain available on every card.
                    # Elsewhere, a control card is its own touch target and
                    # opens its focused configuration popup.
                    if HUD_HELP_LEFT <= x <= HUD_HELP_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.hud_help_card = str(card["id"])
                    elif HUD_PRIORITY_LEFT <= x <= HUD_PRIORITY_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.priority_prompt_card = str(card["id"])
                        priority = card["priority"]
                        self.priority_prompt_value = str(priority) if priority is not None else ""
                    elif card["id"] == "track" and HUD_OVERRIDE_LEFT <= x <= HUD_OVERRIDE_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.sync_track_to_18()
                    elif card["id"] == "write_protect" and HUD_OVERRIDE_LEFT <= x <= HUD_OVERRIDE_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.confirm_write_protect_toggle()
                    elif card["id"] == "capture_health" and HUD_OVERRIDE_LEFT <= x <= HUD_OVERRIDE_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.confirm_clear_diagnostic_drops()
                    elif card["id"] in {"startup_rom", "boot_iec"} and HUD_REFRESH_LEFT <= x <= HUD_REFRESH_RIGHT and row_y + 23 <= y <= row_y + 87:
                        self.refresh_control_startup_settings()
                    elif card.get("kind") == "control" and card["id"] != "write_protect":
                        self.choose_dashboard_control(str(card["id"]), event)
                    self.open_size_preview()
                    return
            elif self.preview_page == "settings" and 378 <= y <= 430:
                option_bounds = getattr(self, "settings_option_bounds", {})
                if option_bounds.get("rom", (0, 0))[0] <= x <= option_bounds.get("rom", (0, 0))[1]:
                    self.controller_rom_enabled.set(not self.controller_rom_enabled.get())
                elif option_bounds.get("iec", (0, 0))[0] <= x <= option_bounds.get("iec", (0, 0))[1]:
                    self.controller_iec_enabled.set(not self.controller_iec_enabled.get())
                elif option_bounds.get("wp", (0, 0))[0] <= x <= option_bounds.get("wp", (0, 0))[1]:
                    self.controller_wp_enabled.set(not self.controller_wp_enabled.get())
                    if not self.controller_wp_enabled.get():
                        self.writable.set(False)
                self.save_control_features()
            self.open_size_preview()
        # Bind directly to the drawing surface.  On some Windows/Tk builds a
        # Canvas does not reliably forward touch/mouse events to its Toplevel.
        canvas.bind("<Button-1>", clicked)
        preview.bind("<F11>", self.toggle_preview_fullscreen)
        canvas.bind("<Motion>", lambda event: self.update_preview_tooltip(event, canvas, sx, sy))
        canvas.bind("<Leave>", lambda _event: canvas.delete("icon_tooltip"))
        preview.bind("<Escape>", self.handle_preview_escape)
        preview.protocol("WM_DELETE_WINDOW", self.close_preview)
        def resized(event):
            if event.widget is not preview or event.width < 640 or event.height < 360:
                return
            size = (event.width, event.height)
            if size == getattr(self, "_preview_render_size", None):
                return
            self._preview_manual_size = True
            pending = getattr(self, "_preview_resize_after", None)
            if pending is not None:
                try:
                    self.after_cancel(pending)
                except tk.TclError:
                    pass
            self._preview_resize_after = self.after(
                60, lambda current=size: self.open_size_preview(_window_size=current)
            )
        preview.bind("<Configure>", resized)
        previous_animation = getattr(self, "_preview_animation_id", None)
        if previous_animation is not None:
            try:
                preview.after_cancel(previous_animation)
            except tk.TclError:
                pass
            self._preview_animation_id = None
        if self.preview_page == "hud":
            def animate_disk() -> None:
                if self.preview is preview and preview.winfo_exists() and self.preview_page == "hud":
                    # A modal must remain the topmost graphic.  Stop and
                    # erase decorative HUD animation for every popup type;
                    # otherwise a later animation tick can draw over a
                    # perfectly good dialog. Naturally Tk will happily do
                    # exactly that unless we tell it otherwise.
                    modal_open = any((
                        self.hud_help_card is not None,
                        self.priority_prompt_card is not None,
                        self.write_protect_prompt is not None,
                        self.clear_drops_prompt,
                        self.dashboard_setting_prompt is not None,
                        self.popup_menu is not None,
                        self.color_picker is not None,
                    ))
                    if not modal_open:
                        cards = self.scroll_hud_cards()
                        visible = cards[self.hud_scroll_index:self.hud_scroll_index + HUD_VISIBLE_CARD_COUNT]
                        rotation_index = next((index for index, card in enumerate(visible) if card["id"] == "rotation"), None)
                        if rotation_index is not None:
                            draw_spinning_disk(time.monotonic(), HUD_CARD_TOP + rotation_index * HUD_CARD_PITCH + 55)
                        else:
                            canvas.delete("disk")
                        head_index = next((index for index, card in enumerate(visible) if card["id"] == "head"), None)
                        if head_index is not None:
                            draw_head_motion(time.monotonic(), 860, HUD_CARD_TOP + head_index * HUD_CARD_PITCH)
                        else:
                            canvas.delete("head")
                    else:
                        canvas.delete("disk")
                        canvas.delete("head")
                    self._preview_animation_id = preview.after(180, animate_disk)
            self._preview_animation_id = preview.after(180, animate_disk)

    def register_input(self, _event=None) -> None:
        if self.screensaver:
            self.show_page(self.default_page())
        self.last_input = time.monotonic()

    def tick(self) -> None:
        if self.ub4.get() and self.motor:
            self.sector = self.sector % 17 + 1
            self.sector_var.set(f"{self.sector:02d}")
        if not self.screensaver and self.current_page != "settings" and time.monotonic() - self.last_input >= self.idle_seconds.get():
            self.show_idle()
        self.after(100, self.tick)

    def poll_serial_loop(self) -> None:
        """Service CDC data and the physical-device presence check.

        This callback remains active while the Canvas preview owns the visible
        UI.  Keeping the two-second enumeration check here avoids relying on
        the independent animation/idle timer to notice a USB removal.
        """
        self.poll_usb_telemetry()
        self.poll_controller_feedback()
        now = time.monotonic()
        if now >= self._next_usb_presence_check:
            self._next_usb_presence_check = now + 2.0
            self.check_connected_board_presence()
        self.after(20, self.poll_serial_loop)


def capture_raw_cdc(port: str, output: Path, duration_seconds: float = 30.0) -> None:
    """Capture raw CDC bytes for bench debugging without a second script."""
    if serial is None:
        raise RuntimeError("pyserial is not installed")
    chunks: list[bytes] = []
    with serial.Serial(port, BAUD_RATE, timeout=0.2) as device:
        device.dtr = device.rts = False
        time.sleep(0.2)
        device.dtr = True
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            chunk = device.read(4096)
            if chunk:
                chunks.append(chunk)
    data = b"".join(chunks)
    output.write_bytes(data)
    print(data.decode("utf-8", errors="replace"))
    print(f"Captured {len(data)} bytes to {output}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--capture":
        if len(sys.argv) not in (3, 4):
            raise SystemExit("Usage: 1541_touchscreen_simulator.py --capture COMx [output-file]")
        capture_raw_cdc(sys.argv[2], Path(sys.argv[3]) if len(sys.argv) == 4 else Path("ub4-raw-capture.txt"))
    else:
        TouchSimulator().mainloop()
