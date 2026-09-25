#!/usr/bin/env python3
"""1541 OneROM 7-inch touchscreen layout simulator.

This is a personal Windows design prototype.  It deliberately has no serial,
firmware, or repository connection: its purpose is to refine the Pi display
layout before the hardware arrives.
"""
from __future__ import annotations

import time
import math
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


class TouchSimulator(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("1541 OneROM — 7-inch Touchscreen Simulator")
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.minsize(980, 600)
        self.configure(bg=BG)

        self.ub3 = tk.BooleanVar(value=True)
        self.ub4 = tk.BooleanVar(value=True)
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
        style.configure("Nav.TButton", font=("Segoe UI", 12, "bold"), padding=(20, 12))

        self.header = ttk.Frame(self, padding=(22, 12))
        self.header.pack(fill="x")
        ttk.Label(self.header, text="1541 OneROM", style="Title.TLabel").pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(self.header, textvariable=self.status_var, style="Sub.TLabel").pack(side="right", padx=(0, 12))
        ttk.Button(self.header, text="⚙ Settings", style="Nav.TButton", command=lambda: self.show_page("settings")).pack(side="right")

        self.body = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.body.pack(fill="both", expand=True)
        self.pages: dict[str, ttk.Frame] = {}
        self.build_hud()
        self.build_control()
        self.build_settings()
        self.build_idle()
        self.nav = ttk.Frame(self, padding=(22, 0, 22, 18))
        self.nav.pack(fill="x")
        self.hud_button = ttk.Button(self.nav, text="DriveHUD", style="Nav.TButton", command=lambda: self.show_page("hud"))
        self.control_button = ttk.Button(self.nav, text="OneROM Control", style="Nav.TButton", command=lambda: self.show_page("control"))
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
        ttk.Button(footer, text="Simulate motor", command=self.toggle_motor).pack(side="right")

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
        ttk.Button(rom, text="Save startup ROM", command=self.save_rom).pack(anchor="e", pady=(12, 0))
        iec = ttk.Frame(right, style="Panel.TFrame", padding=18); iec.pack(fill="x")
        ttk.Label(iec, text="BOOT IEC ADDRESS", style="Section.TLabel").pack(anchor="w")
        self.iec_var = tk.StringVar(value="Device 8")
        ttk.Label(iec, textvariable=self.iec_var, style="MetricSmall.TLabel").pack(anchor="w", pady=(8, 12))
        self.iec_choice = ttk.Combobox(iec, values=self.iec_choices, state="readonly", font=("Segoe UI", 12))
        self.iec_choice.set("Device 8"); self.iec_choice.pack(fill="x", pady=4)
        ttk.Button(iec, text="Save IEC address", command=self.save_iec).pack(anchor="e", pady=(12, 0))
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
        ttk.Button(startup, text="Preview idle screen", command=self.show_idle).grid(row=2, column=2, padx=8, pady=(12, 0))
        ttk.Label(startup, text="Monitor PPI (V226HQL is 102.4):", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Spinbox(startup, from_=70, to=240, increment=0.1, textvariable=self.preview_ppi, width=9, font=("Segoe UI", 11)).grid(row=3, column=1, sticky="w", padx=12, pady=(16, 0))
        ttk.Label(startup, text="Preview scale (%):", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(startup, from_=50, to=200, textvariable=self.preview_scale, width=9, font=("Segoe UI", 11)).grid(row=4, column=1, sticky="w", padx=12, pady=(8, 0))
        ttk.Button(startup, text="Open / apply 7-inch preview", command=self.open_size_preview).grid(row=4, column=2, padx=8, pady=(8, 0))
        ttk.Button(page, text="← Return", style="Nav.TButton", command=lambda: self.show_page(self.default_page())).pack(anchor="w", pady=16)

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
            self.control_status.set("Write-protect control unavailable: UB3 is disconnected.")
            self.open_size_preview()
            return
        self.write_protect_prompt = not self.writable.get()
        self.open_size_preview()

    def save_preview_rom(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: UB3 connection is unavailable.")
        else:
            self.rom_var.set(self.rom_choice.get())
            self.control_status.set(f"✓ ROM saved and read-back verified: {self.rom_var.get()}")
        self.open_size_preview()

    def save_preview_iec(self) -> None:
        if not self.ub3.get():
            self.control_status.set("Save failed: UB3 connection is unavailable.")
        else:
            self.iec_var.set(self.iec_choice.get())
            self.control_status.set(f"✓ IEC address saved and read-back verified: {self.iec_var.get()}")
        self.open_size_preview()

    def choose_from_menu(self, event, choices: tuple[str, ...], variable: tk.StringVar, message: str) -> None:
        menu = tk.Menu(self.preview, tearoff=False, font=("Segoe UI", 11))
        def select(value: str) -> None:
            variable.set(value)
            self.control_status.set(message.format(value=value))
            self.open_size_preview()
        for item in choices:
            menu.add_command(label=item, command=lambda choice=item: select(choice))
        menu.tk_popup(event.x_root, event.y_root)

    def change_preview_scale(self, delta: int) -> None:
        self.preview_scale.set(max(50, min(200, self.preview_scale.get() + delta)))
        self.open_size_preview()

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

    def show_idle(self) -> None:
        self.show_page("idle")

    def launch_physical_preview(self) -> None:
        self.withdraw()
        self.preview_page = "hud"
        self.open_size_preview()

    def open_size_preview(self, _restore_workspace: bool = False) -> None:
        """Run the simulator itself as a scaled, movable 7-inch display."""
        old = getattr(self, "preview", None)
        position: tuple[int, int] | None = None
        if old and old.winfo_exists():
            position = (old.winfo_x(), old.winfo_y())
            old.destroy()
        # A VM commonly reports a generic logical DPI rather than the physical
        # monitor DPI.  The Settings values therefore take precedence.  At
        # 102.4 PPI and 100%, this is calibrated for the V226HQL.
        ppi = float(self.preview_ppi.get()) * (float(self.preview_scale.get()) / 100.0)
        width, height = round((155 / 25.4) * ppi), round((88 / 25.4) * ppi)
        preview = tk.Toplevel(self)
        preview.title("1541 OneROM — 7-inch Touchscreen Simulator")
        geometry = f"{width}x{height}"
        if position:
            geometry += f"+{position[0]}+{position[1]}"
        preview.geometry(geometry)
        preview.resizable(False, False)
        self.preview = preview
        canvas = tk.Canvas(preview, width=width, height=height, bg=BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        sx, sy = width / 1280, height / 720
        def text(x, y, value, size=16, fill=TEXT, bold=False, anchor="w"):
            canvas.create_text(x*sx, y*sy, text=value, fill=fill, anchor=anchor,
                               font=("Segoe UI Semibold" if bold else "Segoe UI", max(4, round(size*sy)), "normal"))
        def box(x1, y1, x2, y2, label, value=""):
            canvas.create_rectangle(x1*sx, y1*sy, x2*sx, y2*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            # 26 virtual pixels equals roughly 13 physical pixels at the
            # calibrated 7-inch scale: enough breathing room for touch UI.
            text(x1+26, y1+30, label.upper(), 18, MUTED, True)
            if value:
                canvas.create_text((x1+26)*sx, (y1+78)*sy, text=value, fill=TEXT, anchor="w", font=("Cascadia Mono", max(7, round(28*sy)), "normal"))
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
            text(26, 66, "DriveHUD · UB4 passive monitor · Firmware V1.0.0", 16, MUTED)
            # Primary live telemetry in the first two rows; the remaining
            # available HUD tags and sector FIFO stay visible below them.
            box(24, 82, 420, 220, "Track", "18.0")
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
            box(24, 382, 420, 520, "Sync / Rev Est", "33.90" if self.motor else "--.--")
            box(440, 382, 1256, 520, "Write Protect", "WRITABLE" if self.writable.get() else "PROTECTED")
            hud_wp_action = "Disable override" if self.writable.get() else "Enable writable override"
            canvas.create_rectangle(850*sx, 446*sy, 1228*sx, 500*sy, fill=WARNING if self.writable.get() else ACCENT, outline="")
            text(1039, 473, hud_wp_action, 17, BG, True, "center")
            text(28, 548, "HOME: anchored at Track 1.0", 20, MUTED)
            canvas.create_rectangle(24*sx, 570*sy, 1256*sx, 616*sy, fill=PANEL, outline=PANEL_ALT, width=1)
            text(42, 594, "RECENT SECTORS", 15, MUTED, True)
            text(270, 594, "12   02   04   06   08   10   12   02   04   06" if self.motor else "— FIFO empty —", 18, TEXT, True)
        elif self.preview_page == "control":
            text(26, 66, "OneROM Control • UB3", 16, MUTED)
            box(24, 96, 760, 318, "Startup ROM", self.rom_choice.get())
            text(720, 275, "▼", 24, ACCENT, True, "e")
            text(50, 232, f"Saved: {self.rom_var.get()}", 15, MUTED)
            canvas.create_rectangle(440*sx, 252*sy, 718*sx, 302*sy, fill=ACCENT, outline="")
            text(579, 277, "Save ROM", 17, BG, True, "center")
            box(784, 96, 1256, 318, "Boot IEC Address", self.iec_choice.get())
            text(1220, 275, "▼", 24, ACCENT, True, "e")
            text(810, 232, f"Saved: {self.iec_var.get()}", 15, MUTED)
            canvas.create_rectangle(1010*sx, 252*sy, 1228*sx, 302*sy, fill=ACCENT, outline="")
            text(1119, 277, "Save IEC", 17, BG, True, "center")
            box(24, 344, 760, 566, "Write-Protect Override", "FORCES WRITABLE" if self.writable.get() else "NORMAL PROTECTION")
            action = "Disable override" if self.writable.get() else "Enable writable override"
            canvas.create_rectangle(330*sx, 486*sy, 720*sx, 540*sy, fill=WARNING if self.writable.get() else ACCENT, outline="")
            text(525, 513, action, 17, BG, True, "center")
            box(784, 344, 1256, 566, "Connection", "UB3 online" if self.ub3.get() else "USB LOST")
            text(1020, 530, "Tap to simulate connect / loss", 13, MUTED, False, "center")
            text(28, 594, self.control_status.get(), 16, ACCENT if self.ub3.get() else OFFLINE)
        else:
            text(26, 66, "Settings", 16, MUTED)
            box(24, 96, 1256, 214, "Simulated devices", "UB3 connected   •   UB4 connected")
            box(24, 236, 1256, 430, "Display scale", f"{self.preview_scale.get()}%")
            text(50, 344, "50%", 16, MUTED)
            text(1230, 344, "200%", 16, MUTED, False, "e")
            canvas.create_line(70*sx, 376*sy, 1210*sx, 376*sy, fill=MUTED, width=max(1, round(7*sy)))
            knob_x = 70 + (self.preview_scale.get() - 50) / 150 * 1140
            canvas.create_oval((knob_x-14)*sx, 362*sy, (knob_x+14)*sx, 390*sy, fill=ACCENT, outline="")
            text(50, 466, "Tap the scale bar to resize this simulated display.", 17, MUTED)
            text(50, 505, f"Monitor calibration: {self.preview_ppi.get():.1f} PPI", 17, MUTED)
            canvas.create_rectangle(840*sx, 394*sy, 1000*sx, 442*sy, fill=PANEL_ALT, outline="")
            canvas.create_rectangle(1020*sx, 394*sy, 1200*sx, 442*sy, fill=ACCENT, outline="")
            text(920, 418, "− 1%", 17, TEXT, True, "center")
            text(1110, 418, "+ 1%", 17, BG, True, "center")
        canvas.create_rectangle(24*sx, 632*sy, 1256*sx, 710*sy, fill=PANEL_ALT, outline="")
        # Always-visible, deliberately large navigation buttons.  These are
        # touch targets, not a desktop-tab imitation.
        if self.preview_page == "hud":
            canvas.create_rectangle(34*sx, 654*sy, 360*sx, 706*sy, fill=PANEL, outline="")
            text(197, 680, "ONE ROM", 22, TEXT, True, "center")
        elif self.preview_page == "control":
            canvas.create_rectangle(34*sx, 654*sy, 300*sx, 706*sy, fill=PANEL, outline="")
            text(167, 680, "HUD", 22, TEXT, True, "center")
        else:
            canvas.create_rectangle(34*sx, 662*sy, 216*sx, 706*sy, fill=PANEL, outline="")
            canvas.create_rectangle(228*sx, 662*sy, 510*sx, 706*sy, fill=PANEL, outline="")
            text(125, 684, "HUD", 20, TEXT, True, "center")
            text(369, 684, "ONE ROM", 20, TEXT, True, "center")
        if self.preview_page == "hud":
            text(545, 680, "Values update while disk is spinning.", 20, MUTED)
        text(1240, 680, "● Connected", 17, ACCENT, True, "e")
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
            canvas.create_rectangle(340*sx, 410*sy, 600*sx, 470*sy, fill=PANEL_ALT, outline="")
            canvas.create_rectangle(680*sx, 410*sy, 940*sx, 470*sy, fill=WARNING if enabling else ACCENT, outline="")
            text(470, 440, "Cancel", 21, TEXT, True, "center")
            text(810, 440, "Confirm", 21, BG, True, "center")
        def clicked(event):
            x, y = event.x / sx, event.y / sy
            if self.write_protect_prompt is not None:
                if 680 <= x <= 940 and 410 <= y <= 470:
                    self.writable.set(self.write_protect_prompt)
                    self.write_protect_prompt = None
                    self.update_protection()
                elif 340 <= x <= 600 and 410 <= y <= 470:
                    self.write_protect_prompt = None
                self.open_size_preview()
                return
            if y < 76 and x > 1120:
                self.preview_page = "settings"
            elif self.preview_page == "hud" and 440 <= x <= 660 and 82 <= y <= 220:
                self.motor = not self.motor
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 680 <= x <= 900 and 82 <= y <= 220:
                self.head_direction = "OUT" if self.head_direction == "IN" else "IN"
                self.open_size_preview()
                return
            elif self.preview_page == "hud" and 850 <= x <= 1228 and 446 <= y <= 500:
                self.confirm_write_protect_toggle()
                return
            elif self.preview_page == "hud" and 654 <= y <= 706 and 34 <= x <= 360:
                self.preview_page = "control"
            elif self.preview_page == "control" and 654 <= y <= 706 and 34 <= x <= 300:
                self.preview_page = "hud"
            elif self.preview_page == "settings" and y >= 658 and 34 <= x <= 216:
                self.preview_page = "hud"
            elif self.preview_page == "settings" and y >= 658 and 228 <= x <= 510:
                self.preview_page = "control"
            elif self.preview_page == "control" and 440 <= x <= 718 and 252 <= y <= 302:
                self.save_preview_rom()
                return
            elif self.preview_page == "control" and 1010 <= x <= 1228 and 252 <= y <= 302:
                self.save_preview_iec()
                return
            elif self.preview_page == "control" and 24 <= x <= 760 and 96 <= y <= 318:
                self.choose_from_menu(event, self.rom_choices, self.rom_choice, "Startup ROM selected: {value}. Save is simulated.")
                return
            elif self.preview_page == "control" and 784 <= x <= 1256 and 96 <= y <= 318:
                self.choose_from_menu(event, self.iec_choices, self.iec_choice, "Boot IEC address selected: {value}. Save is simulated.")
                return
            elif self.preview_page == "control" and 330 <= x <= 720 and 486 <= y <= 540:
                self.confirm_write_protect_toggle()
                return
            elif self.preview_page == "control" and 784 <= x <= 1256 and 344 <= y <= 566:
                self.ub3.set(not self.ub3.get())
                self.apply_device_state()
            elif self.preview_page == "settings" and 340 <= y <= 385:
                self.preview_scale.set(max(50, min(200, round(50 + ((x - 70) / 1140) * 150))))
                self.open_size_preview()
                return
            elif self.preview_page == "settings" and 840 <= x <= 1000 and 394 <= y <= 442:
                self.change_preview_scale(-1)
                return
            elif self.preview_page == "settings" and 1020 <= x <= 1200 and 394 <= y <= 442:
                self.change_preview_scale(1)
                return
            self.open_size_preview()
        preview.bind("<Button-1>", clicked)
        preview.bind("<Escape>", lambda _event: self.destroy())
        preview.protocol("WM_DELETE_WINDOW", self.destroy)
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
                    preview.after(180, animate_disk)
            preview.after(180, animate_disk)

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
