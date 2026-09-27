# HUD RPM display note

## Why the display uses sync-derived RPM

The firmware's raw `RPM` telemetry can occasionally disagree with the stable
physical sync rate while the drive is reading or writing. For example, a D3
track produced `SYNC / SEC = 210` while the firmware reported `286.42 RPM`.
D3 has 42 syncs per revolution, so the sync measurement implies:

```text
210 syncs/sec ÷ 42 syncs/rev × 60 sec/min = 300 RPM
```

The HUD therefore uses the density-specific sync geometry for its primary RPM
value whenever motor, density, and sync data are valid:

```text
display RPM = syncs/sec × 60 ÷ expected syncs/revolution
```

The density targets are D3=42, D2=38, D1=36, and D0=34 syncs per revolution.
The firmware RPM remains available internally for the `EST` diagnostic, which
helps identify timing-window or firmware measurement jitter without allowing a
single bad raw sample to make the main RPM display misleading.

During formatting or a seek, a one-second SYNC window can be partial or mixed
between tracks. The derived RPM must be in a physically plausible 240–360 RPM
range; otherwise the window is rejected and the HUD holds the last qualified
RPM until a valid measurement arrives or the motor stops.
