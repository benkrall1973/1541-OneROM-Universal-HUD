#!/usr/bin/env python3
"""1541 OneROM Universal GUI V1.0.0-GUI_converge01.

This combines the V1 standalone USB Selector and DriveHUD user interfaces
without changing either board's firmware or USB wire protocol.  Windows COM
numbers are treated as temporary: saved bindings use the USB descriptor serial
number exposed by pyserial instead.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Nuitka one-file builds unpack Tcl/Tk beside the executable payload.  Python's
# normal installation lookup is unavailable there, so point Tkinter at the
# bundled libraries before importing it.  This is ignored when running source.
if "__compiled__" in globals():
    _bundle_root = Path(__file__).resolve().parent
    os.environ.setdefault("TCL_LIBRARY", str(_bundle_root / "tcl" / "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", str(_bundle_root / "tk" / "tk8.6"))

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:
    raise SystemExit("pyserial is required: py -m pip install -r requirements.txt") from exc

APP_VERSION = "V1.0.0-GUI_converge01"
BAUD = 115200
ROM_PREFIX = "$ROMTEST,"
DEFAULT_GEOMETRY = "1240x840"


@dataclass(frozen=True)
class PortInfo:
    device: str
    serial_number: str
    description: str
    vid_pid: str


def enumerate_ports() -> list[PortInfo]:
    ports: list[PortInfo] = []
    for port in list_ports.comports():
        serial_number = (port.serial_number or "").strip()
        vid_pid = ""
        if port.vid is not None and port.pid is not None:
            vid_pid = f"{port.vid:04X}:{port.pid:04X}"
        ports.append(PortInfo(port.device, serial_number, port.description or "", vid_pid))
    return sorted(ports, key=lambda item: item.device)


class SerialLines:
    """One CDC port, with a reader thread and a queue consumed by Tk."""
    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.ser: serial.Serial | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def connect(self, port: str, pulse_dtr: bool = False) -> None:
        self.disconnect()
        if not pulse_dtr:
            # Keep UB3 byte-for-byte equivalent to the opening behavior in
            # the hardware-proven standalone USB Selector.
            self.ser = serial.Serial(port=port, baudrate=BAUD, timeout=0.15, write_timeout=1.0)
            self.stop.clear()
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
            self.events.put(("status", f"Connected to {port} at {BAUD}"))
            return
        candidate = serial.Serial()
        candidate.port, candidate.baudrate, candidate.timeout, candidate.write_timeout = port, BAUD, 0.15, 1
        # UB4's telemetry firmware intentionally needs a fresh CDC/DTR edge to
        # resend its cached STATE snapshot.  UB3's proven selector connection
        # does not use that pulse; changing DTR there can suppress its command
        # response path on some Windows USB stacks.
        candidate.dtr = False
        candidate.rts = False
        candidate.open()
        self.ser = candidate
        self.stop.clear()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.events.put(("status", f"Connected to {port} at {BAUD}"))

    def assert_dtr(self) -> None:
        if self.ser:
            self.ser.dtr = True

    def disconnect(self) -> None:
        self.stop.set()
        if self.ser:
            try:
                self.ser.dtr = False
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def send(self, line: str) -> None:
        if not self.connected or not self.ser:
            raise RuntimeError("Not connected")
        self.ser.write((line.strip() + "\r\n").encode("ascii"))
        self.ser.flush()
        self.events.put(("tx", line.strip()))

    def _read(self) -> None:
        assert self.ser is not None
        buffer = bytearray()
        while not self.stop.is_set() and self.ser and self.ser.is_open:
            try:
                data = self.ser.read(256)
            except Exception as exc:
                self.events.put(("error", str(exc)))
                return
            if not data:
                continue
            buffer.extend(data)
            while b"\n" in buffer:
                raw, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                self.events.put(("rx", raw.rstrip(b"\r").decode("utf-8", "replace")))


class ActivePanel(ttk.Frame):
    """The existing V1 ROM/IEC/write-protect workflow, hosted in a tab."""
    def __init__(self, parent: tk.Misc, state_changed) -> None:
        super().__init__(parent, padding=10)
        self.state_changed = state_changed
        self.connection = SerialLines()
        self.port_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Disconnected")
        self.firmware_var = tk.StringVar(value="Not queried")
        self.saved_var = tk.StringVar(value="Unknown")
        self.iec_var = tk.StringVar(value="Unknown")
        self.iec_choice = tk.StringVar(value="8")
        self.wp_var = tk.StringVar(value="Unknown")
        self.wp_enabled = tk.BooleanVar(value=False)
        self.selected_var = tk.StringVar(value="None")
        self.last_var = tk.StringVar(value="No operation yet")
        self.rows: dict[int, str] = {}
        self.listing, self.list_count, self.next_slot = False, 0, 0
        self.pending_wp: bool | None = None
        self.wp_available = self.wp_confirmed_on = False
        self.connected_port: str | None = None
        self.retry_after = 0.0
        self._build()
        # SerialLines reads UB3 on a worker thread.  Consume those queued
        # replies on Tk's UI thread just as the standalone Selector does.
        self.after(60, self.poll)

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="1541 OneROM USB - Persistent ROM Selection", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Label(header, text="Build with ChatGPT", font=("Segoe UI", 15, "bold")).pack(side="right")
        ttk.Label(self, text="Saves startup ROM (NV0) and boot IEC address (NV2). A power-cycle/reset applies saved values.", wraplength=900).pack(anchor="w", pady=(2, 8))
        bar = ttk.LabelFrame(self, text="OneROM USB CDC", padding=8); bar.pack(fill="x")
        ttk.Label(bar, text="Connection:").pack(side="left")
        ttk.Label(bar, textvariable=self.connection_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        ttk.Label(bar, text="Managed automatically in Options.").pack(side="left", padx=12)
        body = ttk.Panedwindow(self, orient="horizontal"); body.pack(fill="both", expand=True, pady=10)
        left = ttk.Frame(body); body.add(left, weight=3)
        test = ttk.LabelFrame(left, text="Test Firmware", padding=8); test.pack(fill="x")
        ttk.Label(test, text="Reported firmware:").pack(side="left"); ttk.Label(test, textvariable=self.firmware_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        state = ttk.LabelFrame(left, text="ROM State", padding=8); state.pack(fill="x", pady=(8, 0))
        ttk.Label(state, text="Persistent startup ROM:").grid(row=0, column=0, sticky="w"); ttk.Label(state, textvariable=self.saved_var, font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky="w", padx=8)
        self.save_button = ttk.Button(state, text="Save Selected ROM", command=self.save_rom, state="disabled"); self.save_button.grid(row=0, column=2, padx=8)
        ttk.Label(state, text="Selected in GUI:").grid(row=1, column=0, sticky="w", pady=(5, 0)); ttk.Label(state, textvariable=self.selected_var).grid(row=1, column=1, sticky="w", padx=8, pady=(5, 0)); state.columnconfigure(1, weight=1)
        slots = ttk.LabelFrame(left, text="Available ROM Slots", padding=8); slots.pack(fill="both", expand=True, pady=(8, 0))
        self.tree = ttk.Treeview(slots, columns=("slot", "name", "selectable"), show="headings", selectmode="browse")
        for key, text, width in (("slot", "Slot", 55), ("name", "Name", 250), ("selectable", "Selectable", 95)):
            self.tree.heading(key, text=text); self.tree.column(key, width=width, stretch=key == "name", anchor="center" if key != "name" else "w")
        self.tree.pack(fill="both", expand=True); self.tree.bind("<<TreeviewSelect>>", lambda _: self.selection_changed())
        iec = ttk.LabelFrame(left, text="IEC Address", padding=8); iec.pack(fill="x", pady=(8, 0))
        ttk.Label(iec, text="Saved boot IEC address (NV2):").grid(row=0, column=0, sticky="w"); ttk.Label(iec, textvariable=self.iec_var, font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(iec, text="Set boot IEC address:").grid(row=1, column=0, sticky="w", pady=(5, 0)); ttk.Combobox(iec, textvariable=self.iec_choice, values=("8", "9", "10", "11"), width=6, state="readonly").grid(row=1, column=1, sticky="w", padx=8, pady=(5, 0))
        self.iec_button = ttk.Button(iec, text="Save IEC Address", command=self.save_iec, state="disabled"); self.iec_button.grid(row=1, column=2, padx=8, pady=(5, 0)); iec.columnconfigure(1, weight=1)
        wp = ttk.LabelFrame(left, text="Write-Protect Override (X2)", padding=8); wp.pack(fill="x", pady=(8, 0))
        ttk.Label(wp, text="Drive protection:").pack(side="left"); ttk.Label(wp, textvariable=self.wp_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        self.wp_box = ttk.Checkbutton(wp, text="Forces Writable", variable=self.wp_enabled, command=self.toggle_wp, state="disabled"); self.wp_box.pack(side="right")
        right = ttk.LabelFrame(body, text="Test Log", padding=8); body.add(right, weight=2)
        self.log = tk.Text(right, height=20, state="disabled", wrap="word", font=("Consolas", 9)); self.log.pack(fill="both", expand=True)
        bottom = ttk.Frame(self); bottom.pack(fill="x"); ttk.Label(bottom, text="Last operation:").pack(side="left"); ttk.Label(bottom, textvariable=self.last_var).pack(side="left", padx=6); self.refresh_button = ttk.Button(bottom, text="Refresh Device", command=self.request_refresh, state="disabled"); self.refresh_button.pack(side="right")
        # USB port selection belongs to the Options tab.  This panel is only
        # given the correct port after serial-number binding has identified it.

    def refresh_ports(self) -> None:
        pass

    def connect_port(self, port: str) -> None:
        if self.connection.connected and self.connected_port == port:
            return
        if self.connection.connected:
            self.toggle()
        self.port_var.set(port)
        if not self.connection.connected: self.toggle()

    def toggle(self) -> None:
        if self.connection.connected:
            self.connection.disconnect(); self.connected_port = None; self.connection_var.set("Disconnected"); self.reset_disconnected_state(); self.state_changed(); return
        if not self.port_var.get(): messagebox.showerror("No COM port", "Select a USB CDC port first."); return
        try:
            self.connection.connect(self.port_var.get()); self.connected_port = self.port_var.get(); self.connection_var.set("Connected"); self.after(300, self.request_refresh)
        except Exception as exc: messagebox.showerror("Connection failed", str(exc))
        self.state_changed()

    def reset_disconnected_state(self) -> None:
        self.listing = False
        self.firmware_var.set("Not queried (disconnected)")
        self.saved_var.set("Not queried (disconnected)")
        self.iec_var.set("Not queried (disconnected)")
        self.wp_var.set("Not queried (disconnected)")
        self.wp_available = False
        self.wp_confirmed_on = False
        self.wp_enabled.set(False)
        self.set_controls(False)

    def set_controls(self, enabled: bool) -> None:
        self.iec_button.config(state="normal" if enabled else "disabled"); self.refresh_button.config(state="normal" if enabled else "disabled"); self.wp_box.config(state="normal" if enabled and self.wp_available else "disabled"); self.selection_changed()

    def request_refresh(self) -> None:
        if not self.connection.connected: return
        self.rows.clear(); [self.tree.delete(item) for item in self.tree.get_children()]
        self.firmware_var.set("Querying..."); self.saved_var.set("Querying..."); self.iec_var.set("Querying..."); self.wp_var.set("Querying..."); self.listing = True
        try: self.connection.send("ROMTEST?")
        except Exception as exc: self.write_log(str(exc))

    def connection_failed(self, _reason: str) -> None:
        """Release a failed handle and retry the already-routed port quietly."""
        self.connection.disconnect()
        self.connection_var.set("Connection lost — retrying...")
        self.last_var.set("USB connection lost; retrying automatically")
        self.set_controls(False)
        self.retry_after = time.monotonic() + 3.0

    def retry_connection(self) -> None:
        if self.connection.connected or not self.connected_port or time.monotonic() < self.retry_after:
            return
        try:
            self.connection.connect(self.connected_port)
        except Exception:
            self.connection_var.set("Reconnecting...")
            self.retry_after = time.monotonic() + 3.0
            return
        self.connection_var.set("Connected")
        self.last_var.set("USB connection restored")
        self.after(300, self.request_refresh)

    def selected_slot(self) -> int | None:
        item = self.tree.selection()
        if not item: return None
        values = self.tree.item(item[0], "values")
        return int(values[0]) if values and values[2] == "Yes" else None

    def selection_changed(self) -> None:
        slot = self.selected_slot(); self.selected_var.set(f"Slot {slot} - {self.rows.get(slot, '')}" if slot is not None else "None"); self.save_button.config(state="normal" if slot is not None and self.connection.connected else "disabled")

    def save_rom(self) -> None:
        slot = self.selected_slot()
        if slot is None: return
        if messagebox.askyesno("Save startup ROM", f"Save Slot {slot} to NV0? This does not live-switch the running drive."):
            self.connection.send(f"ROMSET={slot}"); self.last_var.set(f"Saving slot {slot}...")

    def save_iec(self) -> None:
        address = int(self.iec_choice.get())
        if messagebox.askyesno("Save boot IEC address", f"Save device {address} to NV2? The running drive address is unchanged."):
            self.connection.send(f"ROMIEC={address}"); self.last_var.set(f"Saving IEC address {address}...")

    def toggle_wp(self) -> None:
        requested = bool(self.wp_enabled.get()); self.wp_enabled.set(self.wp_confirmed_on)
        if requested and not messagebox.askyesno("Enable write-protect override", "Force the 1541 writable? Use only with a sacrificial disk during testing."): return
        self.connection.send("ROMWP=ON" if requested else "ROMWP=OFF"); self.last_var.set("Changing write-protect override...")

    @staticmethod
    def fields(line: str) -> tuple[str, dict[str, str]]:
        values: dict[str, str] = {}; parts = line[len(ROM_PREFIX):].split(",")
        for part in parts[1:]:
            if "=" in part: key, value = part.split("=", 1); values[key] = value
            elif part: values["_status"] = part
        return (parts[0] if parts else ""), values

    def handle(self, line: str) -> None:
        kind, fields = self.fields(line)
        if kind == "INFO": self.firmware_var.set(fields.get("VERSION", "unknown") + (" (ready)" if fields.get("READY") == "1" else "")); self.connection.send("ROMLIST")
        elif kind == "LIST": self.list_count = int(fields.get("COUNT", "0")); self.next_slot = 0; self.connection.send("ROMSLOT=0" if self.list_count else "ROMGET")
        elif kind == "SLOT":
            slot = int(fields.get("INDEX", "-1")); name = re.search(r",NAME=(.*)$", line); self.rows[slot] = name.group(1) if name else "(unnamed)"; self.tree.insert("", "end", values=(slot, self.rows[slot], "Yes" if fields.get("SELECTABLE") == "1" else "No")); self.next_slot = slot + 1; self.connection.send(f"ROMSLOT={self.next_slot}" if self.next_slot < self.list_count else "ROMGET")
        elif kind == "SAVED":
            slot = int(fields.get("SLOT", "255")); self.saved_var.set(f"Slot {slot} - {self.rows.get(slot, '')}" if fields.get("VALID") == "1" else f"Invalid/unset value {slot}"); self.set_iec(fields); self.listing = False; self.connection.send("ROMWP?")
        elif kind == "IEC": self.set_iec({"BOOT_IEC": fields.get("VERIFY", fields.get("ADDRESS", "")), "BOOT_IEC_VALID": fields.get("VALID", "")}); self.last_var.set("IEC address read-back verified" if fields.get("_status") == "OK" else line)
        elif kind == "SET":
            saved_slot, verified_slot = fields.get("SLOT"), fields.get("VERIFY")
            if fields.get("_status") == "OK" and saved_slot == verified_slot:
                try:
                    slot = int(saved_slot)
                except (TypeError, ValueError):
                    slot = -1
                self.saved_var.set(f"Slot {slot} - {self.rows.get(slot, '')}" if slot >= 0 else "Saved (verified)")
                self.set_iec(fields)
                self.last_var.set("ROM save read-back verified")
            else:
                self.last_var.set(f"ROM save failed: {line}")
        elif kind == "WP":
            self.wp_available = fields.get("AVAILABLE") == "1"; self.wp_confirmed_on = fields.get("STATE") == "ON"; self.wp_enabled.set(self.wp_confirmed_on); self.wp_var.set("ON — forces writable" if self.wp_confirmed_on else ("OFF — normal protection" if self.wp_available else fields.get("ERROR", "Unavailable / X2 in use"))); self.set_controls(self.connection.connected)

    def set_iec(self, fields: dict[str, str]) -> None:
        address = fields.get("BOOT_IEC", ""); self.iec_var.set(f"Device {address}" if fields.get("BOOT_IEC_VALID") == "1" else (f"Invalid/unset value {address}" if address else "Not reported"))

    def write_log(self, text: str) -> None:
        self.log.config(state="normal"); self.log.insert("end", text + "\n"); self.log.see("end"); self.log.config(state="disabled")

    def poll(self) -> None:
        try:
            # A busy USB device can continually refill this queue.  Process a
            # bounded batch so Tk always regains control to repaint and handle
            # clicks.
            for _ in range(100):
                kind, text = self.connection.events.get_nowait(); self.write_log(("TX " if kind == "tx" else "RX " if kind == "rx" else "") + text)
                if kind == "rx" and text.startswith(ROM_PREFIX): self.handle(text)
                if kind == "error": self.connection_failed(text)
        except queue.Empty: pass
        self.retry_connection()
        self.state_changed(); self.after(60, self.poll)


class HUDPanel(ttk.Frame):
    """V1 HUD presentation and parser; it receives the unchanged UB4 stream."""
    motor_re = re.compile(r"MOTOR\s+state=(\d+)"); phase_re = re.compile(r"PHASE\s+old=(\d+)\s+new=(\d+)\s+delta=(\d+)\s+motor=(\d+)")
    track_re = re.compile(r"TRACK_WRITE\s+addr=\$0022\s+data=\$[0-9A-Fa-f]+\s+\((\d+)\)"); density_re = re.compile(r"DENSITY\s+state=(\d+)"); wp_re = re.compile(r"WRITE_PROTECT\s+state=(\d+)")
    rpm_re = re.compile(r"RPM\s+(?:T0\.0\.11|T0\.0\.16|V[0-9.]+(?:-RC\d+)?)\s+T=(\d+)\s+REV=(\d+)\s+RPM=(\d+\.\d{2})")
    hdr_re = re.compile(r"HDRPHY\s+(?:T0\.0\.11|T0\.0\.16|V[0-9.]+(?:-RC\d+)?)\s+T=(\d+)\s+S=(\d+)"); sync_re = re.compile(r"SYNC\s+(?:T0\.0\.15|T0\.0\.16|V[0-9.]+(?:-RC\d+)?)\s+COUNT=(\d+)")
    def __init__(self, parent: tk.Misc, state_changed) -> None:
        super().__init__(parent, padding=10); self.state_changed = state_changed; self.connection = SerialLines(); self.port_var = tk.StringVar(); self.connection_var = tk.StringVar(value="Disconnected"); self.connected_port: str | None = None; self.retry_after = 0.0
        self.track_var, self.motor_var, self.head_var, self.wp_var, self.density_var = [tk.StringVar(value=value) for value in ("--.-", "OFF", "PARK", "--", "D2")]
        self.home_var = tk.StringVar(value="HOME: waiting for $0022 = 1"); self.firmware_var = tk.StringVar(value="Firmware: 1541HUD V1.0.0 — OneROM v0.7.3"); self.rpm_var = tk.StringVar(value="---.--"); self.rpm_state_var = tk.StringVar(value="NO SAMPLE"); self.sector_var = tk.StringVar(value="--"); self.sync_var = tk.StringVar(value="--")
        self.pos2: int | None = None; self.motor = False; self.direction = 0; self.last_move = 0.0; self.last_phase_at: float | None = None; self.track_candidate: int | None = None; self.track_candidate_after: str | None = None; self.sectors: list[int] = []; self.wp_candidate: str | None = None; self.wp_reported: str | None = None; self._build(); self.after(30, self.poll); self.after(100, self.tick)

    def _build(self) -> None:
        top = ttk.Frame(self); top.pack(fill="x"); ttk.Label(top, text="1541HUD — OneROM V1.0.0", font=("Segoe UI", 15, "bold")).pack(side="left"); ttk.Label(top, textvariable=self.connection_var, font=("Segoe UI", 10, "bold")).pack(side="right")
        ttk.Label(self, text="USB connection is managed automatically in Options.").pack(anchor="w", pady=(4, 4))
        ttk.Separator(self).pack(fill="x", pady=8); body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=18)
        for row, (label, variable, size) in enumerate((("TRACK", self.track_var, 38), ("MOTOR", self.motor_var, 22), ("HEAD", self.head_var, 22), ("WRITE PROTECT", self.wp_var, 22), ("DENSITY", self.density_var, 22), ("RPM", self.rpm_var, 22), ("RPM STATE", self.rpm_state_var, 11), ("SECTOR", self.sector_var, 22), ("SYNC / SEC", self.sync_var, 22))):
            ttk.Label(body, text=label, font=("Segoe UI", 12 if size > 11 else 10, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 30), pady=6); ttk.Label(body, textvariable=variable, font=("Consolas", size, "bold")).grid(row=row, column=1, sticky="w", pady=6)
        ttk.Label(body, textvariable=self.home_var).grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 3)); ttk.Label(body, textvariable=self.firmware_var).grid(row=10, column=0, columnspan=2, sticky="w")
        fifo = ttk.LabelFrame(self, text="RECENT SECTORS"); fifo.pack(fill="x", padx=18, pady=10); self.fifo = tk.StringVar(value=""); ttk.Label(fifo, textvariable=self.fifo, font=("Consolas", 16, "bold")).pack(anchor="w", padx=12, pady=10); self.refresh_ports()

    def refresh_ports(self) -> None:
        pass
    def connect_port(self, port: str) -> None:
        if self.connection.connected and self.connected_port == port:
            return
        if self.connection.connected:
            self.toggle()
        self.port_var.set(port)
        if not self.connection.connected:
            self.toggle()
    def toggle(self) -> None:
        if self.connection.connected: self.connection.disconnect(); self.connected_port = None; self.connection_var.set("Disconnected"); self.state_changed(); return
        if not self.port_var.get(): messagebox.showerror("No COM port", "Select the DriveHUD USB CDC port first."); return
        try: self.connection.connect(self.port_var.get(), pulse_dtr=True); self.connected_port = self.port_var.get(); self.connection_var.set("Connected"); self.after(150, self.connection.assert_dtr)
        except Exception as exc: messagebox.showerror("Connection failed", str(exc))
        self.state_changed()

    def connection_failed(self, _reason: str) -> None:
        self.connection.disconnect()
        self.connection_var.set("Connection lost — retrying...")
        self.retry_after = time.monotonic() + 3.0

    def retry_connection(self) -> None:
        if self.connection.connected or not self.connected_port or time.monotonic() < self.retry_after:
            return
        try:
            self.connection.connect(self.connected_port, pulse_dtr=True)
        except Exception:
            self.connection_var.set("Reconnecting...")
            self.retry_after = time.monotonic() + 3.0
            return
        self.connection_var.set("Connected")
        self.after(150, self.connection.assert_dtr)
    def render_track(self) -> None:
        if self.pos2 is None:
            self.track_var.set("--.-")
            return
        self.track_var.set(f"{self.pos2 // 2:02d}{'.5' if self.pos2 & 1 else '.0'}")

    def report_write_protect(self, state: str) -> None:
        """Display only a stable raw X2/write-protect sample.

        UB4 is intentionally a passive monitor.  A brief edge or noise on its
        probe must not make the HUD appear to change the drive's protection.
        """
        if state == self.wp_candidate:
            return
        self.wp_candidate = state
        self.after(250, lambda expected=state: self.commit_write_protect(expected))

    def commit_write_protect(self, expected: str) -> None:
        if self.wp_candidate != expected or self.wp_reported == expected:
            return
        self.wp_reported = expected
        self.wp_var.set("PROTECTED" if expected == "1" else "WRITABLE")

    def schedule_track_candidate(self, value: int) -> None:
        # $0022 can briefly hold a destination or other non-track value while
        # DOS is seeking.  It is only a startup hint, never an authoritative
        # replacement for live phase tracking.
        if self.pos2 is not None or not (1 <= value <= 42):
            return
        self.track_candidate = value
        if self.track_candidate_after is not None:
            self.after_cancel(self.track_candidate_after)
        self.track_candidate_after = self.after(250, self.commit_track_candidate)

    def commit_track_candidate(self) -> None:
        self.track_candidate_after = None
        if self.pos2 is not None or self.track_candidate is None:
            return
        if self.last_phase_at is not None and time.monotonic() - self.last_phase_at < 0.250:
            self.track_candidate_after = self.after(100, self.commit_track_candidate)
            return
        self.pos2 = self.track_candidate * 2
        self.render_track()
        self.home_var.set(f"TRACK: initialized from DOS $0022 = {self.track_candidate}")

    def anchor_home(self) -> None:
        self.pos2 = 2
        self.track_candidate = None
        if self.track_candidate_after is not None:
            self.after_cancel(self.track_candidate_after)
            self.track_candidate_after = None
        self.render_track()
        self.home_var.set("HOME: anchored at Track 1.0")

    def process(self, line: str) -> None:
        match = self.motor_re.search(line)
        if match: self.motor = match.group(1) == "1"; self.motor_var.set("ON" if self.motor else "OFF"); self.rpm_state_var.set("ACQUIRING" if self.motor else "MOTOR OFF"); return
        match = self.phase_re.search(line)
        if match:
            delta = int(match.group(3)); self.last_phase_at = self.last_move = time.monotonic()
            if delta == 1:
                self.direction = 1
                if self.pos2 is not None:
                    self.pos2 += 1
                    self.render_track()
            elif delta == 3:
                self.direction = -1
                if self.pos2 is not None:
                    # A real 1541 head cannot travel below Track 1.0.  The
                    # lower clamp also rejects stale events during startup.
                    self.pos2 = max(2, self.pos2 - 1)
                    self.render_track()
            self.head_var.set("IN" if delta == 1 else "OUT" if delta == 3 else "STALL"); return
        match = self.track_re.search(line)
        if match:
            value = int(match.group(1))
            if value == 1:
                self.anchor_home()
            elif self.pos2 is None:
                self.schedule_track_candidate(value)
            return
        match = self.density_re.search(line)
        if match: self.density_var.set("D" + match.group(1)); return
        match = self.wp_re.search(line)
        if match: self.report_write_protect(match.group(1)); return
        match = self.rpm_re.search(line)
        if match: self.rpm_var.set(match.group(3)); self.rpm_state_var.set("FRESH" if self.motor else "MOTOR OFF"); return
        match = self.hdr_re.search(line)
        if match: self.sector_var.set(match.group(2)); self.sectors = (self.sectors + [int(match.group(2))])[-10:]; self.fifo.set("  ".join(f"{value:02d}" for value in self.sectors)); return
        match = self.sync_re.search(line)
        if match: self.sync_var.set(match.group(1))
    def poll(self) -> None:
        try:
            # Keep continuous HUD telemetry from starving the Tk event loop.
            for _ in range(100):
                kind, text = self.connection.events.get_nowait()
                if kind == "rx": self.process(text)
                elif kind == "error": self.connection_failed(text)
        except queue.Empty: pass
        self.retry_connection()
        self.state_changed(); self.after(30, self.poll)
    def tick(self) -> None:
        if self.motor and time.monotonic() - self.last_move > 1.25: self.head_var.set("STALL")
        self.after(100, self.tick)


class UniversalApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__(); self.title(f"1541 OneROM Universal GUI {APP_VERSION}"); self.minsize(980, 720)
        self.settings_path = Path(os.environ.get("APPDATA", str(Path.home()))) / "1541-OneROM" / "universal-gui.json"; self.settings = self.load_settings(); self.normal_geometry = self.settings.get("geometry", DEFAULT_GEOMETRY); self.geometry(self.normal_geometry)
        self.maximized = tk.BooleanVar(value=bool(self.settings.get("maximized", False))); self.show_active = tk.BooleanVar(value=self.settings.get("show_active", True)); self.show_hud = tk.BooleanVar(value=self.settings.get("show_hud", True)); self.auto_connect = tk.BooleanVar(value=self.settings.get("auto_connect", True)); self.default_tab = tk.StringVar(value=self.settings.get("default_tab", "OneROM Control")); self.inventory: list[PortInfo] = []; self.binding_var = tk.StringVar(); self.connection_summary_var = tk.StringVar()
        top = ttk.Frame(self, padding=(12, 8)); top.pack(fill="x"); ttk.Label(top, text="1541 OneROM Universal GUI", font=("Segoe UI", 16, "bold")).pack(side="left"); ttk.Checkbutton(top, text="Maximize window", variable=self.maximized, command=self.apply_maximize).pack(side="right")
        self.notebook = ttk.Notebook(self); self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.active = ActivePanel(self.notebook, self.update_dashboard); self.hud = HUDPanel(self.notebook, self.update_dashboard); self.options = ttk.Frame(self.notebook, padding=18); self.notebook.add(self.active, text="OneROM Control"); self.notebook.add(self.hud, text="DriveHUD"); self.notebook.add(self.options, text="Options")
        self.build_options(); self.refresh_inventory(); self.apply_tabs(); self.after_idle(self.apply_maximize); self.after_idle(self.select_default_tab); self.after(700, self.auto_connect_devices); self.bind("<Configure>", self.remember_geometry); self.protocol("WM_DELETE_WINDOW", self.close)
    def build_options(self) -> None:
        ttk.Label(self.options, text="Options", font=("Segoe UI", 16, "bold")).pack(anchor="w"); ttk.Checkbutton(self.options, text="Show OneROM Control tab", variable=self.show_active, command=self.apply_tabs).pack(anchor="w", pady=(10, 2)); ttk.Checkbutton(self.options, text="Show DriveHUD tab", variable=self.show_hud, command=self.apply_tabs).pack(anchor="w", pady=2); ttk.Checkbutton(self.options, text="Automatically connect saved devices at startup", variable=self.auto_connect).pack(anchor="w", pady=2)
        startup = ttk.Frame(self.options); startup.pack(anchor="w", pady=(8, 0)); ttk.Label(startup, text="Default startup tab:").pack(side="left"); ttk.Combobox(startup, textvariable=self.default_tab, values=("OneROM Control", "DriveHUD"), state="readonly", width=18).pack(side="left", padx=6); ttk.Button(startup, text="Apply", command=self.apply_default_tab).pack(side="left")
        current = ttk.LabelFrame(self.options, text="Current queried connections", padding=8); current.pack(fill="x", pady=(12, 0)); ttk.Label(current, textvariable=self.connection_summary_var, justify="left", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        frame = ttk.LabelFrame(self.options, text="USB CDC devices — bind by USB serial number, never by COM port", padding=10); frame.pack(fill="both", expand=True, pady=14); self.port_tree = ttk.Treeview(frame, columns=("port", "serial", "role", "vidpid", "query"), show="headings", height=8)
        for key, text, width in (("port", "Current port", 105), ("serial", "USB serial", 220), ("role", "Detected role / binding", 240), ("vidpid", "VID:PID", 100), ("query", "Query result", 390)): self.port_tree.heading(key, text=text); self.port_tree.column(key, width=width, stretch=key == "query")
        self.port_tree.pack(fill="both", expand=True); buttons = ttk.Frame(frame); buttons.pack(fill="x", pady=(8, 0)); ttk.Button(buttons, text="Scan", command=self.refresh_inventory).pack(side="left"); ttk.Button(buttons, text="Connect saved devices", command=self.connect_saved_devices).pack(side="left", padx=5); ttk.Button(buttons, text="Save current UB3 + UB4", command=self.save_current_connections).pack(side="left", padx=5); ttk.Button(buttons, text="Bind selected as UB3", command=lambda: self.bind_selected("ub3_active_onerom")).pack(side="left", padx=5); ttk.Button(buttons, text="Bind selected as UB4", command=lambda: self.bind_selected("ub4_drivehud")).pack(side="left", padx=5); ttk.Button(buttons, text="Forget bindings", command=self.forget_bindings).pack(side="right")
        ttk.Label(self.options, textvariable=self.binding_var).pack(anchor="w")
    def refresh_inventory(self) -> None:
        self.inventory = enumerate_ports(); [self.port_tree.delete(item) for item in self.port_tree.get_children()]
        for item in self.inventory:
            role = self.role_for_port(item.device)
            self.port_tree.insert("", "end", iid=item.device, values=(item.device, item.serial_number or "(no USB serial)", role, item.vid_pid, self.query_for_port(item.device)))
        self.update_dashboard()

    def role_for_port(self, port: str) -> str:
        roles: list[str] = []
        if self.active.connection.connected and self.active.connected_port == port:
            roles.append("UB3 active control (connected)")
        if self.hud.connection.connected and self.hud.connected_port == port:
            roles.append("UB4 DriveHUD (connected)")
        if not roles:
            for name, label in (("ub3_active_onerom", "Saved UB3 binding"), ("ub4_drivehud", "Saved UB4 binding")):
                serial_number = self.settings.get("devices", {}).get(name, {}).get("usb_serial")
                item = next((candidate for candidate in self.inventory if candidate.device == port), None)
                if item and serial_number and item.serial_number == serial_number:
                    roles.append(label)
        return "; ".join(roles) if roles else "Unassigned"

    def query_for_port(self, port: str) -> str:
        if self.active.connection.connected and self.active.connected_port == port:
            return self.active.firmware_var.get()
        if self.hud.connection.connected and self.hud.connected_port == port:
            return self.hud.firmware_var.get()
        return "Not queried"

    def refresh_live_query_rows(self) -> None:
        # The initial scan happens before the automatic connections finish.
        # Update just the live role/query cells as those replies arrive.
        if not hasattr(self, "port_tree"):
            return
        for item in self.inventory:
            if not self.port_tree.exists(item.device):
                continue
            values = list(self.port_tree.item(item.device, "values"))
            if len(values) == 5:
                values[2] = self.role_for_port(item.device)
                values[4] = self.query_for_port(item.device)
                self.port_tree.item(item.device, values=values)

    def serial_for_port(self, port: str | None) -> str | None:
        if not port:
            return None
        item = next((candidate for candidate in self.inventory if candidate.device == port), None)
        return item.serial_number if item and item.serial_number else None

    def save_current_connections(self) -> None:
        active_serial = self.serial_for_port(self.active.connected_port)
        hud_serial = self.serial_for_port(self.hud.connected_port)
        if not active_serial and not hud_serial:
            messagebox.showerror("No active connections", "Connect UB3 and/or UB4 first, then save their current role bindings.")
            return
        devices = self.settings.setdefault("devices", {})
        if active_serial:
            devices["ub3_active_onerom"] = {"usb_serial": active_serial, "expected_role": "active-rom-control"}
        if hud_serial:
            devices["ub4_drivehud"] = {"usb_serial": hud_serial, "expected_role": "drivehud-passive-monitor"}
        self.save_settings()
        self.refresh_inventory()
        messagebox.showinfo("Bindings saved", "The currently connected UB3/UB4 board identities were saved by USB serial number.")
    def bind_selected(self, role: str) -> None:
        selected = self.port_tree.selection()
        if not selected: messagebox.showerror("No device selected", "Select a USB CDC device first."); return
        info = next((item for item in self.inventory if item.device == selected[0]), None)
        if not info or not info.serial_number: messagebox.showerror("No USB serial", "This device exposes no USB serial number, so it cannot be safely bound."); return
        self.settings.setdefault("devices", {}).setdefault(role, {})["usb_serial"] = info.serial_number; self.settings["devices"][role]["expected_role"] = "active-rom-control" if role.startswith("ub3") else "drivehud-passive-monitor"; self.save_settings()
        if role == "ub3_active_onerom":
            self.active.connect_port(info.device)
        else:
            self.hud.connect_port(info.device)
        self.refresh_inventory(); messagebox.showinfo("Binding saved", f"{role} is now bound to USB serial {info.serial_number} and routed to its tab.")
    def forget_bindings(self) -> None:
        # Forget is a fresh-setup action.  Clear both the saved role bindings
        # and the displayed discovery results so the user can plug in one
        # board, press Scan, and identify it from that new result.
        self.settings["devices"] = {}
        self.inventory = []
        for item in self.port_tree.get_children():
            self.port_tree.delete(item)
        self.save_settings()
        self.update_dashboard()
    def find_bound_port(self, role: str) -> str | None:
        wanted = self.settings.get("devices", {}).get(role, {}).get("usb_serial"); return next((item.device for item in self.inventory if wanted and item.serial_number == wanted), None)
    def auto_connect_devices(self) -> None:
        if not self.auto_connect.get(): return
        self.connect_saved_devices()
    def connect_saved_devices(self) -> None:
        self.refresh_inventory(); active_port, hud_port = self.find_bound_port("ub3_active_onerom"), self.find_bound_port("ub4_drivehud")
        if active_port: self.active.connect_port(active_port)
        if hud_port: self.hud.connect_port(hud_port)
    def update_dashboard(self) -> None:
        saved_active = self.settings.get("devices", {}).get("ub3_active_onerom", {}).get("usb_serial")
        saved_hud = self.settings.get("devices", {}).get("ub4_drivehud", {}).get("usb_serial")
        active_serial = saved_active or self.serial_for_port(self.active.connected_port) or "not bound"
        hud_serial = saved_hud or self.serial_for_port(self.hud.connected_port) or "not bound"
        active_note = "" if saved_active else " (detected, not saved)" if active_serial != "not bound" else ""
        hud_note = "" if saved_hud else " (detected, not saved)" if hud_serial != "not bound" else ""
        self.connection_summary_var.set(
            f"UB3 Active OneROM: {'Connected' if self.active.connection.connected else 'Disconnected'} — serial {active_serial}{active_note}\n"
            f"  Query result: {self.active.firmware_var.get()}\n"
            f"UB4 DriveHUD: {'Connected' if self.hud.connection.connected else 'Disconnected'} — serial {hud_serial}{hud_note}\n"
            f"  Query result: {self.hud.firmware_var.get()}"
        )
        self.binding_var.set(f"Saved UB3 serial: {saved_active or 'not bound'}    |    Saved UB4 serial: {saved_hud or 'not bound'}")
        self.refresh_live_query_rows()
    def apply_tabs(self) -> None:
        for tab, visible in ((self.active, self.show_active.get()), (self.hud, self.show_hud.get())):
            if visible: self.notebook.add(tab, text="OneROM Control" if tab is self.active else "DriveHUD")
            else: self.notebook.hide(tab)
    def select_default_tab(self) -> None:
        target = self.active if self.default_tab.get() == "OneROM Control" and self.show_active.get() else self.hud if self.show_hud.get() else self.options
        self.notebook.select(target)
    def apply_default_tab(self) -> None:
        self.apply_tabs()
        self.select_default_tab()
        self.save_settings()
    def load_settings(self) -> dict:
        try: return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {"schema_version": 1, "devices": {}}
    def apply_maximize(self) -> None:
        if self.maximized.get(): self.state("zoomed")
        elif self.state() == "zoomed": self.state("normal"); self.geometry(self.normal_geometry)
    def remember_geometry(self, _event=None) -> None:
        if self.state() == "normal": self.normal_geometry = self.geometry()
    def save_settings(self) -> None:
        self.settings.update({"schema_version": 1, "geometry": self.normal_geometry, "maximized": self.maximized.get(), "show_active": self.show_active.get(), "show_hud": self.show_hud.get(), "auto_connect": self.auto_connect.get(), "default_tab": self.default_tab.get()}); self.settings_path.parent.mkdir(parents=True, exist_ok=True); self.settings_path.write_text(json.dumps(self.settings, indent=2) + "\n", encoding="utf-8")
    def close(self) -> None:
        self.save_settings(); self.active.connection.disconnect(); self.hud.connection.disconnect(); self.destroy()


if __name__ == "__main__":
    UniversalApp().mainloop()
