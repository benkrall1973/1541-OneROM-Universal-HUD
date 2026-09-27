# 1541 OneROM Desktop HUD V0.0.5 baseline

The desktop HUD is a Python/Tkinter monitor for the passive UB4 1541HUD
firmware. It never controls the 1541 drive through UB4; UB3 continues to own
ROM, IEC-address, and write-protect controls.

## Active source set

| File | Purpose |
|---|---|
| `1541_touchscreen_simulator.py` | Fixed 7-inch HUD/Diagnostics UI, rendering, USB connection workflow, and diagnostic window controls. |
| `onerom_usb.py` | CDC board discovery, serial link, telemetry parser, and the compact health-status parser. |
| `capture_ub4_serial.py` | Optional raw CDC capture helper for bench diagnostics. |
| `Run-1541-Touchscreen-Simulator.cmd` | Windows launcher. |

## Diagnostics semantics

Physical Header is the most recently decoded header, for example `T35 S09`.
Sector Coverage is a recent-observation count such as `SEEN 8/17`; normal DOS
work does not necessarily read every sector on a track, so incomplete coverage
does not indicate a bad sector.

The Health card displays the firmware's capture count and two integrity
counters. `ROV` is a raw PIO/DMA ring overrun and must remain zero. `QOV` is a
diagnostic-event queue overflow; it can increment briefly during USB startup
without losing raw capture. The Clear control is intentionally local to the
desktop app: it starts a new diagnostic measurement window without resetting
or interrupting passive firmware capture.

## Matching firmware baseline

Desktop V0.0.5 is paired with 1541HUD firmware V1.0.6. The firmware sends all
physical header events for responsive Recent Sectors while sampling RPM and
disk-ID metadata to keep the USB diagnostic queue healthy. Its compact health
record is:

```text
STATUS T0.0.16 C=<capture_count> R=<ring_overrun> Q=<queue_overflow>
```

Run the HUD on Windows with:

```powershell
python .\1541_touchscreen_simulator.py
```
