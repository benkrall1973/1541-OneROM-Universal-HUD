import pathlib
import sys
import time

import serial


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    output = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "ub4-raw-capture.txt")
    chunks: list[bytes] = []
    with serial.Serial(port, 115200, timeout=0.2) as device:
        device.dtr = False
        device.rts = False
        time.sleep(0.2)
        device.dtr = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            chunk = device.read(4096)
            if chunk:
                chunks.append(chunk)
    data = b"".join(chunks)
    output.write_bytes(data)
    print(data.decode("utf-8", errors="replace"))
    print(f"Captured {len(data)} bytes to {output}")


if __name__ == "__main__":
    main()
