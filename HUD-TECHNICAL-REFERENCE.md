# 1541 OneROM Desktop Monitor V0.0.11 baseline

The desktop Monitor is a Python/Tkinter display for the passive Monitor OneROM
firmware. It never controls the 1541 drive through the Monitor OneROM; the
Control OneROM continues to own
ROM, IEC-address, and write-protect controls.

## Active source set

| File | Purpose |
|---|---|
| `1541_touchscreen_simulator.py` | Complete fixed 7-inch Monitor application: UI, CDC discovery/link, telemetry parser, binding storage, and optional raw capture mode. |
| `Run-1541-Touchscreen-Simulator.cmd` | Windows launcher. |

## Diagnostics semantics

Physical Header is the most recently decoded header, for example `T35 S09`.
Sector Coverage is a recent-observation count such as `SEEN 8/17`; normal DOS
work does not necessarily read every sector on a track, so incomplete coverage
does not indicate a bad sector.

Sector FIFO is a chronological, 31-entry history of checksum-decoded physical
sector headers for the current track. It deliberately retains repeated sectors
and clears on a seek, track change, or motor stop. The card therefore reports
real observations without combining data from different tracks.

Every HUD card has a touchscreen Help popup. The popup documents the telemetry
source, meaning, limitations, and—where relevant—the calculation. Examples
include SYNC-derived RPM, rolling header/capture rates, SYNC marks per
revolution, density geometry, capture-overrun accounting, and Control OneROM
save/verification replies.

The Health card displays the firmware's capture count and two integrity
counters. `ROV` is a raw PIO/DMA ring overrun and must remain zero. `QOV` is a
diagnostic-event queue overflow; it can increment briefly during USB startup
without losing raw capture. The Clear control is intentionally local to the
desktop app: it starts a new diagnostic measurement window without resetting
or interrupting passive firmware capture.

## Session and configuration safety

Monitor readings are session-scoped. A Monitor disconnect, release, or
reconnect clears parser state and all derived disk/header/RPM/capture evidence
before the next CDC session is displayed. Every complete CDC record is parsed;
only the visible USB log is bounded.

The shared `onerom_drive_bindings.json` stores the binding schema version,
Control and Monitor USB serials, appearance colors, HUD card priorities, and
Control-card visibility preferences. Priorities are saved as a number or
`null` (`P–` in the UI); only the default seven cards begin pinned.
Unknown future JSON fields are ignored when loading so a newer preference file
does not discard a usable role assignment.

Control commands allow one pending confirmation at a time and show a response
timeout after three seconds.

## Matching firmware baseline

Desktop V0.0.11 uses the 1541HUD firmware V1.0.6 telemetry baseline. The firmware sends all
physical header events for responsive Recent Sectors while sampling RPM and
disk-ID metadata to keep the USB diagnostic queue healthy. Its compact health
record is:

```text
STATUS T0.0.16 C=<capture_count> R=<ring_overrun> Q=<queue_overflow>
```

Run the Monitor on Windows with:

```powershell
python .\1541_touchscreen_simulator.py
```

For a short raw CDC capture while bench debugging, use the same file:

```powershell
python .\1541_touchscreen_simulator.py --capture COM3 ub4-raw-capture.txt
```
