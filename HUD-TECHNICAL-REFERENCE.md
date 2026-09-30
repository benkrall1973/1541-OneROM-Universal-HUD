# 1541 OneROM Desktop Monitor V0.0.22

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

### Track position and `SYNC 18`

The Track / Position card follows observed UC2 stepper-phase transitions in
half-track increments. A long outward seek followed by an inward transition
establishes a HOME estimate; `$0022` target-track writes and checksum-valid
physical headers can subsequently correct that estimate.

`SYNC 18` is a **local display calibration** button. Use it only after a known
operation has placed the real head on directory Track 18—for example:

```basic
OPEN 15,8,15,"I":CLOSE 15
```

or after loading the directory. Tapping the button sets the displayed counter
to `18.0 · MANUAL SYNC 18`; later observed phase steps continue from that
position. It does not send IEC, Control OneROM, or Monitor OneROM commands and
does not move the head. A later HOME sequence, `$0022` write, or valid physical
header may re-anchor the display automatically.

The Track card turns red above Track 35.0 to call out movement beyond standard
35-track DOS media. The measurement remains visible because 36–42-track media
and diagnostic operations are real, not a reason for the HUD to hide data.

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
reconnect clears transient disk/header/RPM/capture evidence before the next CDC
session is displayed. An already established track estimate is retained as
`CARRIED EST.` across that reconnect and is corrected by later HOME, `$0022`, or
physical-header evidence. Every complete CDC record is parsed; only the visible
USB log is bounded.

The shared `onerom_drive_bindings.json` stores the binding schema version,
Control and Monitor USB serials, appearance colors, HUD card priorities, and
Control-card visibility preferences. Priorities are saved as a number or
`null` (`P–` in the UI); only the default seven cards begin pinned.

The same JSON file stores the last normal desktop window client size. The
Windows test window may be resized, maximized, or toggled fullscreen with
`F11`; its rendered HUD remains centered at the fixed 16:9 Pi aspect ratio,
using black letterbox margins instead of stretching touch geometry.

The application checks attached USB serials every two seconds. If a saved
Control or Monitor OneROM disappears—or its CDC link raises a Windows
disconnect error—its cards become offline immediately, the active Canvas is
redrawn, and normal serial-based reconnect polling begins. This local
enumeration does not send a command to either board.
Unknown future JSON fields are ignored when loading so a newer preference file
does not discard a usable role assignment.

Control commands allow one pending confirmation at a time and show a response
timeout after three seconds.

## Matching firmware baseline

Desktop V0.0.13 uses the 1541HUD firmware V1.0.6 telemetry baseline. The firmware sends all
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
