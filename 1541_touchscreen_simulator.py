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
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from onerom_usb import CdcBoardLink, DriveBinding, DriveTelemetryParser, discover_cdc_boards, load_binding, save_binding


WIDTH, HEIGHT = 1280, 720
APP_VERSION = "V0.0.7"
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

COLOR_PALETTES = {
    "background": ("Background", ("#101820", "#07121d", "#1b1b1f", "#24303a", "#3b2b1e", "#142331", "#222b38", "#2d2638", "#f1e8d5", "#0c2227", "#15261a", "#2a1e26", "#303030", "#22314c", "#3c3322", "#132b3a", "#241d35", "#e7edf0")),
    "group": ("Group / Card Boxes", ("#182632", "#203442", "#263945", "#2d3645", "#382f45", "#2e3c34", "#252525", "#3a3025", "#e6e6e6", "#24404a", "#2f4a37", "#4a3542", "#3b3b3b", "#34445b", "#4b4231", "#25495a", "#3e324f", "#f4f4f4")),
    "button": ("Button Color", ("#33c3a5", "#ef6b73", "#4ea1ff", "#f6c85f", "#af7bff", "#59c36a", "#e77cb4", "#f08a4b", "#e9eef3", "#00a8a8", "#ff8c42", "#61dafb", "#ffcc4d", "#9370db", "#2ecc71", "#ff6b9a", "#ff7043", "#ffffff")),
    "text1": ("Text 1 - Descriptions", ("#a9bbc4", "#d5e2e8", "#91b8d6", "#d4bc86", "#8acfc3", "#d4a8bf", "#b8c0ce", "#d8af82", "#f0f4f7", "#9fd3d6", "#c3d9bc", "#deb4cf", "#c8c8c8", "#b5c7e0", "#e1c59d", "#9ed1e6", "#cfb5e8", "#ffffff")),
    "text2": ("Text 2 - Values", ("#eef6fa", "#ffffff", "#c9e4ff", "#ffdf8a", "#5eead4", "#f0b4da", "#d0d9e5", "#f1cfa5", "#dff8f4", "#bffff4", "#e5ffd7", "#ffd0e5", "#e4e4e4", "#d6e5ff", "#ffe1b8", "#c9efff", "#e7d3ff", "#ffffff")),
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
        # These mirror the optional controller capabilities selected in the
        # board's configuration JSON at compile time.  The touchscreen may
        # show only the controls the finished controller actually supports.
        self.controller_rom_enabled = tk.BooleanVar(value=True)
        self.controller_iec_enabled = tk.BooleanVar(value=True)
        self.controller_wp_enabled = tk.BooleanVar(value=True)
        self.startup = tk.StringVar(value="Automatic")
        self.idle_seconds = tk.IntVar(value=300)
        self.preview_ppi = tk.DoubleVar(value=102.4)
        self.preview_scale = tk.IntVar(value=100)
        # Slot 0 is the OneROM bootloader and deliberately not selectable.
        # Present every selectable startup slot, exactly as the real UB3
        # firmware reports them.
        self.rom_choices = (
            "Slot 1 — ORIGINAL", "Slot 2 — JIFFYDOS", "Slot 3 — ORIGINAL",
            "Slot 4 — JIFFYDOS", "Slot 5 — ORIGINAL", "Slot 6 — JIFFYDOS",
            "Slot 7 — ORIGINAL",
        )
        self.iec_choices = ("Device 8", "Device 9", "Device 10", "Device 11")
        self.current_page = "hud"
        self.last_input = time.monotonic()
        self.screensaver = False
        self.track = 0
        self.sector = 6
        self.motor = False
        self.head_direction = "IN"
        self.write_protect_prompt: bool | None = None
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
        self.live_rpm: float | None = None
        self.live_sector: int | None = None
        self.live_sync_count: int | None = None
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
        self.controller_wp_available: bool | None = None
        self._pending_wp_override: bool | None = None
        self._wp_pending_state: bool | None = None
        self._wp_after_id: str | None = None
        self._reconnect_after: dict[str, str] = {}
        self._reconnect_delay_ms = {"controller": 1000, "hud": 1000}
        self.usb_log_lines: list[str] = []
        self.log_scroll = 0
        self.log_return_page = "settings"
        # The passive HUD is an expandable card list.  Priorities 1–6 pin
        # the default visible cards; unpinned diagnostics follow by name.
        self.hud_scroll_index = 0
        self.hud_help_card: str | None = None
        self.priority_prompt_card: str | None = None
        self.priority_prompt_value = ""
        self._hud_subscription_mask: int | None = None
        self._hud_telemetry_enabled = False
        self._hud_redraw_after: str | None = None
        self.hud_card_priorities: dict[str, int | None] = {
            "track": 1,
            "rotation": 2,
            "activity": 3,
            "physical_header": 4,
            "sector_coverage": 5,
            "capture_health": 6,
        }

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
        self.build_control()
        self.build_settings()
        self.build_connection_setup("controller")
        self.build_connection_setup("hud")
        self.build_idle()
        self.refresh_usb_boards()
        self.nav = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.nav.pack(fill="x")
        self.hud_button = ttk.Button(self.nav, text="DRIVEHUD", style="Nav.TButton", command=lambda: self.show_page("hud"))
        self.control_button = ttk.Button(self.nav, text="ONEROM CONTROL", style="Nav.TButton", command=lambda: self.show_page("control"))
        self.hud_button.pack(side="left")
        self.control_button.pack(side="left", padx=12)
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
        ttk.Label(page, text="DriveHUD", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Live 1541 mechanical telemetry — selected DriveHUD board", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
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
        ttk.Button(footer, text="DRIVEHUD CONNECTION SETUP", command=lambda: self.show_connection_setup("hud")).pack(side="right")

    def build_control(self) -> None:
        page = self.page("control")
        ttk.Label(page, text="OneROM Control", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Persistent ROM, IEC address, and write-protect configuration — selected Controller board", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        left = ttk.Frame(page); left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(page); right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        rom = ttk.Frame(left, style="Panel.TFrame", padding=18); rom.pack(fill="both", expand=True)
        ttk.Label(rom, text="STARTUP ROM", style="Section.TLabel").pack(anchor="w")
        self.rom_var = tk.StringVar(value="Slot 2 — JIFFYDOS")
        ttk.Label(rom, textvariable=self.rom_var, style="MetricSmall.TLabel").pack(anchor="w", pady=(8, 12))
        self.rom_choice = ttk.Combobox(rom, values=self.rom_choices, state="readonly", font=("Segoe UI", 12))
        self.rom_choice.set("Slot 2 — JIFFYDOS"); self.rom_choice.pack(fill="x", pady=4)
        ttk.Button(rom, text="SAVE STARTUP ROM", command=self.save_rom).pack(anchor="e", pady=(12, 0))
        iec = ttk.Frame(right, style="Panel.TFrame", padding=18); iec.pack(fill="x")
        ttk.Label(iec, text="BOOT IEC ADDRESS", style="Section.TLabel").pack(anchor="w")
        self.iec_var = tk.StringVar(value="Device 8")
        ttk.Label(iec, textvariable=self.iec_var, style="MetricSmall.TLabel").pack(anchor="w", pady=(8, 12))
        self.iec_choice = ttk.Combobox(iec, values=self.iec_choices, state="readonly", font=("Segoe UI", 12))
        self.iec_choice.set("Device 8"); self.iec_choice.pack(fill="x", pady=4)
        ttk.Button(iec, text="SAVE IEC ADDRESS", command=self.save_iec).pack(anchor="e", pady=(12, 0))
        protect = ttk.Frame(right, style="Panel.TFrame", padding=18); protect.pack(fill="both", expand=True, pady=(16, 0))
        ttk.Label(protect, text="WRITE-PROTECT OVERRIDE", style="Section.TLabel").pack(anchor="w")
        self.writable = tk.BooleanVar(value=False)
        ttk.Checkbutton(protect, text="Force writable (X2)", variable=self.writable, command=self.update_protection).pack(anchor="w", pady=(12, 4))
        self.control_status = tk.StringVar()
        ttk.Label(protect, textvariable=self.control_status, style="Panel.TLabel", wraplength=450).pack(anchor="w", pady=(15, 0))

    def build_settings(self) -> None:
        page = self.page("settings")
        ttk.Label(page, text="Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Prototype communications and startup behavior", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        devices = ttk.Frame(page); devices.pack(fill="x")
        devices.columnconfigure((0, 1), weight=1)
        controller_card = ttk.Frame(devices, style="Panel.TFrame", padding=18)
        controller_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ttk.Label(controller_card, text="CONTROLLER ONE ROM", style="Section.TLabel").pack(anchor="w")
        ttk.Label(controller_card, text="Choose the OneROM that controls ROM, IEC, and write-protect.", style="Panel.TLabel", wraplength=500).pack(anchor="w", pady=(8, 12))
        ttk.Button(controller_card, text="CONTROLLER CONNECTION SETUP", command=lambda: self.show_connection_setup("controller")).pack(anchor="e")
        hud_card = ttk.Frame(devices, style="Panel.TFrame", padding=18)
        hud_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ttk.Label(hud_card, text="DRIVEHUD ONE ROM", style="Section.TLabel").pack(anchor="w")
        ttk.Label(hud_card, text="Choose the OneROM that supplies passive drive telemetry.", style="Panel.TLabel", wraplength=500).pack(anchor="w", pady=(8, 12))
        ttk.Button(hud_card, text="DRIVEHUD CONNECTION SETUP", command=lambda: self.show_connection_setup("hud")).pack(anchor="e")
        self.device_summary = tk.StringVar()
        ttk.Label(page, textvariable=self.device_summary, style="Sub.TLabel", wraplength=1100).pack(anchor="w", pady=(14, 0))
        startup = ttk.Frame(page, style="Panel.TFrame", padding=18); startup.pack(fill="x", pady=(14, 0))
        ttk.Label(startup, text="STARTUP AND IDLE BEHAVIOR", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(startup, text="Preferred startup screen:", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(startup, textvariable=self.startup, values=("Automatic", "DriveHUD", "OneROM Control"), state="readonly", width=22, font=("Segoe UI", 11)).grid(row=1, column=1, sticky="w", padx=12, pady=(12, 0))
        ttk.Button(startup, text="Apply", command=self.apply_startup).grid(row=1, column=2, padx=8, pady=(12, 0))
        ttk.Label(startup, text="Idle before Wilson screen saver (seconds):", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(startup, from_=10, to=3600, textvariable=self.idle_seconds, width=9, font=("Segoe UI", 11)).grid(row=2, column=1, sticky="w", padx=12, pady=(12, 0))
        ttk.Button(startup, text="PREVIEW IDLE SCREEN", command=self.show_idle).grid(row=2, column=2, padx=8, pady=(12, 0))
        ttk.Label(startup, text="Monitor PPI (V226HQL is 102.4):", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Spinbox(startup, from_=70, to=240, increment=0.1, textvariable=self.preview_ppi, width=9, font=("Segoe UI", 11)).grid(row=3, column=1, sticky="w", padx=12, pady=(16, 0))
        ttk.Label(startup, text="Preview scale (%):", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(startup, from_=25, to=200, textvariable=self.preview_scale, width=9, font=("Segoe UI", 11)).grid(row=4, column=1, sticky="w", padx=12, pady=(8, 0))
        ttk.Button(startup, text="OPEN / APPLY 7-INCH PREVIEW", command=self.open_size_preview).grid(row=4, column=2, padx=8, pady=(8, 0))
        ttk.Button(page, text="← RETURN", style="Nav.TButton", command=lambda: self.show_page(self.default_page())).pack(anchor="w", pady=16)

    def build_connection_setup(self, role: str) -> None:
        """Build one dedicated setup page per physical OneROM role."""
        is_controller = role == "controller"
        page = self.page(f"{role}_connection")
        role_name = "Controller" if is_controller else "DriveHUD"
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
        if name == "hud" and not self.ub4.get():
            name = "control" if self.ub3.get() else "settings"
        if name == "control" and not self.ub3.get():
            name = "hud" if self.ub4.get() else "settings"
        for frame in self.pages.values(): frame.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current_page = name
        self.screensaver = name == "idle"
        self.nav.pack_forget() if self.screensaver else self.nav.pack(fill="x")
        self.register_input()

    def default_page(self) -> str:
        if self.startup.get() == "DriveHUD" and self.ub4.get(): return "hud"
        if self.startup.get() == "OneROM Control" and self.ub3.get(): return "control"
        if self.ub4.get(): return "hud"
        if self.ub3.get(): return "control"
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
        """Keep a bounded, timestamped communications history for the UI."""
        timestamp = time.strftime("%H:%M:%S")
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
        )
        try:
            if selected_role == "controller" and binding.controller_serial and binding.controller_serial == binding.hud_serial:
                raise ValueError("This OneROM is currently bound as DriveHUD. Release the DriveHUD binding before assigning it as Controller.")
            if selected_role == "hud" and binding.hud_serial and binding.hud_serial == binding.controller_serial:
                raise ValueError("This OneROM is currently bound as Controller. Release the Controller binding before assigning it as DriveHUD.")
            binding.validate()
            if not self.usb_boards:
                self.refresh_usb_boards()
            roles = (selected_role,) if selected_role else ("controller", "hud")
            for role in roles:
                serial_number = binding.controller_serial if role == "controller" else binding.hud_serial
                if not serial_number:
                    raise ValueError(f"Select a {role.title()} serial first.")
                board = self.usb_boards.get(serial_number)
                if board is None:
                    raise ValueError(f"{role.title()} serial {serial_number} is not currently connected.")
                existing = self.usb_links.get(role)
                if existing is not None and existing.board.serial_number == serial_number and existing.connected:
                    continue
                if existing is not None:
                    existing.close()
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
            self.usb_status.set(f"{selected_role.title() if selected_role else 'Role'} connected — Controller: {controller_state}; DriveHUD: {hud_state}.")
            self.append_usb_log("SYSTEM", self.usb_status.get())
            self.apply_device_state()
        except Exception as exc:
            self.usb_status.set(f"USB connection failed: {exc}")
            self.append_usb_log("SYSTEM", self.usb_status.get())

    def enable_hud_telemetry(self, link: CdcBoardLink) -> None:
        """Enable every passive firmware telemetry class after HUD attach."""
        if self.usb_links.get("hud") is not link or not link.connected:
            return
        try:
            link.write_command("HUDCFG M=31")
            self._hud_subscription_mask = 31
            self._hud_telemetry_enabled = True
            self.append_usb_log("SYSTEM", "DriveHUD passive telemetry enabled.")
        except Exception as exc:
            self.append_usb_log("SYSTEM", f"DriveHUD telemetry setup failed: {exc}")

    def release_role_binding(self, role: str) -> None:
        """Release one persisted role without touching the other OneROM."""
        if role not in ("controller", "hud"):
            raise ValueError(f"Unknown OneROM role: {role}")
        link = self.usb_links.pop(role, None)
        if link is not None:
            link.close()
        if role == "controller":
            self.controller_serial.set("")
        else:
            self.hud_serial.set("")
        self.drive_binding = DriveBinding(
            drive_id="1541 Drive",
            controller_serial=self.controller_serial.get().strip(),
            hud_serial=self.hud_serial.get().strip(),
        )
        save_binding(self.binding_path, self.drive_binding)
        self.ub3.set("controller" in self.usb_links)
        self.ub4.set("hud" in self.usb_links)
        self.usb_status.set(f"{role.title()} binding released. The other OneROM role was left unchanged.")
        self.append_usb_log("SYSTEM", self.usb_status.get())
        self.apply_device_state()

    def poll_usb_telemetry(self) -> None:
        """Apply passive DriveHUD CDC telemetry without emitting control commands."""
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
            first_tail_index = max(0, len(received_lines) - 96)
            telemetry_lines = [
                line for index, line in enumerate(received_lines)
                if index >= first_tail_index
                or line.startswith(("STATUS ", "STATE ", "TRACK_WRITE ", "PHASE ", "MOTOR "))
            ]
            for line in telemetry_lines:
                self.append_usb_log("HUD", line)
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
                    self.live_rpm = state.rpm
                    self.rpm_var.set(f"{state.rpm:.2f}")
                # The parser retains its last sector between CDC records.
                # Promote it to the HUD only when this specific record is a
                # newly decoded physical header; otherwise it can be stale
                # after a seek, homing pass, or motor stop.
                if line.startswith("HDRPHY ") and state.sector is not None and self.motor:
                    self.live_sector = state.sector
                    self.sector_var.set(f"{state.sector:02d}")
                if state.sync_count is not None:
                    self.live_sync_count = state.sync_count
                    if line.startswith("SYNC "):
                        sample = self.effective_rpm()
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
            self.record_serial_diagnostic("DriveHUD", link, exc)
            link.close()
            self.usb_links.pop("hud", None)
            self.ub4.set(False)
            self.usb_status.set(f"DriveHUD link interrupted; reconnecting automatically: {exc}")
            self.append_usb_log("HUD", self.usb_status.get())
            self.apply_device_state()
            self.schedule_role_reconnect("hud")

    def poll_controller_feedback(self) -> None:
        """Drain Controller CDC output so its USB log/reply FIFO cannot back up."""
        link = self.usb_links.get("controller")
        if link is None:
            return
        try:
            for line in link.read_lines()[-96:]:
                self.append_usb_log("CONTROLLER", line)
                # Selector replies are the only controller lines surfaced to
                # the operator. Other CDC log lines are still drained.
                if line.startswith("$ROMTEST,"):
                    if line.startswith("$ROMTEST,WP,"):
                        self.handle_controller_wp_reply(line)
                    elif ",OK," in line or line.endswith(",OK"):
                        self.control_status.set(f"Controller verified: {line}")
                    elif ",FAIL" in line or ",ERROR," in line:
                        self.control_status.set(f"Controller rejected request: {line}")
        except Exception as exc:
            self.record_serial_diagnostic("Controller", link, exc)
            link.close()
            self.usb_links.pop("controller", None)
            self.ub3.set(False)
            self.usb_status.set(f"Controller serial link interrupted; reconnecting automatically: {exc}")
            self.append_usb_log("CONTROLLER", self.usb_status.get())
            self.apply_device_state()
            self.schedule_role_reconnect("controller")

    def schedule_role_reconnect(self, role: str) -> None:
        """Retry one role with backoff after a transient Windows CDC failure."""
        if role in self._reconnect_after:
            return
        delay = self._reconnect_delay_ms[role]
        self._reconnect_after[role] = self.after(delay, lambda current=role: self.attempt_role_reconnect(current))
        self._reconnect_delay_ms[role] = min(delay * 2, 8000)

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
            return "ID MISMATCH"
        if self.confirmed_disk_id is not None:
            return f"ID {self.confirmed_disk_id[0]:02X} {self.confirmed_disk_id[1]:02X}"
        return "VERIFYING ID" if self.header_checksum_valid else "WAITING FOR ID"

    def disk_identity_detail(self) -> str:
        if self.disk_id_mismatch:
            return "VALID HEADERS DISAGREE"
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

    def sync_revolution_reading(self) -> tuple[float | None, int | None, int | None]:
        """Return the measured ratio, its physical-count display, and zone target."""
        expected_by_density = {3: 42, 2: 38, 1: 36, 0: 34}
        expected = expected_by_density.get(self.live_density)
        if (
            not self.motor
            or self.live_sync_count is None
            or self.live_rpm is None
            or self.live_rpm <= 0
        ):
            return None, None, expected
        estimate = self.live_sync_count * 60 / self.live_rpm
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
        if not self.rpm_samples:
            return "ACQUIRING"
        spread = max(self.rpm_samples) - min(self.rpm_samples)
        return f"LOCKED · ±{spread / 2:.2f}"

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
        self.append_usb_log("SYSTEM", "Cleared HUD Health drop counters (new diagnostic window).")
        self._hud_dirty = True
        self.update_live_hud_fields()

    def effective_rpm(self) -> float | None:
        """Return qualified RPM from SYNC/sec, rejecting partial windows."""
        expected_by_density = {3: 42, 2: 38, 1: 36, 0: 34}
        expected = expected_by_density.get(self.live_density)
        if self.motor and expected and self.live_sync_count is not None and self.live_sync_count > 0:
            # live_sync_count is SYNC pulses per second. Convert it to RPM
            # using the density's physical SYNCs-per-revolution target.
            derived_rpm = self.live_sync_count * 60.0 / expected
            # Short seek/format windows can report a handful of pulses or a
            # mixed count. Accept only a physically plausible 1541 spindle
            # range, otherwise hold the last qualified reading.
            if 240.0 <= derived_rpm <= 360.0:
                self.last_stable_rpm = derived_rpm
        return self.last_stable_rpm

    def disk_activity_label(self) -> str:
        """Render the disk write-gate state without mistaking CPU R/W for I/O."""
        if not self.motor:
            return "OFF"
        return "WRITING" if time.monotonic() < self._writing_display_until else "READING"

    def scroll_hud_cards(self) -> list[dict[str, str | int | None]]:
        """Return every safe passive measurement as a sortable HUD card."""
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
        history_first, history_second = self.diagnostic_history_lines()
        cards = [
            ("track", "Track / Position", self.live_track, f"HEADER Δ {offset:+.1f}" if offset is not None else "POSITION ESTIMATE · HEADER WAITING", "Derived mechanical position; corrected when physical headers are decoded."),
            ("rotation", "Rotation", f"{rpm:.2f}" if rpm is not None else "--.--", self.rpm_quality_detail(), "RPM derived from fresh SYNC pulse counts using the current density zone."),
            ("activity", "Activity", self.disk_activity_label(), f"WRITE PULSES {self.write_pulse_count} · STEPS {self.phase_event_count}", "WRITING is the observed write-gate signal; otherwise active spinning media is READING."),
            ("physical_header", "Physical Header", header, "CONFIRMED HEADER" if self.last_header_track is not None else "NO CONFIRMED HEADER", "Latest decoded on-disk track and sector header; it is physical media evidence."),
            ("sector_coverage", "Sector Coverage", coverage, f"D{self.live_density} ZONE" if self.live_density is not None else "DENSITY UNKNOWN", "Unique sector numbers seen during this current observation window."),
            ("capture_health", "Capture Health", capture, f"CAP {self.capture_count} · ROV {ring_overrun} · QOV {queue_overflow}" if self.capture_count is not None else "STATUS PENDING", "ROV and QOV are passive capture overruns since the local diagnostic reset."),
            ("disk_identity", "Disk Identity", self.disk_identity_label(), self.header_validation_detail(), "Disk ID bytes are confirmed only after matching decoded headers; this does not decode DOS directory data."),
            ("write_protect", "Write Protect", self.hud_write_protect_label(), self.hud_write_protect_detail(), "Reports the physical write-protect sensor and any confirmed Controller override. This screen cannot change it."),
            ("density", "Density Zone", f"D{self.live_density}" if self.live_density is not None else "--", f"EXPECTED {sync_expected} SYNC / REV" if sync_expected else "WAITING FOR DENSITY", "Density is inferred from 1541 timing/header telemetry and determines expected SYNC density."),
            ("motor_head", "Motor / Head", "ON" if self.motor else "OFF", f"HEAD {self.head_var.get()} · {self.phase_event_count} STEPS", "Motor is the observed drive signal. Head state combines step activity and motor condition."),
            ("header_rate", "Header Rate", f"{header_rate:.1f}/S" if header_rate is not None else "WAITING", "DECODED PHYSICAL HEADERS / SECOND", "Rolling rate of successfully decoded physical headers over the last five seconds."),
            ("capture_rate", "Capture Rate", f"{self.capture_rate / 1000:.0f}K/S" if self.capture_rate is not None else "WAITING", "PASSIVE CAPTURE EVENTS / SECOND", "Rate of passive firmware capture events reported through the compact status record."),
            ("sync_rate", "SYNC Rate", f"{self.live_sync_count}/S" if self.live_sync_count is not None else "WAITING", "RAW SYNC PULSES / SECOND", "Raw SYNC pulse count from the latest one-second capture interval."),
            ("sync_per_rev", "SYNC / Revolution", str(sync_per_rev) if sync_per_rev is not None else "--", f"EST {sync_estimate:.2f}" if sync_estimate is not None else "WAITING FOR SYNC", "SYNC pulses per revolution; compared with the density-zone expectation."),
            ("mechanism", "Mechanism", f"{self.phase_event_count} STEPS", f"TRACK {self.live_track} · HEAD {self.head_var.get()}", "Cumulative observed step/phase transitions since this HUD connection was opened."),
            ("recent_evidence", "Recent Evidence", history_first, history_second or "PASSIVE EVENT HISTORY", "Recent decoded headers, seek events, and motor changes. It is an observation trace, not DOS error data."),
        ]
        result = []
        for card_id, title, value, detail, help_text in cards:
            result.append({"id": card_id, "title": title, "value": value, "detail": detail,
                           "help": help_text, "priority": self.hud_card_priorities.get(card_id)})
        return sorted(
            result,
            key=lambda card: (
                (0, int(card["priority"]), str(card["title"]))
                if card["priority"] is not None else (1, str(card["title"]))
            ),
        )

    @staticmethod
    def scroll_card_paint_text(card: dict[str, str | int | None]) -> tuple[str, str]:
        """Fit special long-form cards into the shared scrolling row."""
        value, detail = str(card["value"]), str(card["detail"])
        if card["id"] == "recent_evidence":
            # Evidence is an event trace, not a primary measurement. Keep a
            # useful leading portion on its own compact line and reserve the
            # lower line for its description.
            return (value if len(value) <= 64 else f"{value[:61]}...", detail)
        return value, detail

    def cycle_hud_card_priority(self, card_id: str) -> None:
        """Move one card through P1…P6 then unpinned, swapping occupied slots."""
        current = self.hud_card_priorities.get(card_id)
        next_priority = 1 if current is None else (None if current >= 6 else current + 1)
        if next_priority is not None:
            for other_id, other_priority in self.hud_card_priorities.items():
                if other_id != card_id and other_priority == next_priority:
                    self.hud_card_priorities[other_id] = current
                    break
        self.hud_card_priorities[card_id] = next_priority
        self.hud_scroll_index = 0

    def scroll_hud_by(self, amount: int) -> None:
        """Move the six-card viewport while retaining a valid final page."""
        max_start = max(0, len(self.scroll_hud_cards()) - 6)
        self.hud_scroll_index = max(0, min(max_start, self.hud_scroll_index + amount))

    def visible_hud_subscription_mask(self) -> int:
        """Return the firmware telemetry classes needed by the six visible cards."""
        core, headers, metadata, rpm, sync = 1, 2, 4, 8, 16
        needed = {
            "track": core, "rotation": rpm | sync, "activity": core,
            "physical_header": headers, "sector_coverage": headers,
            "capture_health": 0, "disk_identity": headers | metadata,
            "write_protect": core, "density": core, "motor_head": core,
            "header_rate": headers, "capture_rate": 0, "sync_rate": sync,
            "sync_per_rev": sync, "mechanism": core,
            "recent_evidence": core | headers,
        }
        cards = self.scroll_hud_cards()
        visible = cards[self.hud_scroll_index:self.hud_scroll_index + 6]
        # These are bit flags, not quantities.  Arithmetic addition breaks
        # when two visible cards need the same class: for example two CORE
        # cards made 1 + 1 == 2 (HEADERS), which accidentally turned CORE
        # telemetry off.  Combine each required class exactly once.
        mask = 0
        for card in visible:
            mask |= needed.get(str(card["id"]), 0)
        return mask

    def update_hud_subscription(self, link: CdcBoardLink) -> None:
        """Tell the HUD firmware to emit only telemetry used by this viewport."""
        mask = self.visible_hud_subscription_mask()
        if mask != self._hud_subscription_mask:
            link.write_command(f"HUDCFG M={mask}")
            self._hud_subscription_mask = mask

    def request_hud_redraw(self) -> None:
        """Rebuild the visible six cards at a bounded desktop-only cadence."""
        if self._hud_redraw_after is not None:
            return

        def redraw() -> None:
            self._hud_redraw_after = None
            if self.preview_page == "hud":
                self.open_size_preview()

        self._hud_redraw_after = self.after(150, redraw)

    def update_live_hud_fields(self) -> None:
        """Update existing HUD Canvas items without rebuilding the screen."""
        if self.preview_page not in ("hud", "diagnostics", "diagnostics_detail"):
            return
        preview = getattr(self, "preview", None)
        canvas = getattr(self, "preview_canvas", None)
        if preview is None or canvas is None or not preview.winfo_exists():
            return
        try:
            if self.preview_page == "diagnostics":
                self.update_live_diagnostics_fields(canvas)
                return
            if self.preview_page == "diagnostics_detail":
                self.update_system_diagnostics_fields(canvas)
                return
            # The scrolling HUD can display any six cards.  Tags for cards
            # outside the viewport simply match no canvas item, which lets us
            # refresh the current view without rebuilding it on each sample.
            for card in self.scroll_hud_cards():
                card_id = str(card["id"])
                value, detail = self.scroll_card_paint_text(card)
                if card_id != "recent_evidence" and len(value) > 24:
                    value = f"{value[:21]}..."
                canvas.itemconfigure(f"scroll_{card_id}_value", text=value)
                canvas.itemconfigure(f"scroll_{card_id}_detail", text=detail)
            canvas.itemconfigure("hud_head_state", text=self.head_var.get())
            canvas.itemconfigure(
                "hud_wp_override",
                text="W/O",
                fill=OFFLINE if self._confirmed_writable else ACCENT,
            )
            canvas.itemconfigure("hud_disk_status", text=self.physical_disk_write_status())
            # Paint the existing visible rows only.  Recreating the whole
            # Canvas while the CDC stream is active can keep Windows/Tk in a
            # perpetual redraw cycle, making a healthy link look frozen.
            canvas.update_idletasks()
            return
            canvas.itemconfigure("hud_track", text=self.live_track)
            canvas.itemconfigure("hud_motor", text="ON" if self.motor else "OFF")
            canvas.itemconfigure("hud_head", text=self.head_var.get())
            canvas.itemconfigure("hud_density", text=f"D{self.live_density}" if self.live_density is not None else "--")
            displayed_rpm = self.effective_rpm()
            canvas.itemconfigure("hud_rpm", text=f"{displayed_rpm:.2f}" if displayed_rpm is not None else "0.00")
            canvas.itemconfigure("hud_rpm_state", text=self.disk_activity_label())
            canvas.itemconfigure("hud_sector", text=f"{self.live_sector:02d}" if self.live_sector is not None else "--")
            canvas.itemconfigure("hud_sync", text=str(self.live_sync_count) if self.live_sync_count is not None else "0")
            canvas.itemconfigure(
                "hud_fifo",
                text=" ".join(f"{sector:02d}" for sector in self.recent_sectors) if self.recent_sectors else "— FIFO empty —",
            )
            sync_estimate, sync_count_per_rev, sync_expected = self.sync_revolution_reading()
            canvas.itemconfigure("hud_sync_rev", text=str(sync_count_per_rev) if sync_count_per_rev is not None else "--")
            estimate_detail = f"EST {sync_estimate:.2f}" if sync_estimate is not None else ""
            expected_detail = (
                f"EXPECTED {sync_expected}" if sync_expected is not None else "WAITING FOR SYNC"
            )
            canvas.itemconfigure("hud_sync_rev_estimate", text=estimate_detail)
            canvas.itemconfigure("hud_sync_rev_detail", text=expected_detail)
            canvas.itemconfigure("hud_wp", text=self.hud_write_protect_label())
            canvas.itemconfigure("hud_wp_detail", text=self.hud_write_protect_detail())
        except tk.TclError:
            # The screen may have changed between CDC receive and paint.
            pass

    def update_live_diagnostics_fields(self, canvas: tk.Canvas) -> None:
        """Refresh the Diagnostics cards without rebuilding the canvas."""
        offset = self.header_offset()
        header = (
            f"T{self.last_header_track:02d} S{self.live_sector:02d}"
            if self.last_header_track is not None and self.live_sector is not None else "WAITING"
        )
        expected = self.expected_sector_count()
        coverage = f"SEEN {len(set(self.recent_sectors))}/{expected}" if expected else "WAITING"
        rpm = self.effective_rpm()
        capture, capture_color = self.capture_health_detail()
        canvas.itemconfigure("diag_position", text=self.live_track)
        canvas.itemconfigure(
            "diag_offset",
            text=f"PHASE ESTIMATE · HEADER Δ {offset:+.1f}" if offset is not None else "PHASE ESTIMATE · HEADER WAITING",
        )
        canvas.itemconfigure("diag_header", text=header)
        canvas.itemconfigure("diag_header_detail", text="PHYSICAL HEADER" if self.last_header_track is not None else "NO CONFIRMED HEADER")
        canvas.itemconfigure("diag_rpm", text=f"{rpm:.2f}" if rpm is not None else "--.--")
        sync_detail = f"SYNC {self.live_sync_count}/S" if self.live_sync_count is not None else "SYNC WAITING"
        canvas.itemconfigure("diag_rpm_detail", text=f"{self.rpm_quality_detail()} · {sync_detail}")
        canvas.itemconfigure("diag_coverage", text=coverage)
        canvas.itemconfigure("diag_coverage_detail", text=f"D{self.live_density} ZONE" if self.live_density is not None else "DENSITY UNKNOWN")
        canvas.itemconfigure("diag_activity", text=self.disk_activity_label())
        canvas.itemconfigure("diag_activity_detail", text=f"WRITE PULSES {self.write_pulse_count} · STEPS {self.phase_event_count}")
        canvas.itemconfigure("diag_media", text=self.disk_identity_label())
        canvas.itemconfigure("diag_capture", text=capture, fill=capture_color)
        ring_overrun, queue_overflow = self.diagnostic_drop_counts()
        canvas.itemconfigure("diag_capture_detail", text=(f"CAP {self.capture_count} · ROV {ring_overrun} · QOV {queue_overflow}" if self.capture_count is not None else "STATUS PENDING"))
        history_first, history_second = self.diagnostic_history_lines()
        canvas.itemconfigure("diag_history", text=history_first)
        canvas.itemconfigure("diag_history_2", text=history_second)

    def update_system_diagnostics_fields(self, canvas: tk.Canvas) -> None:
        """Refresh the deeper passive-measurement page from current telemetry."""
        rate = self.header_rate()
        rpm = self.effective_rpm()
        spread = (max(self.rpm_samples) - min(self.rpm_samples)) if len(self.rpm_samples) > 1 else None
        capture, capture_color = self.capture_health_detail()
        ring_overrun, queue_overflow = self.diagnostic_drop_counts()
        history_first, history_second = self.diagnostic_history_lines()
        canvas.itemconfigure("sys_capture_rate", text=(f"{self.capture_rate / 1000:.0f}K/S" if self.capture_rate is not None else "WAITING"))
        canvas.itemconfigure("sys_header_rate", text=(f"{rate:.1f}/S" if rate is not None else "WAITING"))
        canvas.itemconfigure("sys_rotation", text=(f"{rpm:.2f}" if rpm is not None else "--.--"))
        canvas.itemconfigure("sys_rotation_detail", text=(f"±{spread / 2:.2f} RPM · SYNC {self.live_sync_count}/S" if spread is not None and self.live_sync_count is not None else self.rpm_quality_detail()))
        canvas.itemconfigure("sys_mechanism", text=f"{self.phase_event_count} STEPS")
        canvas.itemconfigure("sys_mechanism_detail", text=f"{self.head_var.get()} · POSITION {self.live_track}")
        canvas.itemconfigure("sys_media", text=self.disk_identity_label())
        canvas.itemconfigure("sys_media_detail", text=self.header_validation_detail())
        canvas.itemconfigure("sys_health", text=capture, fill=capture_color)
        canvas.itemconfigure("sys_health_detail", text=(f"ROV {ring_overrun} · QOV {queue_overflow} · CAP {self.capture_count}" if self.capture_count is not None else "STATUS PENDING"))
        canvas.itemconfigure("sys_history", text=history_first)
        canvas.itemconfigure("sys_history_2", text=history_second)

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
        # actually obey when the Controller has confirmed X2 ON.
        if self._confirmed_writable:
            return "FORCES WRITABLE"
        if self.live_protected is None:
            return "WAITING FOR SENSOR"
        return "PROTECTED" if self.live_protected else "WRITABLE"

    def hud_write_protect_detail(self) -> str:
        """Explain whether the displayed permission is sensor or override led."""
        if self._confirmed_writable:
            return "CONTROLLER OVERRIDE ACTIVE"
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
        self.control_button.configure(state="normal" if self.ub3.get() else "disabled")
        controller_id = self.drive_binding.controller_serial or "Not connected"
        hud_id = self.drive_binding.hud_serial or "Not connected"
        self.hud_connection.set(f"DriveHUD {hud_id} connected • Passive telemetry running" if self.ub4.get() else "DriveHUD not detected • HUD unavailable")
        self.control_status.set(f"Controller {controller_id} connected • Ready for controller commands" if self.ub3.get() else "Controller not detected • Control actions unavailable")
        state = []
        state.append(f"Controller: {controller_id}" if self.ub3.get() else "Controller: Not connected")
        state.append(f"DriveHUD: {hud_id}" if self.ub4.get() else "DriveHUD: Not connected")
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
            self.control_status.set(f"Startup ROM slot {slot} sent to the connected Controller.")
        except Exception as exc:
            self.control_status.set(f"Startup ROM was not changed: {exc}")

    def save_iec(self) -> None:
        choice = self.iec_choice.get()
        try:
            address = int(choice.split()[-1])
            self.send_controller_command(f"ROMIEC={address}")
            self.iec_var.set(choice)
            self.control_status.set(f"Boot IEC address {address} sent to the connected Controller.")
        except Exception as exc:
            self.control_status.set(f"IEC address was not changed: {exc}")

    def update_protection(self) -> None:
        desired = self.writable.get()
        try:
            self.send_controller_command("ROMWP=ON" if desired else "ROMWP=OFF")
            self._pending_wp_override = desired
            self.control_status.set("Verifying X2 write-protect override…")
        except Exception as exc:
            self.writable.set(self._confirmed_writable)
            self.control_status.set(f"Write-protect override was not changed: {exc}")

    def request_controller_wp_state(self) -> None:
        """Ask the Controller to report whether its X2 override is usable."""
        try:
            self.send_controller_command("ROMWP?")
            self.append_usb_log("SYSTEM", "Queried Controller X2 write-protect override state.")
        except Exception as exc:
            self.append_usb_log("SYSTEM", f"Controller X2 write-protect query was not sent: {exc}")

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
                    "Controller X2 override is ON — forces writable."
                    if reported_on else "Controller X2 override is OFF — normal protection."
                )
            else:
                self.control_status.set(fields.get("ERROR", "Controller reports X2 write-protect override unavailable."))
            self.update_live_hud_fields()
            return
        wanted = self._pending_wp_override
        verified = fields.get("VERIFY") == ("ON" if wanted else "OFF")
        if fields.get("STATUS") == "OK" and available and reported_on == wanted and verified:
            self._confirmed_writable = wanted
            self.writable.set(wanted)
            self.control_status.set(
                "Controller verified X2 override ON — drive forced writable."
                if wanted else "Controller verified X2 override OFF — normal protection restored."
            )
        else:
            self.writable.set(self._confirmed_writable)
            self.control_status.set(f"Controller did not verify X2 override: {line}")
        self._pending_wp_override = None
        self.update_live_hud_fields()

    def send_controller_command(self, command: str) -> None:
        """Send only the documented USB Selector commands to Controller."""
        link = self.usb_links.get("controller")
        if link is None or not link.connected:
            raise RuntimeError("Controller is not connected")
        link.write_command(command)

    def confirm_write_protect_toggle(self) -> None:
        """Show an in-display confirmation before changing it through UB3."""
        if not self.ub3.get():
            self.control_status.set("Write-protect control unavailable: controller is disconnected.")
            self.open_size_preview()
            return
        if not self.controller_wp_enabled.get():
            self.control_status.set("Write-protect control is disabled in this controller build.")
            self.open_size_preview()
            return
        self.write_protect_prompt = not self.writable.get()
        self.open_size_preview()

    def save_preview_rom(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: controller connection is unavailable.")
        elif not self.controller_rom_enabled.get():
            self.control_status.set("Save failed: Startup ROM control is disabled in this build.")
        else:
            self.save_rom()
        self.open_size_preview()

    def save_preview_iec(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: controller connection is unavailable.")
        elif not self.controller_iec_enabled.get():
            self.control_status.set("Save failed: IEC address control is disabled in this build.")
        else:
            self.save_iec()
        self.open_size_preview()

    def choose_from_menu(self, event, choices: tuple[str, ...], variable: tk.StringVar, message: str) -> None:
        self.popup_menu = {
            "kind": "choice",
            "title": "Choose value",
            "choices": choices,
            "variable": variable,
            "message": message,
        }
        self.open_size_preview()

    def choose_appearance_menu(self, event) -> None:
        """Choose which visual color family to edit from one touch menu."""
        self.popup_menu = {
            "kind": "appearance",
            "title": "Appearance",
            "choices": (
                ("Background", "background"),
                ("Group / Card Boxes", "group"),
                ("Button", "button"),
                ("Descriptions", "text1"),
                ("Values", "text2"),
            ),
        }
        self.open_size_preview()

    def return_to_appearance_menu(self) -> None:
        """Close Custom Color and restore its parent Appearance menu."""
        self.color_picker = None
        self.hex_keyboard = False
        self.popup_menu = {
            "kind": "appearance",
            "title": "Appearance",
            "choices": (
                ("Background", "background"),
                ("Group / Card Boxes", "group"),
                ("Button", "button"),
                ("Descriptions", "text1"),
                ("Values", "text2"),
            ),
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

    def change_preview_scale(self, delta: int) -> None:
        self.preview_scale.set(max(25, min(200, self.preview_scale.get() + delta)))
        self.open_size_preview()

    def set_preview_color(self, target: str, color: str) -> None:
        """Apply one visual preference to the touchscreen preview."""
        global BG, PANEL, ACCENT, MUTED, TEXT
        if target == "background":
            BG = color
        elif target == "group":
            PANEL = color
        elif target == "button":
            ACCENT = color
        elif target == "text1":
            MUTED = color
        elif target == "text2":
            TEXT = color
        self.color_picker = None
        self.hex_keyboard = False
        self.open_size_preview()

    def preview_color(self, target: str) -> str:
        """Return the currently active color for one appearance area."""
        return {
            "background": BG,
            "group": PANEL,
            "button": ACCENT,
            "text1": MUTED,
            "text2": TEXT,
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
        if self.preview_page in ("hud", "diagnostics") and not self.ub4.get():
            self.preview_page = "control" if self.ub3.get() else "setup"
        elif self.preview_page == "control" and not self.ub3.get():
            self.preview_page = "hud" if self.ub4.get() else "setup"
        elif self.preview_page == "setup" and (self.ub3.get() or self.ub4.get()):
            self.preview_page = "hud" if self.ub4.get() else "control"

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

    def open_size_preview(self, _restore_workspace: bool = False) -> None:
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
        ppi = float(self.preview_ppi.get()) * SEVEN_INCH_BASE_SCALE * (float(self.preview_scale.get()) / 100.0)
        width, height = round((155 / 25.4) * ppi), round((88 / 25.4) * ppi)
        geometry = f"{width}x{height}"
        if position:
            geometry += f"+{position[0]}+{position[1]}"
        if not reusing_preview:
            preview = tk.Toplevel(self)
            preview.title("1541 OneROM - 7-inch Touchscreen Simulator")
            preview.resizable(False, False)
            self.preview = preview
            canvas = tk.Canvas(preview, width=width, height=height, bg=BG, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            self.preview_canvas = canvas
        else:
            canvas = self.preview_canvas
            # Keep the existing native surface alive. Repainting this canvas
            # is instant and avoids the white flash from window destruction.
            canvas.configure(width=width, height=height, bg=BG)
            canvas.delete("all")
        preview.geometry(geometry)
        sx, sy = width / 1280, height / 720
        def text(x, y, value, size=16, fill=TEXT, bold=False, anchor="w", tag=None):
            canvas.create_text(x*sx, y*sy, text=value, fill=fill, anchor=anchor,
                               font=("Segoe UI Semibold" if bold else "Segoe UI", max(4, round(size*sy)), "normal"), tags=tag)
        def box(x1, y1, x2, y2, label, value="", value_size=28, value_y=None, value_tag=None):
            canvas.create_rectangle(x1*sx, y1*sy, x2*sx, y2*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            # 26 virtual pixels equals roughly 13 physical pixels at the
            # calibrated 7-inch scale: enough breathing room for touch UI.
            text(x1+26, y1+30, label.upper(), 18, MUTED, True)
            if value:
                canvas.create_text((x1+26)*sx, (value_y if value_y is not None else y1+78)*sy, text=value, fill=TEXT, anchor="w", font=("Cascadia Mono", max(7, round(value_size*sy)), "normal"), tags=value_tag)
        def draw_spinning_disk(phase: float) -> None:
            """A tiny 5.25-inch floppy with deliberately subtle motion marks."""
            canvas.delete("disk")
            # The live motor icon belongs to the dedicated status column.
            cx, cy, radius = 1190, 173, 35
            canvas.create_oval((cx-radius)*sx, (cy-radius)*sy, (cx+radius)*sx, (cy+radius)*sy,
                               fill="#28343b", outline="#82959b", width=max(1, round(2*sy)), tags="disk")
            canvas.create_oval((cx-15)*sx, (cy-15)*sy, (cx+15)*sx, (cy+15)*sy,
                               fill="#101820", outline="#a9bbc4", width=max(1, round(sy)), tags="disk")
            canvas.create_oval((cx-4)*sx, (cy-4)*sy, (cx+4)*sx, (cy+4)*sy,
                               fill="#d5e5e9", outline="", tags="disk")
            # A stopped drive is still physically present. Keep the platter
            # visible, but suppress the motion arrows.
            if not self.motor:
                return
            # Three curved arrows orbit the platter only while it is
            # running. Their absence makes a stopped disk unambiguous.
            active = ACCENT if int(phase * 5) % 2 == 0 else "#76ded0"
            arrow_radius = 47
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
        def draw_head_motion(phase: float) -> None:
            """Use foreshortened chevrons to show head motion in depth."""
            canvas.delete("head")
            # Chevrons represent current physical travel only.  A stalled
            # head is not moving, and a parked head has no active direction.
            if self.head_var.get() not in ("IN", "OUT"):
                return
            # Add one chevron per beat.  Four downward-pointing marks make
            # depth visible without changing the physical direction glyph.
            count = int(phase * (4 / 1.5)) % 4 + 1
            center_x = 1190
            for index in range(count):
                if self.head_direction == "IN":
                    y = 280 + index * 22
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
                    y = 348 - index * 22
                    scale = (1.0, 0.81, 0.63, 0.45)[index]
                    points = (
                        (center_x - 20 * scale, y + 11 * scale),
                        (center_x, y - 9 * scale),
                        (center_x + 20 * scale, y + 11 * scale),
                    )
                canvas.create_line(*(coordinate * (sx if pos % 2 == 0 else sy) for pos, coordinate in enumerate(sum((list(point) for point in points), []))),
                                   fill=ACCENT, width=max(1, round((2 + 3 * scale)*sy)), joinstyle="round", tags="head")
        text(26, 34, f"1541 OneROM {APP_VERSION}", 22, TEXT, True)
        # Setup and Hardware Setup both provide explicit navigation.  Keep
        # the header gear only on the operational/status screens.
        if self.preview_page not in ("setup", "settings", "log"):
            # Center the larger options gear between the frame top and the
            # aligned HUD/Controller card grid, whose top edge is y=96.
            text(1254, 48, "⚙", 38, ACCENT, True, "e")
        if self.preview_page == "control" and self.ub4.get():
            canvas.create_rectangle(990*sx, 18*sy, 1170*sx, 66*sy, fill=PANEL_ALT, outline=ACCENT)
            text(1080, 42, "HUD", 15, TEXT, True, "center")
        if self.preview_page == "hud":
            text(26, 76, "DriveHUD · passive measurements · tap P# to set display priority", 16, MUTED)
            # A compact list leaves a dedicated right-hand status column.
            cards = self.scroll_hud_cards()
            max_start = max(0, len(cards) - 6)
            self.hud_scroll_index = min(self.hud_scroll_index, max_start)
            visible = cards[self.hud_scroll_index:self.hud_scroll_index + 6]
            for index, card in enumerate(visible):
                # Preserve a clean gap below the subtitle while slightly
                # compressing each row so the footer retains its margin.
                y1 = 100 + index * 96
                y2 = y1 + 92
                canvas.create_rectangle(24*sx, y1*sy, 920*sx, y2*sy, fill=PANEL, outline=ACCENT, width=1)
                text(48, y1 + 19, str(card["title"]).upper(), 14, MUTED, True)
                value, detail = self.scroll_card_paint_text(card)
                display_value = value if len(value) <= 24 else f"{value[:21]}..."
                # Values retain a consistent visual weight across every
                # card.  Supporting text begins well to their right.
                if card["id"] == "recent_evidence":
                    text(48, y1 + 52, value, 15, TEXT, True, tag=f"scroll_{card['id']}_value")
                    text(48, y1 + 78, detail, 15, TEXT, True, tag=f"scroll_{card['id']}_detail")
                else:
                    text(48, y1 + 60, display_value, 24, TEXT, True, tag=f"scroll_{card['id']}_value")
                    text(400, y1 + 60, detail, 14, MUTED, True, tag=f"scroll_{card['id']}_detail")
                priority = card["priority"]
                # Help precedes the display priority, matching the natural
                # left-to-right reading order: what it means, then its rank.
                # Large, finger-friendly actions occupy the right edge of
                # every card without reducing the primary value area.
                canvas.create_rectangle(746*sx, (y1 + 14)*sy, 818*sx, (y1 + 78)*sy, fill=PANEL_ALT, outline=ACCENT)
                text(782, y1 + 50, "?", 25, ACCENT, True, "center")
                canvas.create_rectangle(826*sx, (y1 + 14)*sy, 908*sx, (y1 + 78)*sy, fill=PANEL_ALT, outline=ACCENT)
                text(867, y1 + 50, f"P{priority}" if priority else "P–", 18, TEXT, True, "center")
            # Four finger-sized scrolling controls. The symbols intentionally
            # omit their former 5/1 labels: direction alone is clearer.
            for y1, label in ((100, "⇑"), (244, "↑"), (388, "↓"), (532, "⇓")):
                canvas.create_rectangle(934*sx, y1*sy, 1006*sx, (y1 + 140)*sy, fill=PANEL_ALT, outline=ACCENT)
                text(970, y1 + 70, label, 38, TEXT, True, "center")
            # Persistent, centered status cards live at the far right.
            status_cards = ((100, 240, "MOTOR"), (244, 384, "HEAD"), (388, 528, "WRITE PROTECT"), (532, 672, "CONTROLLER"))
            for y1, y2, label in status_cards:
                canvas.create_rectangle(1020*sx, y1*sy, 1256*sx, y2*sy, fill=PANEL, outline=ACCENT, width=1)
                text(1044, y1 + 20, label, 14, MUTED, True)
            draw_spinning_disk(time.monotonic())
            # HUD values share one consistent type scale; the labels and
            # icons remain visually distinct without looking like data.
            text(1100, 173, "ON" if self.motor else "OFF", 24, TEXT, True, "center")
            head_state = self.head_var.get()
            # Keep the state label left and the animated motion cue right so
            # IN/OUT remains readable alongside the working chevrons.
            text(1044, 317, head_state, 24, TEXT, True, "w", tag="hud_head_state")
            if head_state in ("IN", "OUT"):
                draw_head_motion(time.monotonic())
            # Red W/O is reserved for the active Controller override. The
            # line below continues to report the physical disk sensor state.
            wp_color = OFFLINE if self._confirmed_writable else ACCENT
            text(1138, 460, "W/O", 24, wp_color, True, "center", tag="hud_wp_override")
            text(1138, 504, self.physical_disk_write_status(), 14, TEXT, True, "center", tag="hud_disk_status")
            text(1133, 608, "▶", 58, ACCENT if self.ub3.get() else MUTED, True, "center")
            telemetry_label = "TELEMETRY ON" if self._hud_telemetry_enabled else "TELEMETRY WAITING"
            text(24, 698, f"SHOWING {self.hud_scroll_index + 1}–{min(self.hud_scroll_index + 6, len(cards))} OF {len(cards)} · {telemetry_label} · P1–P6 PINNED · ? HELP", 14, ACCENT, True)
            if self.hud_help_card:
                card = next((entry for entry in cards if entry["id"] == self.hud_help_card), None)
                if card:
                    canvas.create_rectangle(120*sx, 218*sy, 1090*sx, 486*sy, fill=BG, outline=ACCENT, width=max(1, round(2*sy)))
                    text(156, 260, str(card["title"]).upper(), 24, TEXT, True)
                    text(156, 310, str(card["help"]), 18, MUTED, False)
                    text(156, 386, "SOURCE: PASSIVE DRIVEHUD TELEMETRY · TAP ANYWHERE TO CLOSE", 15, ACCENT, True)
                    if card["id"] == "capture_health":
                        # Health counters are displayed relative to a local
                        # baseline, so Clear starts a fresh diagnostic window
                        # without transmitting anything to the drive.
                        canvas.create_rectangle(888*sx, 430*sy, 1054*sx, 466*sy, fill=PANEL_ALT, outline=ACCENT)
                        text(971, 448, "CLEAR DROPS", 14, TEXT, True, "center")
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
        elif self.preview_page == "diagnostics":
            text(26, 76, "Drive Diagnostics · passive measurements", 16, MUTED)
            box(24, 96, 420, 244, "Mechanical Position")
            canvas.create_text(50*sx, 172*sy, text=self.live_track, fill=TEXT, anchor="w",
                               font=("Cascadia Mono", max(7, round(35*sy)), "normal"), tags="diag_position")
            offset = self.header_offset()
            text(50, 220, f"HEADER Δ {offset:+.1f}" if offset is not None else "HEADER WAITING", 15, MUTED, True, tag="diag_offset")
            box(440, 96, 820, 244, "Physical Header")
            header = f"T{self.last_header_track:02d} S{self.live_sector:02d}" if self.last_header_track is not None and self.live_sector is not None else "WAITING"
            canvas.create_text(466*sx, 172*sy, text=header, fill=TEXT, anchor="w",
                               font=("Cascadia Mono", max(7, round(35*sy)), "normal"), tags="diag_header")
            text(466, 220, "PHYSICAL HEADER" if self.last_header_track is not None else "NO CONFIRMED HEADER", 15, MUTED, True, tag="diag_header_detail")
            box(840, 96, 1256, 244, "Rotation")
            displayed_rpm = self.effective_rpm()
            canvas.create_text(866*sx, 172*sy, text=f"{displayed_rpm:.2f}" if displayed_rpm is not None else "--.--", fill=TEXT, anchor="w",
                               font=("Cascadia Mono", max(7, round(35*sy)), "normal"), tags="diag_rpm")
            text(866, 220, self.rpm_quality_detail(), 15, MUTED, True, tag="diag_rpm_detail")
            box(24, 260, 420, 408, "Sector Coverage")
            expected = self.expected_sector_count()
            coverage = f"SEEN {len(set(self.recent_sectors))}/{expected}" if expected else "WAITING"
            text(50, 336, coverage, 31, TEXT, True, tag="diag_coverage")
            text(50, 384, f"D{self.live_density} ZONE" if self.live_density is not None else "DENSITY UNKNOWN", 15, MUTED, True, tag="diag_coverage_detail")
            box(440, 260, 820, 408, "Activity")
            text(466, 336, self.disk_activity_label(), 31, TEXT, True, tag="diag_activity")
            text(466, 384, f"WRITE PULSES {self.write_pulse_count} · STEPS {self.phase_event_count}", 15, MUTED, True, tag="diag_activity_detail")
            box(840, 260, 1256, 408, "Disk ID / System")
            text(866, 336, self.disk_identity_label(), 35, TEXT, True, tag="diag_media")
            text(866, 384, "TAP FOR SYSTEM DIAGNOSTICS", 14, ACCENT, True)
            box(24, 424, 420, 572, "HUD Health")
            capture, capture_color = self.capture_health_detail()
            text(50, 500, capture, 27, capture_color, True, tag="diag_capture")
            # Keep the local diagnostic reset in the header band so it does
            # not collide with long health states such as WAITING FOR STATUS.
            canvas.create_rectangle(284*sx, 434*sy, 396*sx, 466*sy, fill=PANEL_ALT, outline=ACCENT)
            text(340, 450, "CLEAR", 14, TEXT, True, "center")
            ring_overrun, queue_overflow = self.diagnostic_drop_counts()
            capture_detail = f"CAP {self.capture_count} · ROV {ring_overrun} · QOV {queue_overflow}" if self.capture_count is not None else "STATUS PENDING"
            text(50, 548, capture_detail, 14, MUTED, True, tag="diag_capture_detail")
            box(440, 424, 1256, 572, "Recent Evidence")
            history_first, history_second = self.diagnostic_history_lines()
            text(466, 486, history_first, 17, TEXT, True, tag="diag_history")
            text(466, 516, history_second, 17, TEXT, True, tag="diag_history_2")
            # Match every other card's lower-detail baseline: 24 virtual
            # pixels above the lower edge of this 148-pixel-high card.
            # This is a capability note, not an active fault; keep it quiet.
            text(466, 548, "DOS ERROR / RETRY DATA UNAVAILABLE", 13, MUTED)
            text(28, 612, self.hud_connection.get(), 16, ACCENT if self.ub4.get() else OFFLINE)
        elif self.preview_page == "diagnostics_detail":
            text(26, 76, "System Diagnostics · passive measurements", 16, MUTED)
            box(24, 96, 420, 244, "Capture Rate")
            text(50, 172, f"{self.capture_rate / 1000:.0f}K/S" if self.capture_rate is not None else "WAITING", 35, TEXT, True, tag="sys_capture_rate")
            text(50, 220, "PASSIVE CAPTURE EVENTS PER SECOND", 15, MUTED, True)
            box(440, 96, 820, 244, "Header Rate")
            header_rate = self.header_rate()
            text(466, 172, f"{header_rate:.1f}/S" if header_rate is not None else "WAITING", 35, TEXT, True, tag="sys_header_rate")
            text(466, 220, "DECODED PHYSICAL HEADERS", 15, MUTED, True)
            box(840, 96, 1256, 244, "Rotation Quality")
            displayed_rpm = self.effective_rpm()
            text(866, 172, f"{displayed_rpm:.2f}" if displayed_rpm is not None else "--.--", 35, TEXT, True, tag="sys_rotation")
            text(866, 220, self.rpm_quality_detail(), 15, MUTED, True, tag="sys_rotation_detail")
            box(24, 260, 420, 408, "Mechanism")
            text(50, 336, f"{self.phase_event_count} STEPS", 31, TEXT, True, tag="sys_mechanism")
            text(50, 384, f"{self.head_var.get()} · POSITION {self.live_track}", 15, MUTED, True, tag="sys_mechanism_detail")
            box(440, 260, 820, 408, "Media Validation")
            text(466, 336, self.disk_identity_label(), 31, TEXT, True, tag="sys_media")
            text(466, 384, self.header_validation_detail(), 15, MUTED, True, tag="sys_media_detail")
            box(840, 260, 1256, 408, "Capture Integrity")
            capture, capture_color = self.capture_health_detail()
            text(866, 336, capture, 31, capture_color, True, tag="sys_health")
            ring_overrun, queue_overflow = self.diagnostic_drop_counts()
            text(866, 384, f"ROV {ring_overrun} · QOV {queue_overflow}", 15, MUTED, True, tag="sys_health_detail")
            box(24, 424, 420, 572, "Observability")
            text(50, 500, "PASSIVE", 27, ACCENT, True)
            text(50, 548, "DOS ERRORS / RETRIES NOT EXPOSED", 13, MUTED)
            box(440, 424, 1256, 572, "Recent Evidence")
            history_first, history_second = self.diagnostic_history_lines()
            text(466, 486, history_first, 17, TEXT, True, tag="sys_history")
            text(466, 516, history_second, 17, TEXT, True, tag="sys_history_2")
            text(466, 548, "HEADER/RPM/STEP DATA FROM UB4 PASSIVE CAPTURE", 13, MUTED)
            text(28, 612, self.hud_connection.get(), 16, ACCENT if self.ub4.get() else OFFLINE)
        elif self.preview_page == "control":
            text(26, 76, "OneROM Controller", 16, MUTED)
            if self.controller_rom_enabled.get():
                box(24, 96, 760, 318, "Startup ROM", self.rom_choice.get())
                canvas.create_polygon(
                    696*sx, 120*sy,
                    736*sx, 120*sy,
                    716*sx, 150*sy,
                    fill=ACCENT,
                    outline="",
                )
                text(50, 232, f"Saved: {self.rom_var.get()}", 15, MUTED)
                canvas.create_rectangle(454*sx, 251*sy, 732*sx, 303*sy, fill=ACCENT, outline="")
                text(593, 277, "SAVE ROM", 17, BG, True, "center")
                text(50, 292, "MENU", 14, MUTED, True)
            else:
                box(24, 96, 760, 318, "Startup ROM", "NOT ENABLED")
                text(50, 232, "Enable in Settings / controller JSON build.", 15, MUTED)
            if self.controller_iec_enabled.get():
                box(784, 96, 1256, 318, "Boot IEC Address", self.iec_choice.get())
                canvas.create_polygon(
                    1192*sx, 120*sy,
                    1232*sx, 120*sy,
                    1212*sx, 150*sy,
                    fill=ACCENT,
                    outline="",
                )
                text(810, 232, f"Saved: {self.iec_var.get()}", 15, MUTED)
                canvas.create_rectangle(1010*sx, 251*sy, 1228*sx, 303*sy, fill=ACCENT, outline="")
                text(1119, 277, "SAVE IEC", 17, BG, True, "center")
                text(810, 292, "MENU", 14, MUTED, True)
            else:
                box(784, 96, 1256, 318, "Boot IEC Address", "NOT ENABLED")
                text(810, 232, "Enable in Settings / controller JSON build.", 15, MUTED)
            if self.controller_wp_enabled.get():
                box(24, 344, 760, 566, "Write-Protect Override", "FORCES WRITABLE" if self.writable.get() else "NORMAL PROTECTION")
                action = "Disable override" if self.writable.get() else "Enable writable override"
                canvas.create_rectangle(342*sx, 499*sy, 732*sx, 551*sy, fill=OFFLINE if self.writable.get() else ACCENT, outline="")
                text(537, 525, action.upper(), 17, BG, True, "center")
            else:
                box(24, 344, 760, 566, "Write-Protect Override", "NOT ENABLED")
                text(50, 486, "Enable in Settings / controller JSON build.", 15, MUTED)
            # Reserved blank card for future controller status content.
            canvas.create_rectangle(784*sx, 344*sy, 1256*sx, 566*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            text(28, 612, self.control_status.get(), 16, ACCENT if self.ub3.get() else OFFLINE)
        elif self.preview_page == "settings":
            text(26, 76, "Settings", 16, MUTED)
            canvas.create_rectangle(24*sx, 96*sy, 620*sx, 214*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            canvas.create_rectangle(660*sx, 96*sy, 1256*sx, 214*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            text(50, 126, "CONTROLLER-ROLE ONEROM", 18, MUTED, True)
            text(50, 172, "CONNECTED" if self.ub3.get() else "NOT CONNECTED", 26, ACCENT if self.ub3.get() else OFFLINE, True)
            text(686, 126, "HUD-ROLE ONEROM", 18, MUTED, True)
            text(686, 172, "CONNECTED" if self.ub4.get() else "NOT CONNECTED", 26, ACCENT if self.ub4.get() else OFFLINE, True)
            text(50, 202, "Tap to open Controller connection setup", 13, MUTED)
            text(686, 202, "Tap to open DriveHUD connection setup", 13, MUTED)
            text(26, 246, "Either physical OneROM can be assigned as Controller or DriveHUD for this drive.", 14, MUTED)
            # Controller choices are a left-hand stack, exactly aligned with
            # the Controller-role status card. Appearance choices mirror it
            # on the right, aligned with the HUD-role status card.
            text(24, 274, "Controller options", 16, MUTED, True)
            text(660, 274, "Appearance", 16, MUTED, True)
            for y1, label, variable in (
                (294, "Startup ROM", self.controller_rom_enabled),
                (360, "IEC address", self.controller_iec_enabled),
                (426, "Write-protect", self.controller_wp_enabled),
            ):
                canvas.create_rectangle(24*sx, y1*sy, 620*sx, (y1+56)*sy, fill=PANEL, outline=PANEL_ALT)
                canvas.create_rectangle(46*sx, (y1+12)*sy, 78*sx, (y1+44)*sy,
                                        fill=ACCENT if variable.get() else PANEL_ALT, outline=ACCENT)
                if variable.get():
                    text(62, y1+28, "✓", 19, BG, True, "center")
                text(96, y1+28, label, 18, TEXT, True)
            box(660, 294, 1256, 482, "Appearance")
            # The small triangle is the touch affordance for this menu card.
            canvas.create_polygon(
                1202*sx, 318*sy,
                1238*sx, 318*sy,
                1220*sx, 344*sy,
                fill=ACCENT,
                outline="",
            )
            text(686, 456, "MENU", 14, MUTED, True)
            text(24, 514, "Controller feature availability mirrors the configuration JSON used when the board is compiled.", 13, MUTED)
            # Kept available for the Windows simulator, but tucked below the
            # configuration grid so it will not dominate the fixed 7-inch UI.
            canvas.create_rectangle(24*sx, 540*sy, 1256*sx, 624*sy, fill=PANEL, outline=PANEL_ALT)
            text(44, 562, "DISPLAY SCALE", 14, MUTED, True)
            text(44, 590, f"{self.preview_scale.get()}%", 20, TEXT, True)
            # Keep the scale control in the open space between its label and
            # the adjustment buttons, rather than low against the footer.
            text(200, 574, "25%", 14, MUTED, False, "center")
            text(860, 574, "200%", 14, MUTED, False, "center")
            canvas.create_line(200*sx, 594*sy, 860*sx, 594*sy, fill=MUTED, width=max(1, round(5*sy)))
            knob_x = 200 + (self.preview_scale.get() - 25) / 175 * 660
            canvas.create_oval((knob_x-12)*sx, 582*sy, (knob_x+12)*sx, 606*sy, fill=ACCENT, outline="")
            canvas.create_rectangle(900*sx, 556*sy, 1060*sx, 608*sy, fill=PANEL_ALT, outline="")
            canvas.create_rectangle(1080*sx, 556*sy, 1240*sx, 608*sy, fill=ACCENT, outline="")
            text(980, 582, "− 1%", 17, TEXT, True, "center")
            text(1160, 582, "+ 1%", 17, BG, True, "center")
        elif self.preview_page in ("controller_connection", "hud_connection"):
            role = "controller" if self.preview_page == "controller_connection" else "hud"
            role_title = "CONTROLLER" if role == "controller" else "DRIVEHUD"
            serial_var = self.controller_serial if role == "controller" else self.hud_serial
            text(26, 76, f"{role_title.title()} OneROM Communication Setup", 16, MUTED)
            box(24, 96, 1256, 188, f"{role_title} ROLE", serial_var.get() or "NO ONEROM SELECTED", value_size=23, value_y=158)
            canvas.create_rectangle(24*sx, 210*sy, 342*sx, 262*sy, fill=PANEL_ALT, outline="")
            text(183, 236, "REFRESH USB DEVICES", 17, TEXT, True, "center")
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
                text(24, 290, "UNASSIGNED ONEROM USB DEVICES", 16, MUTED, True)
                for index, board in enumerate(boards[:4]):
                    y = 310 + index * 62
                    selected = board.serial_number == serial_var.get()
                    canvas.create_rectangle(24*sx, y*sy, 1256*sx, (y+52)*sy,
                                            fill=ACCENT if selected else PANEL, outline=PANEL_ALT)
                    text(48, y+26, board.serial_number, 18, BG if selected else TEXT, True)
                    text(1228, y+26, board.port, 16, BG if selected else MUTED, False, "e")
            else:
                box(24, 290, 1256, 434, "USB DEVICES", "NO UNASSIGNED ONEROM FOUND", value_size=24)
                text(50, 394, "Connect a new board, or release an existing binding.", 16, MUTED)
            canvas.create_rectangle(24*sx, 548*sy, 350*sx, 600*sy, fill=PANEL_ALT, outline="")
            text(187, 574, "← BACK TO SETTINGS", 17, TEXT, True, "center")
            canvas.create_rectangle(386*sx, 548*sy, 812*sx, 600*sy, fill=OFFLINE, outline="")
            text(599, 574, f"RELEASE {role_title} BINDING", 17, BG, True, "center")
            canvas.create_rectangle(900*sx, 548*sy, 1228*sx, 600*sy, fill=ACCENT, outline="")
            text(1064, 574, f"CONNECT {role_title}", 17, BG, True, "center")
            text(24, 470, self.usb_status.get(), 18, ACCENT if boards else MUTED)
        elif self.preview_page == "log":
            text(26, 70, "USB Communications Log", 16, MUTED)
            # Match the top elevation of the main HUD and Controller cards.
            canvas.create_rectangle(24*sx, 96*sy, 1256*sx, 616*sy, fill=PANEL, outline=PANEL_ALT)
            text(48, 112, "LIVE CDC / CONNECTION HISTORY", 16, MUTED, True)
            # Use the same 300 x 52 touch-button standard as the footer and
            # Controller actions, with all right-side actions aligned.
            # The copy action lives in the right-aligned header band.
            canvas.create_rectangle(636*sx, 22*sy, 936*sx, 74*sy, fill=OFFLINE, outline="")
            text(786, 48, "CLEAR LOG", 17, BG, True, "center")
            canvas.create_rectangle(956*sx, 22*sy, 1256*sx, 74*sy, fill=ACCENT, outline="")
            text(1106, 48, "COPY LOG", 17, BG, True, "center")
            visible_lines = 20
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
            text(48, 590, f"{len(self.usb_log_lines)} entries · scroll {self.log_scroll}/{max_scroll}", 14, MUTED)
        else:
            text(26, 76, "OneROM Setup", 16, MUTED)
            box(24, 120, 1256, 410, "No OneROM role configured", "OPEN SETTINGS")
            text(50, 330, "Open Settings to connect the Controller and DriveHUD OneROMs for this drive.", 18, MUTED)
        if self.preview_page not in ("hud", "control"):
            canvas.create_rectangle(24*sx, 632*sy, 1256*sx, 710*sy, fill=PANEL_ALT, outline="")
        # Deliberately large touch targets.  Only offer a destination that is
        # actually installed: the compact UI should never expose a dead tab.
        if self.preview_page == "diagnostics":
            canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
            text(184, 671, "HUD", 17, TEXT, True, "center")
            if self.ub3.get():
                canvas.create_rectangle(346*sx, 645*sy, 646*sx, 697*sy, fill=PANEL, outline="")
                text(496, 671, "CONTROL", 17, TEXT, True, "center")
        elif self.preview_page == "diagnostics_detail":
            canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
            text(184, 671, "HUD", 17, TEXT, True, "center")
            canvas.create_rectangle(346*sx, 645*sy, 646*sx, 697*sy, fill=PANEL, outline="")
            text(496, 671, "DIAGNOSTICS", 17, TEXT, True, "center")
        elif self.preview_page == "control" and self.ub4.get():
            canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
            text(184, 671, "HUD", 17, TEXT, True, "center")
        elif self.preview_page == "settings":
            if self.ub4.get():
                canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
                text(184, 671, "HUD", 17, TEXT, True, "center")
            if self.ub3.get():
                x1, x2 = (346, 646) if self.ub4.get() else (34, 334)
                canvas.create_rectangle(x1*sx, 645*sy, x2*sx, 697*sy, fill=PANEL, outline="")
                text((x1+x2)/2, 671, "CONTROL", 17, TEXT, True, "center")
        # USB diagnostics belong on Hardware Setup, not on the normal HUD or
        # Controller operator screens.
        if self.preview_page == "settings":
            canvas.create_rectangle(658*sx, 645*sy, 958*sx, 697*sy, fill=PANEL, outline="")
            text(808, 671, "LOG", 17, TEXT, True, "center")
        elif self.preview_page == "log":
            canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
            text(184, 671, "← BACK", 17, TEXT, True, "center")
            canvas.create_rectangle(346*sx, 645*sy, 646*sx, 697*sy, fill=PANEL, outline="")
            canvas.create_rectangle(658*sx, 645*sy, 958*sx, 697*sy, fill=PANEL, outline="")
            text(496, 671, "▲ OLDER", 17, TEXT, True, "center")
            text(808, 671, "▼ NEWER", 17, TEXT, True, "center")
        if self.preview_page in ("diagnostics", "diagnostics_detail"):
            # The status at the far right needs its own stable space.  Keep
            # this deliberately short so it never encroaches on navigation.
            # Left-align the footer descriptor with the available content
            # lane immediately after Control, instead of floating mid-lane.
            text(668, 680, "PASSIVE LIVE DATA", 15, MUTED, True)
        if self.preview_page == "hud":
            footer_status = "● DriveHUD connected"
        elif self.preview_page == "control":
            footer_status = "● Controller connected"
        elif self.preview_page == "diagnostics":
            footer_status = "● DriveHUD diagnostics"
        elif self.preview_page == "diagnostics_detail":
            footer_status = "● DriveHUD system diagnostics"
        elif self.preview_page == "settings":
            footer_status = "● Hardware setup"
        elif self.preview_page in ("controller_connection", "hud_connection"):
            controller_state = "connected" if self.ub3.get() else "unassigned"
            hud_state = "connected" if self.ub4.get() else "unassigned"
            footer_status = f"● Controller {controller_state} · DriveHUD {hud_state}"
        elif self.preview_page == "log":
            footer_status = "● USB Log"
        else:
            footer_status = "● No boards configured"
        if self.preview_page not in ("hud", "control"):
            diagnostic = self.serial_last_error.get()
            if diagnostic:
                text(1240, 680, f"USB ERROR: {diagnostic[:88]}", 14, OFFLINE, True, "e")
            else:
                text(1240, 680, footer_status, 20, ACCENT if self.preview_page != "setup" else MUTED, True, "e")
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
                                        fill=PANEL_ALT, outline="")
                text(x1+42, row_y + row_height / 2, label, 17, TEXT, True)
            canvas.create_rectangle((x2-182)*sx, (y1+20)*sy, (x2-24)*sx, (y1+72)*sy, fill=BG, outline="")
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
            canvas.create_rectangle(1050*sx, 30*sy, 1248*sx, 82*sy, fill=PANEL_ALT, outline="")
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
                        canvas.create_rectangle(x1*sx, y1*sy, (x1+key_width)*sx, (y1+52)*sy, fill=fill, outline="")
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
            canvas.create_rectangle(340*sx, 414*sy, 600*sx, 466*sy, fill=PANEL_ALT, outline="")
            canvas.create_rectangle(680*sx, 414*sy, 940*sx, 466*sy, fill=WARNING if enabling else ACCENT, outline="")
            text(470, 440, "CANCEL", 17, TEXT, True, "center")
            text(810, 440, "CONFIRM", 17, BG, True, "center")
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
            if self.write_protect_prompt is not None:
                if 680 <= x <= 940 and 414 <= y <= 466:
                    self.writable.set(self.write_protect_prompt)
                    self.write_protect_prompt = None
                    self.update_protection()
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
                    if self.priority_prompt_value:
                        self.hud_card_priorities[self.priority_prompt_card] = int(self.priority_prompt_value)
                        self.hud_scroll_index = 0
                    self.priority_prompt_card = None
                    self.priority_prompt_value = ""
                self.open_size_preview()
                return
            if self.preview_page == "hud" and self.hud_help_card is not None:
                if self.hud_help_card == "capture_health" and 888 <= x <= 1054 and 430 <= y <= 466:
                    self.clear_diagnostic_drops()
                self.hud_help_card = None
                self.open_size_preview()
                return
            if self.preview_page not in ("setup", "settings", "log") and y < 76 and x > 1180:
                self.preview_page = "settings"
            elif self.preview_page == "log":
                if 636 <= x <= 936 and 22 <= y <= 74:
                    self.clear_usb_log()
                    return
                if 956 <= x <= 1256 and 22 <= y <= 74:
                    self.copy_usb_log()
                    return
                if 346 <= x <= 646 and 645 <= y <= 697:
                    self.log_scroll = min(max(0, len(self.usb_log_lines) - 20), self.log_scroll + 10)
                elif 658 <= x <= 958 and 645 <= y <= 697:
                    self.log_scroll = max(0, self.log_scroll - 10)
                elif 12 <= x <= 370 and 630 <= y <= 710:
                    self.preview_page = self.log_return_page
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 640 <= x <= 976 and 630 <= y <= 710:
                self.open_usb_log()
                return
            elif self.preview_page in ("controller_connection", "hud_connection"):
                role = "controller" if self.preview_page == "controller_connection" else "hud"
                serial_var = self.controller_serial if role == "controller" else self.hud_serial
                if 24 <= x <= 342 and 210 <= y <= 262:
                    self.refresh_usb_boards()
                elif 12 <= x <= 370 and 520 <= y <= 620:
                    # This screen is a fixed physical 7-inch workflow.  Do
                    # not carry an accidental preview-scale adjustment back
                    # into the main settings layout.
                    self.preview_scale.set(100)
                    self.preview_page = "settings"
                elif 370 <= x <= 830 and 520 <= y <= 620:
                    self.release_role_binding(role)
                elif 880 <= x <= 1248 and 520 <= y <= 620:
                    self.connect_assigned_boards(role)
                elif 24 <= x <= 1256 and 310 <= y <= 548:
                    index = int((y - 310) // 62)
                    assigned_serials = {
                        serial for serial in (self.drive_binding.controller_serial, self.drive_binding.hud_serial) if serial
                    }
                    boards = [board for board in self.usb_boards.values() if board.serial_number not in assigned_serials]
                    if 0 <= index < len(boards) and y <= 310 + index * 62 + 52:
                        serial_var.set(boards[index].serial_number)
                        self.usb_status.set(f"Selected {boards[index].serial_number}. Tap CONNECT {role.upper()} to save and open the link.")
                self.open_size_preview()
                return
            elif self.preview_page == "setup" and 24 <= x <= 1256 and 120 <= y <= 410:
                self.preview_page = "settings"
            elif self.preview_page == "settings" and 24 <= x <= 620 and 96 <= y <= 214:
                self.open_desktop_connection_setup("controller")
                return
            elif self.preview_page == "settings" and 660 <= x <= 1256 and 96 <= y <= 214:
                self.open_desktop_connection_setup("hud")
                return
            elif self.preview_page == "settings" and 660 <= x <= 1256 and 294 <= y <= 482:
                self.choose_appearance_menu(event)
                return
            elif self.preview_page == "hud" and self.ub3.get() and 990 <= x <= 1170 and 18 <= y <= 66:
                self.preview_page = "control"
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 1020 <= x <= 1256 and 532 <= y <= 672:
                if self.ub3.get():
                    self.preview_page = "control"
                else:
                    self.usb_status.set("Controller is not connected.")
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 934 <= x <= 1006:
                if 100 <= y <= 240:
                    self.scroll_hud_by(-5)
                elif 244 <= y <= 384:
                    self.scroll_hud_by(-1)
                elif 388 <= y <= 528:
                    self.scroll_hud_by(1)
                elif 532 <= y <= 672:
                    self.scroll_hud_by(5)
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 24 <= x <= 920 and 100 <= y <= 672:
                visible = self.scroll_hud_cards()[self.hud_scroll_index:self.hud_scroll_index + 6]
                index = int((y - 100) // 96)
                if 0 <= index < len(visible):
                    card = visible[index]
                    row_y = 100 + index * 96
                    if 746 <= x <= 818 and row_y + 14 <= y <= row_y + 78:
                        self.hud_help_card = str(card["id"])
                    elif 826 <= x <= 908 and row_y + 14 <= y <= row_y + 78:
                        self.priority_prompt_card = str(card["id"])
                        priority = card["priority"]
                        self.priority_prompt_value = str(priority) if priority is not None else ""
                    self.open_size_preview()
                    return
            elif self.preview_page == "diagnostics" and 284 <= x <= 396 and 434 <= y <= 466:
                self.clear_diagnostic_drops()
                return
            elif self.preview_page == "diagnostics" and 840 <= x <= 1256 and 260 <= y <= 408:
                self.preview_page = "diagnostics_detail"
                self.open_size_preview()
                return
            elif self.preview_page == "diagnostics" and 645 <= y <= 697 and 34 <= x <= 334:
                self.preview_page = "hud"
            elif self.preview_page == "diagnostics" and self.ub3.get() and 645 <= y <= 697 and 346 <= x <= 646:
                self.preview_page = "control"
            elif self.preview_page == "diagnostics_detail" and 645 <= y <= 697 and 34 <= x <= 334:
                self.preview_page = "hud"
            elif self.preview_page == "diagnostics_detail" and 645 <= y <= 697 and 346 <= x <= 646:
                self.preview_page = "diagnostics"
            elif self.preview_page == "control" and self.ub4.get() and 990 <= x <= 1170 and 18 <= y <= 66:
                self.preview_page = "hud"
            elif self.preview_page == "control" and self.ub4.get() and 645 <= y <= 697 and 34 <= x <= 334:
                self.preview_page = "hud"
            elif self.preview_page == "settings" and self.ub4.get() and 645 <= y <= 697 and 34 <= x <= 334:
                self.preview_page = "hud"
            elif self.preview_page == "settings" and self.ub3.get() and 645 <= y <= 697 and ((self.ub4.get() and 346 <= x <= 646) or (not self.ub4.get() and 34 <= x <= 334)):
                self.preview_page = "control"
            elif self.preview_page == "control" and self.controller_rom_enabled.get() and 454 <= x <= 732 and 251 <= y <= 303:
                self.save_preview_rom()
                return
            elif self.preview_page == "control" and self.controller_iec_enabled.get() and 1010 <= x <= 1228 and 251 <= y <= 303:
                self.save_preview_iec()
                return
            elif self.preview_page == "control" and self.controller_rom_enabled.get() and 24 <= x <= 760 and 96 <= y <= 318:
                self.choose_from_menu(event, self.rom_choices, self.rom_choice, "Startup ROM selected: {value}. Save applies it to the connected controller.")
                return
            elif self.preview_page == "control" and self.controller_iec_enabled.get() and 784 <= x <= 1256 and 96 <= y <= 318:
                self.choose_from_menu(event, self.iec_choices, self.iec_choice, "Boot IEC address selected: {value}. Save applies it to the connected controller.")
                return
            elif self.preview_page == "control" and self.controller_wp_enabled.get() and 342 <= x <= 732 and 499 <= y <= 551:
                self.confirm_write_protect_toggle()
                return
            elif self.preview_page == "settings" and 200 <= x <= 860 and 578 <= y <= 610:
                self.preview_scale.set(max(25, min(200, round(25 + ((x - 200) / 660) * 175))))
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 900 <= x <= 1060 and 556 <= y <= 608:
                self.change_preview_scale(-1)
                return
            elif self.preview_page == "settings" and 1080 <= x <= 1240 and 556 <= y <= 608:
                self.change_preview_scale(1)
                return
            elif self.preview_page == "settings" and 24 <= x <= 620 and 294 <= y <= 350:
                self.controller_rom_enabled.set(not self.controller_rom_enabled.get())
            elif self.preview_page == "settings" and 24 <= x <= 620 and 360 <= y <= 416:
                self.controller_iec_enabled.set(not self.controller_iec_enabled.get())
            elif self.preview_page == "settings" and 24 <= x <= 620 and 426 <= y <= 482:
                self.controller_wp_enabled.set(not self.controller_wp_enabled.get())
                if not self.controller_wp_enabled.get():
                    self.writable.set(False)
            self.open_size_preview()
        # Bind directly to the drawing surface.  On some Windows/Tk builds a
        # Canvas does not reliably forward touch/mouse events to its Toplevel.
        canvas.bind("<Button-1>", clicked)
        preview.bind("<Escape>", lambda _event: self.destroy())
        preview.protocol("WM_DELETE_WINDOW", self.destroy)
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
                    # A modal must remain the topmost graphic.  Pause the
                    # decorative HUD animations until it is dismissed.
                    if self.write_protect_prompt is None:
                        if self.motor:
                            draw_spinning_disk(time.monotonic())
                        draw_head_motion(time.monotonic())
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
        """Drain the HUD CDC FIFO at the proven desktop-GUI cadence."""
        self.poll_usb_telemetry()
        self.poll_controller_feedback()
        self.after(20, self.poll_serial_loop)


if __name__ == "__main__":
    TouchSimulator().mainloop()
