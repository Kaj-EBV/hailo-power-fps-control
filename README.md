# Hailo Power FPS Control

A small Python/GStreamer experiment for **power-aware Hailo inference**.

The script reads Hailo device power from `hailodevicestats` and dynamically adjusts `videorate.max-rate`, so fewer or more frames reach `hailonet`. It is useful for testing whether a Hailo-8 workload can stay near a target device-power budget, for example 2 W average.

> This controls inference FPS. It does not downclock the Hailo device and it does not measure total system power.

## Features

- V4L2 camera input
- Raw and MJPEG camera modes
- High-FPS MJPEG USB camera support, for example 640x480 @ 120 FPS
- Hailo inference through `hailonet`
- Hailo device power measurement through `hailodevicestats`
- Dynamic FPS throttling using `videorate.max-rate`
- Optional display of the throttled stream using `fpsdisplaysink`
- Stable model input caps before `hailonet`
- `force-writable=true` enabled on `hailonet` for tee/display pipelines

## Requirements

You need a Linux system with:

- Python 3
- GStreamer
- PyGObject / `gi`
- V4L2 camera support
- HailoRT
- Hailo GStreamer plugins, including `hailonet` and `hailodevicestats`
- A compiled Hailo `.hef` model file

Useful checks:

```bash
hailortcli fw-control identify
gst-inspect-1.0 hailonet
gst-inspect-1.0 hailodevicestats
gst-inspect-1.0 videorate
gst-inspect-1.0 fpsdisplaysink
```

Check your model input shape:

```bash
hailortcli hef-info -f yolov5s.hef
```

Check camera modes:

```bash
v4l2-ctl -d /dev/video4 --list-formats-ext
```

## Install

Example Ubuntu/Debian packages:

```bash
sudo apt update

sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-libav \
  v4l-utils
```

Create a virtual environment. Use `--system-site-packages` so Python can see system-installed GStreamer/PyGObject/Hailo bindings:

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
```

Test Python/GStreamer import:

```bash
python - <<'PY'
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
print("GStreamer import OK")
PY
```

## Quickstart

### 120 FPS MJPEG camera

For a camera mode like `MJPG 640x480 @ 120 FPS`:

```bash
python hailo_power_fps_control.py --hef yolov5s.hef --device /dev/video4 --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120 --model-width 640 --model-height 640 --model-format RGB --target-w 2.0 --initial-fps 120 --min-fps 1 --max-fps 120 --stats-interval 2 --print-pipeline
```

### Visual low-FPS sanity check

Force a low fixed FPS so the display visibly updates slowly:

```bash
python hailo_power_fps_control.py --hef yolov5s.hef --device /dev/video4 --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120 --model-width 640 --model-height 640 --initial-fps 10 --min-fps 10 --max-fps 10 --target-w 2.0
```

### Headless mode

```bash
python hailo_power_fps_control.py --hef yolov5s.hef --device /dev/video4 --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120 --initial-fps 120 --max-fps 120 --no-display
```

### Try a different display sink

If the default display has X/GL issues:

```bash
python hailo_power_fps_control.py --hef yolov5s.hef --device /dev/video4 --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120 --initial-fps 120 --max-fps 120 --display-sink ximagesink
```

or:

```bash
python hailo_power_fps_control.py --hef yolov5s.hef --device /dev/video4 --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120 --initial-fps 120 --max-fps 120 --display-sink glimagesink
```

## How it works

The pipeline is roughly:

```text
hailodevicestats

camera
  -> optional jpegdec
  -> queue
  -> videorate max-rate=N
  -> videoscale
  -> videoconvert
  -> fixed model input caps
  -> tee
       -> hailonet force-writable=true
       -> fpsdisplaysink
```

The control loop runs once every `--stats-interval` seconds:

```text
read Hailo power
smooth power with an exponential moving average
if power is above target + deadband: reduce FPS by 10%
if power is below target - deadband: increase FPS by 1
otherwise: keep FPS unchanged
```

Important: the script changes `videorate.max-rate`, not the caps. This avoids repeated caps renegotiation while the pipeline is running.

## Important options

| Option | Meaning |
|---|---|
| `--hef` | Path to compiled Hailo HEF model |
| `--device` | V4L2 camera device |
| `--camera-encoding raw|mjpeg` | Camera capture mode |
| `--camera-width`, `--camera-height`, `--camera-fps` | Requested camera mode |
| `--model-width`, `--model-height`, `--model-format` | Caps expected by the HEF input |
| `--target-w` | Hailo device power target |
| `--deadband-w` | Power tolerance band |
| `--initial-fps` | Starting FPS cap |
| `--min-fps`, `--max-fps` | Controller limits |
| `--stats-interval` | Shared stats/control interval in seconds |
| `--no-display` | Disable display |
| `--display-sink` | Display sink used by `fpsdisplaysink` |

## Troubleshooting

### Camera defaults to 30 FPS

If your camera supports 120 FPS only as MJPEG, you must request MJPEG:

```bash
--camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120
```

### `Input buffer is not writable`

The script enables:

```text
hailonet force-writable=true
```

This is needed because the pipeline uses a `tee`, so the display and inference branches may share buffers.

### `No valid power measurement yet`

This can happen if the Hailo power sensor is not available on your hardware, not ready yet, or blocked by another measurement process.

### Hailo overcurrent warning

You may see a HailoRT warning that continuous power measurement uses the overcurrent-protection DVM and disables overcurrent protection while measurement is running. Treat that as a hardware/runtime caveat of the power-measurement path.

### Display/XVideo errors

Try:

```bash
--display-sink ximagesink
```

or:

```bash
--display-sink glimagesink
```

or run headless:

```bash
--no-display
```

## Notes

- `hailodevicestats` measures Hailo device power, not total system power.
- CPU MJPEG decoding, USB camera power, display power, and host preprocessing are not included in that reading.
- The displayed video is intentionally after `videorate`, so it reflects the throttled stream.
- The script currently sends inference output to `fakesink`; add model-specific post-processing if you want detections, overlays, or application output.

## License

MIT. See `LICENSE`.
