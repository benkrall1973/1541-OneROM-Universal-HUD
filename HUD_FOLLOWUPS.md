# Monitor follow-ups

## Validate and improve `SYNC / REV`

The current Monitor reports raw physical SYNC edge counts once per second and
derives a live rate. Before presenting a whole-number `SYNC / REV` as a
physical disk fact, validate the capture path against a known standard 1541
disk.

- Confirm expected counts by standard zone: tracks 1–17 = 42, 18–24 = 38,
  25–30 = 36, and 31–35 = 34 SYNC marks per revolution.
- Determine whether the present GPIO polling path misses short SYNC pulses.
- If necessary, replace polling with a reliable edge/PIO/DMA capture path.
- Accumulate total SYNC edges and integrated revolutions over a 5–10 second
  stable window; do not round an individual one-second sample.
- Reset the measurement after a head step, motor stop, stale RPM, or track
  change.
- Publish a rounded integer only after it is stable. Until then, label the
  value `SYNC / REV EST` (or show an estimating state).
- Treat nonstandard/copy-protected disks as valid exceptions: their observed
  count may intentionally differ from normal DOS formatting.
