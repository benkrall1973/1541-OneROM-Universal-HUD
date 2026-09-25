# 1541 OneROM Universal GUI V1.0.0-GUI_converge01

This is the non-destructive first step toward one desktop/Raspberry Pi GUI for
the 1541 OneROM system. It combines the proven V1 control and HUD workflows
without changing UB3 or UB4 firmware.

## What it does now

- Dashboard, **OneROM Control**, **DriveHUD**, and **Options** tabs.
- Preserves the current UB3 ROM, boot IEC address, and write-protect workflow.
- Preserves the current UB4 telemetry grammar and display fields.
- Saves window geometry/maximize state and tab visibility locally.
- Scans CDC ports and binds UB3/UB4 by USB descriptor serial number—not COM
  number—into `%APPDATA%\\1541-OneROM\\universal-gui.json` on Windows.
- Reconnects saved boards automatically when enabled.

## Deliberate V1 limitation

No firmware command or protocol has been changed. The user binds a newly found
board in **Options** by selecting it and assigning UB3 or UB4. A later additive
firmware `INFO` command can make role confirmation fully automatic.

## Run

```powershell
py -m pip install -r requirements.txt
py src\\1541_onerom_universal_gui.py
```

The standalone V1 USB Selector and DriveHUD remain the recovery/reference
tools. This test GUI does not replace or modify them.

## Windows executable

Use `build_windows.ps1` to create a one-file Windows executable. The generated
`dist/` output is intentionally ignored by Git; release artifacts should be
published only after hardware verification.
