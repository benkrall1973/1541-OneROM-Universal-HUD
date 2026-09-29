# 1541-OneROM-Universal-HUD — codex/usb-communication Fix Report

## Changes in this repo

- Fixed CDC receive framing so complete records are extracted before the unterminated-fragment safety limit is applied.
- Restricted OneROM discovery to the verified OneROM USB VID/PID (`0x1209:0xF542`).
- Kept Monitor telemetry/parser state session-scoped and reset it across physical disconnect/reconnect boundaries.
- Added explicit controller-session cleanup, transaction tracking, requested-value verification, startup refresh, atomic configuration writes, Diagnostics access, and regression coverage.
- Updated affected Universal-HUD documentation and handoff notes.

## Intentional non-changes

- No OneROM firmware source was changed in this repository.
- No PIO/DMA acquisition code was changed here.
- No changes were made to `1541-OneROM/main`.
