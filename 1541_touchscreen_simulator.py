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
import tkinter as tk
from tkinter import ttk


WIDTH, HEIGHT = 1280, 720
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
        self.title("1541 OneROM — 7-inch Touchscreen Simulator")
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.minsize(980, 600)
        self.configure(bg=BG)

        self.ub3 = tk.BooleanVar(value=True)
        self.ub4 = tk.BooleanVar(value=True)
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
        self.track = 18
        self.sector = 6
        self.motor = True
        self.head_direction = "IN"
        self.write_protect_prompt: bool | None = None
        self.color_picker: str | None = None
        self.popup_menu: dict | None = None
        self.appearance_target = "background"
        self.gradient_hue = 0.47
        self.hex_keyboard = False

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
        self.build_idle()
        self.nav = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.nav.pack(fill="x")
        self.hud_button = ttk.Button(self.nav, text="DRIVEHUD", style="Nav.TButton", command=lambda: self.show_page("hud"))
        self.control_button = ttk.Button(self.nav, text="ONEROM CONTROL", style="Nav.TButton", command=lambda: self.show_page("control"))
        self.hud_button.pack(side="left")
        self.control_button.pack(side="left", padx=12)
        ttk.Label(self.nav, text="Touchscreen layout simulator — no real hardware is accessed", style="Sub.TLabel").pack(side="right", pady=12)

        self.bind_all("<Button>", self.register_input, add=True)
        self.bind_all("<Key>", self.register_input, add=True)
        self.apply_device_state(initial=True)
        self.tick()
        # The default view is the calibrated physical reference, because that
        # is how the finished device will actually appear on the 7-inch panel.
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
        ttk.Label(page, text="Live 1541 mechanical telemetry — UB4 passive monitor", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        grid = ttk.Frame(page); grid.pack(fill="both", expand=True)
        grid.columnconfigure((0, 1, 2), weight=1); grid.rowconfigure((0, 1), weight=1)
        self.track_card, self.track_var = self.card(grid, "Track", "18.0")
        self.motor_card, self.motor_var = self.card(grid, "Motor", "ON")
        self.rpm_card, self.rpm_var = self.card(grid, "RPM", "300.7")
        self.head_card, self.head_var = self.card(grid, "Head", "IN")
        self.sector_card, self.sector_var = self.card(grid, "Sector", "06")
        self.wp_card, self.wp_var = self.card(grid, "Write Protect", "PROTECTED", small=True)
        for index, card in enumerate((self.track_card, self.motor_card, self.rpm_card, self.head_card, self.sector_card, self.wp_card)):
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=7, pady=7)
        footer = ttk.Frame(page, style="Panel.TFrame", padding=14); footer.pack(fill="x", pady=(12, 0))
        self.hud_connection = tk.StringVar()
        ttk.Label(footer, textvariable=self.hud_connection, style="Panel.TLabel").pack(side="left")
        ttk.Button(footer, text="SIMULATE MOTOR", command=self.toggle_motor).pack(side="right")

    def build_control(self) -> None:
        page = self.page("control")
        ttk.Label(page, text="OneROM Control", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Persistent ROM, IEC address, and write-protect configuration — UB3", style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
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
        devices = ttk.Frame(page, style="Panel.TFrame", padding=18); devices.pack(fill="x")
        ttk.Label(devices, text="SIMULATED USB DEVICES", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(devices, text="UB3 Active OneROM connected (COM4)", variable=self.ub3, command=self.apply_device_state).pack(anchor="w", pady=(12, 4))
        ttk.Checkbutton(devices, text="UB4 DriveHUD connected (COM3)", variable=self.ub4, command=self.apply_device_state).pack(anchor="w", pady=4)
        self.device_summary = tk.StringVar()
        ttk.Label(devices, textvariable=self.device_summary, style="Panel.TLabel", wraplength=1100).pack(anchor="w", pady=(14, 0))
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
        ttk.Spinbox(startup, from_=50, to=200, textvariable=self.preview_scale, width=9, font=("Segoe UI", 11)).grid(row=4, column=1, sticky="w", padx=12, pady=(8, 0))
        ttk.Button(startup, text="OPEN / APPLY 7-INCH PREVIEW", command=self.open_size_preview).grid(row=4, column=2, padx=8, pady=(8, 0))
        ttk.Button(page, text="← RETURN", style="Nav.TButton", command=lambda: self.show_page(self.default_page())).pack(anchor="w", pady=16)

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

    def apply_device_state(self, initial=False) -> None:
        # A maintained override must never survive loss of the control board.
        # This models the real application rule for a drive power-off / USB
        # disconnect: re-query before any later change is permitted.
        if not self.ub3.get():
            self.writable.set(False)
        self.hud_button.configure(state="normal" if self.ub4.get() else "disabled")
        self.control_button.configure(state="normal" if self.ub3.get() else "disabled")
        self.hud_connection.set("UB4 connected • Passive telemetry running" if self.ub4.get() else "UB4 not detected • DriveHUD unavailable")
        self.control_status.set("UB3 connected • Changes are simulated only" if self.ub3.get() else "UB3 not detected • Control actions unavailable")
        state = []
        state.append("UB3: Connected (COM4)" if self.ub3.get() else "UB3: Not connected")
        state.append("UB4: Connected (COM3)" if self.ub4.get() else "UB4: Not connected")
        self.status_var.set("   •   ".join(state))
        self.device_summary.set("   •   ".join(state) + "\nIn the finished version, serial-number bindings—not these COM numbers—identify each board.")
        if not initial and self.current_page not in ("settings", "idle"):
            self.show_page(self.default_page())
        elif initial:
            self.show_page(self.default_page())

    def apply_startup(self) -> None:
        self.show_page(self.default_page())

    def save_rom(self) -> None:
        self.rom_var.set(self.rom_choice.get())
        self.control_status.set("Startup ROM saved in simulated NV0. A real drive applies it on reset/power-cycle.")

    def save_iec(self) -> None:
        self.iec_var.set(self.iec_choice.get())
        self.control_status.set("Boot IEC address saved in simulated NV2.")

    def update_protection(self) -> None:
        self.control_status.set("Write-protect override ON — forces writable." if self.writable.get() else "Write-protect override OFF — normal protection.")

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
            self.rom_var.set(self.rom_choice.get())
            self.control_status.set(f"✓ ROM saved and read-back verified: {self.rom_var.get()}")
        self.open_size_preview()

    def save_preview_iec(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: controller connection is unavailable.")
        elif not self.controller_iec_enabled.get():
            self.control_status.set("Save failed: IEC address control is disabled in this build.")
        else:
            self.iec_var.set(self.iec_choice.get())
            self.control_status.set(f"✓ IEC address saved and read-back verified: {self.iec_var.get()}")
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
                ("Background Color", "background"),
                ("Group / Card Boxes", "group"),
                ("Button Color", "button"),
                ("Text 1 - Descriptions", "text1"),
                ("Text 2 - Values", "text2"),
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
        self.preview_scale.set(max(50, min(200, self.preview_scale.get() + delta)))
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
        if self.preview_page == "hud" and not self.ub4.get():
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
        self.withdraw()
        self.preview_page = "hud"
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
        ppi = float(self.preview_ppi.get()) * (float(self.preview_scale.get()) / 100.0)
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
        def text(x, y, value, size=16, fill=TEXT, bold=False, anchor="w"):
            canvas.create_text(x*sx, y*sy, text=value, fill=fill, anchor=anchor,
                               font=("Segoe UI Semibold" if bold else "Segoe UI", max(4, round(size*sy)), "normal"))
        def box(x1, y1, x2, y2, label, value="", value_size=28, value_y=None):
            canvas.create_rectangle(x1*sx, y1*sy, x2*sx, y2*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            # 26 virtual pixels equals roughly 13 physical pixels at the
            # calibrated 7-inch scale: enough breathing room for touch UI.
            text(x1+26, y1+30, label.upper(), 18, MUTED, True)
            if value:
                canvas.create_text((x1+26)*sx, (value_y if value_y is not None else y1+78)*sy, text=value, fill=TEXT, anchor="w", font=("Cascadia Mono", max(7, round(value_size*sy)), "normal"))
        def draw_spinning_disk(phase: float) -> None:
            """A tiny 5.25-inch floppy with deliberately subtle motion marks."""
            canvas.delete("disk")
            cx, cy, radius = 606, 154, 43
            canvas.create_oval((cx-radius)*sx, (cy-radius)*sy, (cx+radius)*sx, (cy+radius)*sy,
                               fill="#28343b", outline="#82959b", width=max(1, round(2*sy)), tags="disk")
            canvas.create_oval((cx-18)*sx, (cy-18)*sy, (cx+18)*sx, (cy+18)*sy,
                               fill="#101820", outline="#a9bbc4", width=max(1, round(sy)), tags="disk")
            canvas.create_oval((cx-5)*sx, (cy-5)*sy, (cx+5)*sx, (cy+5)*sy,
                               fill="#d5e5e9", outline="", tags="disk")
            # Three short highlights shift around the disk and blink, giving
            # a readable "spinning" cue without a distracting animation.
            active = ACCENT if int(phase * 5) % 2 == 0 else "#76ded0"
            for offset in (0, 2.1, 4.2):
                angle = phase * 5 + offset
                x1, y1 = cx + math.cos(angle) * 49, cy + math.sin(angle) * 49
                x2, y2 = cx + math.cos(angle) * 62, cy + math.sin(angle) * 62
                canvas.create_line(x1*sx, y1*sy, x2*sx, y2*sy, fill=active,
                                   width=max(1, round(3*sy)), tags="disk")
        def draw_head_motion(phase: float) -> None:
            """Sequential chevrons make the simulated head direction obvious."""
            canvas.delete("head")
            # 2.67 Hz is 50% slower than the original 4 Hz prototype rate.
            count = int(phase * (4 / 1.5)) % 3 + 1
            center_x = 855
            for index in range(count):
                # IN starts at the bottom (#1), then adds #2 and #3 upward.
                # OUT starts at the top (#1), then adds downward.
                # Both full three-chevron states are centered on y=147.
                # IN grows bottom-to-top; OUT grows top-to-bottom.
                y = 171 - index * 24 if self.head_direction == "IN" else 123 + index * 24
                if self.head_direction == "IN":
                    points = ((center_x - 20, y + 11), (center_x, y - 9), (center_x + 20, y + 11))
                else:
                    points = ((center_x - 20, y - 11), (center_x, y + 9), (center_x + 20, y - 11))
                canvas.create_line(*(coordinate * (sx if pos % 2 == 0 else sy) for pos, coordinate in enumerate(sum((list(point) for point in points), []))),
                                   fill=ACCENT, width=max(1, round(5*sy)), joinstyle="round", tags="head")
        text(26, 34, "1541 OneROM", 22, TEXT, True)
        text(1254, 34, "⚙", 25, ACCENT, True, "e")
        if self.preview_page == "hud":
            text(26, 66, "DriveHUD · passive monitor · Firmware V1.0.0", 16, MUTED)
            # Primary live telemetry in the first two rows; the remaining
            # available HUD tags and sector FIFO stay visible below them.
            box(24, 82, 420, 220, "Track", "18.0", value_size=56, value_y=171)
            box(440, 82, 660, 220, "Motor", "")
            text(462, 160, "ON" if self.motor else "OFF", 28, TEXT, True)
            if self.motor:
                draw_spinning_disk(time.monotonic())
            box(680, 82, 900, 220, "Head", "")
            text(702, 160, self.head_direction, 28, TEXT, True)
            draw_head_motion(time.monotonic())
            box(920, 82, 1256, 220, "Density", self.simulated_density())
            box(24, 232, 420, 370, "RPM", "300.7" if self.motor else "0.0")
            box(440, 232, 660, 370, "RPM State", "FRESH" if self.motor else "OFF")
            box(680, 232, 900, 370, "Sector", "06" if self.motor else "--")
            box(920, 232, 1256, 370, "Sync / Sec", "170" if self.motor else "0")
            text(946, 342, "raw SYNC / fresh RPM", 14, MUTED)
            if self.ub3.get() and self.controller_wp_enabled.get():
                box(24, 382, 420, 520, "Sync / Rev Est", "33.90" if self.motor else "--.--")
                box(440, 382, 1256, 520, "Write Protect", "WRITABLE" if self.writable.get() else "PROTECTED")
                hud_wp_action = "Disable override" if self.writable.get() else "Enable writable override"
                canvas.create_rectangle(850*sx, 447*sy, 1228*sx, 499*sy, fill=OFFLINE if self.writable.get() else ACCENT, outline="")
                text(1039, 473, hud_wp_action.upper(), 17, BG, True, "center")
            else:
                # HUD-only installations remain purely passive: no
                # write-protect state or control is presented.
                box(24, 382, 1256, 520, "Sync / Rev Est", "33.90" if self.motor else "--.--")
            text(28, 548, "HOME: anchored at Track 1.0", 20, MUTED)
            canvas.create_rectangle(24*sx, 570*sy, 1256*sx, 616*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            text(42, 594, "RECENT SECTORS", 15, MUTED, True)
            text(270, 594, "12   02   04   06   08   10   12   02   04   06" if self.motor else "— FIFO empty —", 18, TEXT, True)
        elif self.preview_page == "control":
            text(26, 66, "OneROM Controller", 16, MUTED)
            if self.controller_rom_enabled.get():
                box(24, 96, 760, 318, "Startup ROM", self.rom_choice.get())
                text(720, 275, "▼", 24, ACCENT, True, "e")
                text(50, 232, f"Saved: {self.rom_var.get()}", 15, MUTED)
                canvas.create_rectangle(454*sx, 251*sy, 732*sx, 303*sy, fill=ACCENT, outline="")
                text(593, 277, "SAVE ROM", 17, BG, True, "center")
                text(50, 292, "MENU", 14, MUTED, True)
            else:
                box(24, 96, 760, 318, "Startup ROM", "NOT ENABLED")
                text(50, 232, "Enable in Settings / controller JSON build.", 15, MUTED)
            if self.controller_iec_enabled.get():
                box(784, 96, 1256, 318, "Boot IEC Address", self.iec_choice.get())
                text(1220, 275, "▼", 24, ACCENT, True, "e")
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
            box(784, 344, 1256, 566, "Connection", "Controller online" if self.ub3.get() else "USB LOST")
            text(1020, 530, "Tap to simulate connect / loss", 18, MUTED, False, "center")
            text(28, 594, self.control_status.get(), 16, ACCENT if self.ub3.get() else OFFLINE)
        elif self.preview_page == "settings":
            text(26, 66, "Settings", 16, MUTED)
            canvas.create_rectangle(24*sx, 96*sy, 620*sx, 214*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            canvas.create_rectangle(660*sx, 96*sy, 1256*sx, 214*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            text(50, 126, "CONTROLLER-ROLE ONEROM", 18, MUTED, True)
            text(50, 172, "INSTALLED" if self.ub3.get() else "NOT INSTALLED", 26, ACCENT if self.ub3.get() else OFFLINE, True)
            text(686, 126, "HUD-ROLE ONEROM", 18, MUTED, True)
            text(686, 172, "INSTALLED" if self.ub4.get() else "NOT INSTALLED", 26, ACCENT if self.ub4.get() else OFFLINE, True)
            text(50, 202, "Tap to toggle simulated hardware", 13, MUTED)
            text(686, 202, "Tap to toggle simulated hardware", 13, MUTED)
            text(26, 246, "Either OneROM can be compiled as the Controller or the passive DriveHUD; the board JSON selects its role.", 14, MUTED)
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
            appearance_name = COLOR_PALETTES[self.appearance_target][0]
            box(660, 294, 1256, 482, "Appearance", appearance_name)
            text(686, 456, "MENU", 14, MUTED, True)
            canvas.create_rectangle(1000*sx, 415*sy, 1228*sx, 467*sy, fill=ACCENT, outline="")
            text(1114, 441, "CUSTOM COLOR", 17, BG, True, "center")
            text(24, 514, "Controller feature availability mirrors the configuration JSON used when the board is compiled.", 13, MUTED)
            # Kept available for the Windows simulator, but tucked below the
            # configuration grid so it will not dominate the fixed 7-inch UI.
            canvas.create_rectangle(24*sx, 540*sy, 1256*sx, 624*sy, fill=PANEL, outline=PANEL_ALT)
            text(44, 562, "DISPLAY SCALE", 14, MUTED, True)
            text(44, 590, f"{self.preview_scale.get()}%", 20, TEXT, True)
            # Keep the scale control in the open space between its label and
            # the adjustment buttons, rather than low against the footer.
            text(200, 574, "50%", 14, MUTED, False, "center")
            text(860, 574, "200%", 14, MUTED, False, "center")
            canvas.create_line(200*sx, 594*sy, 860*sx, 594*sy, fill=MUTED, width=max(1, round(5*sy)))
            knob_x = 200 + (self.preview_scale.get() - 50) / 150 * 660
            canvas.create_oval((knob_x-12)*sx, 582*sy, (knob_x+12)*sx, 606*sy, fill=ACCENT, outline="")
            canvas.create_rectangle(900*sx, 556*sy, 1060*sx, 608*sy, fill=PANEL_ALT, outline="")
            canvas.create_rectangle(1080*sx, 556*sy, 1240*sx, 608*sy, fill=ACCENT, outline="")
            text(980, 582, "− 1%", 17, TEXT, True, "center")
            text(1160, 582, "+ 1%", 17, BG, True, "center")
        else:
            text(26, 66, "OneROM Setup", 16, MUTED)
            box(24, 120, 1256, 410, "No OneROM role configured", "OPEN SETTINGS")
            text(50, 330, "Use the settings gear to simulate Controller and DriveHUD hardware.", 18, MUTED)
        canvas.create_rectangle(24*sx, 632*sy, 1256*sx, 710*sy, fill=PANEL_ALT, outline="")
        # Deliberately large touch targets.  Only offer a destination that is
        # actually installed: the compact UI should never expose a dead tab.
        if self.preview_page == "hud" and self.ub3.get():
            canvas.create_rectangle(34*sx, 645*sy, 334*sx, 697*sy, fill=PANEL, outline="")
            text(184, 671, "CONTROL", 17, TEXT, True, "center")
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
        if self.preview_page == "hud":
            text(680, 680, "Values update while disk is spinning.", 20, MUTED, False, "center")
        if self.preview_page == "hud":
            footer_status = "● DriveHUD connected"
        elif self.preview_page == "control":
            footer_status = "● Controller connected"
        elif self.preview_page == "settings":
            footer_status = "● Hardware setup"
        else:
            footer_status = "● No boards configured"
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
                        else:
                            self.popup_menu["variable"].set(choice)
                            self.control_status.set(self.popup_menu["message"].format(value=choice))
                        self.popup_menu = None
                        self.open_size_preview()
                        return
                return
            if self.color_picker is not None:
                if 1050 <= x <= 1248 and 30 <= y <= 82:
                    self.color_picker = None
                    self.hex_keyboard = False
                    self.open_size_preview()
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
            if y < 76 and x > 1120:
                self.preview_page = "settings"
            elif self.preview_page == "setup" and 24 <= x <= 1256 and 120 <= y <= 410:
                self.preview_page = "settings"
            elif self.preview_page == "settings" and 24 <= x <= 620 and 96 <= y <= 214:
                self.toggle_simulated_board("ub3")
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 660 <= x <= 1256 and 96 <= y <= 214:
                self.toggle_simulated_board("ub4")
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 1000 <= x <= 1228 and 415 <= y <= 467:
                self.color_picker = f"gradient:{self.appearance_target}"
                self.hex_keyboard = False
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 660 <= x <= 1256 and 294 <= y <= 482:
                self.choose_appearance_menu(event)
                return
            elif self.preview_page == "hud" and 440 <= x <= 660 and 82 <= y <= 220:
                self.motor = not self.motor
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 680 <= x <= 900 and 82 <= y <= 220:
                self.head_direction = "OUT" if self.head_direction == "IN" else "IN"
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and self.ub3.get() and self.controller_wp_enabled.get() and 850 <= x <= 1228 and 447 <= y <= 499:
                self.confirm_write_protect_toggle()
                return
            elif self.preview_page == "hud" and self.ub3.get() and 645 <= y <= 697 and 34 <= x <= 334:
                self.preview_page = "control"
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
                self.choose_from_menu(event, self.rom_choices, self.rom_choice, "Startup ROM selected: {value}. Save is simulated.")
                return
            elif self.preview_page == "control" and self.controller_iec_enabled.get() and 784 <= x <= 1256 and 96 <= y <= 318:
                self.choose_from_menu(event, self.iec_choices, self.iec_choice, "Boot IEC address selected: {value}. Save is simulated.")
                return
            elif self.preview_page == "control" and self.controller_wp_enabled.get() and 342 <= x <= 732 and 499 <= y <= 551:
                self.confirm_write_protect_toggle()
                return
            elif self.preview_page == "control" and 784 <= x <= 1256 and 344 <= y <= 566:
                self.toggle_simulated_board("ub3")
            elif self.preview_page == "settings" and 200 <= x <= 860 and 578 <= y <= 610:
                self.preview_scale.set(max(50, min(200, round(50 + ((x - 200) / 660) * 150))))
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
        preview.bind("<Button-1>", clicked)
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
                        else:
                            canvas.delete("disk")
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
        self.after(550, self.tick)


if __name__ == "__main__":
    TouchSimulator().mainloop()
