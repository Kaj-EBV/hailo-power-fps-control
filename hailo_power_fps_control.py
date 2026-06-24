#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
hailo_power_fps_control.py

A small GStreamer/Python experiment for power-aware Hailo inference.

The application:
  * captures video from a V4L2 camera,
  * optionally requests a high-FPS MJPEG camera mode,
  * decodes/converts/resizes frames to match a Hailo HEF input,
  * sends frames to hailonet,
  * reads Hailo device power from hailodevicestats, and
  * adjusts videorate.max-rate to throttle inference/display FPS.

This controls the number of frames reaching hailonet. It does not downclock
the Hailo device and it does not measure total system power.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib


def gst_quote(value: str) -> str:
    """Quote a string for safe use inside a Gst.parse_launch() pipeline string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Power-aware FPS controller for Hailo + GStreamer."
    )

    # Model/device selection.
    parser.add_argument(
        "--hef",
        required=True,
        help="Path to compiled Hailo HEF model, e.g. yolov5s.hef.",
    )
    parser.add_argument(
        "--device",
        default="/dev/video0",
        help="V4L2 camera device. Default: /dev/video0.",
    )

    # Camera capture configuration.
    parser.add_argument(
        "--camera-encoding",
        choices=["raw", "mjpeg"],
        default="raw",
        help=(
            "Camera capture encoding. Use 'mjpeg' for high-FPS USB camera modes. "
            "Default: raw."
        ),
    )
    parser.add_argument(
        "--camera-width",
        type=int,
        default=640,
        help="Camera capture width. Default: 640.",
    )
    parser.add_argument(
        "--camera-height",
        type=int,
        default=480,
        help="Camera capture height. Default: 480.",
    )
    parser.add_argument(
        "--camera-fps",
        type=int,
        default=30,
        help="Camera/source framerate. Default: 30.",
    )

    # Hailo model input caps. These should match `hailortcli hef-info`.
    parser.add_argument(
        "--model-width",
        type=int,
        default=640,
        help="Width expected by the HEF input. Default: 640.",
    )
    parser.add_argument(
        "--model-height",
        type=int,
        default=640,
        help="Height expected by the HEF input. Default: 640.",
    )
    parser.add_argument(
        "--model-format",
        default="RGB",
        help="Pixel format expected by hailonet/HEF input. Default: RGB.",
    )

    # FPS controller limits.
    parser.add_argument(
        "--initial-fps",
        type=int,
        default=15,
        help="Initial inference/display FPS cap. Default: 15.",
    )
    parser.add_argument(
        "--min-fps",
        type=int,
        default=1,
        help="Minimum FPS cap. Default: 1.",
    )
    parser.add_argument(
        "--max-fps",
        type=int,
        default=30,
        help="Maximum FPS cap. Default: 30.",
    )

    # Power-control behavior.
    parser.add_argument(
        "--target-w",
        type=float,
        default=2.0,
        help="Target Hailo device power in watts. Default: 2.0.",
    )
    parser.add_argument(
        "--deadband-w",
        type=float,
        default=0.10,
        help="Power deadband in watts. Default: 0.10.",
    )
    parser.add_argument(
        "--stats-interval",
        type=int,
        default=2,
        help=(
            "Single interval in seconds for both hailodevicestats sampling "
            "and the Python control loop. Default: 2."
        ),
    )

    # Display/debug options.
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Disable display branch and use fakesink instead.",
    )
    parser.add_argument(
        "--plain-display",
        action="store_true",
        help="Use the selected display sink directly instead of fpsdisplaysink.",
    )
    parser.add_argument(
        "--display-sink",
        default="autovideosink",
        help=(
            "Display sink used by fpsdisplaysink/plain display. "
            "Try ximagesink or glimagesink if autovideosink has issues. "
            "Default: autovideosink."
        ),
    )
    parser.add_argument(
        "--print-pipeline",
        action="store_true",
        help="Print the generated GStreamer pipeline string before running.",
    )

    return parser.parse_args()


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """
    Clamp inconsistent settings before constructing the pipeline.

    We use videorate with drop-only=true, so it can drop frames but cannot
    synthesize more frames than the camera source provides.
    """
    if args.stats_interval < 1:
        print("[app] Clamping stats interval to 1 second.")
        args.stats_interval = 1

    if args.camera_fps < 1:
        raise ValueError("--camera-fps must be >= 1")

    if args.min_fps < 1:
        print(f"[app] Clamping min FPS from {args.min_fps} to 1.")
        args.min_fps = 1

    if args.max_fps > args.camera_fps:
        print(
            f"[app] Clamping max FPS from {args.max_fps} to camera FPS "
            f"{args.camera_fps}; videorate drop-only cannot output more "
            "frames than the source."
        )
        args.max_fps = args.camera_fps

    if args.min_fps > args.max_fps:
        print(
            f"[app] Clamping min FPS from {args.min_fps} to max FPS "
            f"{args.max_fps}."
        )
        args.min_fps = args.max_fps

    if args.initial_fps > args.max_fps:
        print(
            f"[app] Clamping initial FPS from {args.initial_fps} to max FPS "
            f"{args.max_fps}."
        )
        args.initial_fps = args.max_fps

    if args.initial_fps < args.min_fps:
        print(
            f"[app] Clamping initial FPS from {args.initial_fps} to min FPS "
            f"{args.min_fps}."
        )
        args.initial_fps = args.min_fps

    return args


class PowerFpsController:
    """Owns the GStreamer pipeline and the power-to-FPS feedback loop."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.fps = args.initial_fps
        self.ema_power: Optional[float] = None
        self.loop = GLib.MainLoop()
        self.pipeline: Optional[Gst.Pipeline] = None
        self._stopping = False

        Gst.init(None)

        hef_path = gst_quote(args.hef)
        device_path = gst_quote(args.device)

        camera_source = self.build_camera_source(args, device_path)
        display_branch = self.build_display_branch(args)

        # Pipeline shape:
        #
        #   hailodevicestats                    # no pads; exposes telemetry
        #
        #   camera_source
        #     -> leaky queue
        #     -> videorate name=vr              # dynamic FPS cap
        #     -> videoscale/videoconvert
        #     -> modelcaps                      # fixed HEF input caps
        #     -> tee
        #          -> hailonet force-writable   # inference branch
        #          -> fpsdisplaysink            # visual throttled-FPS branch
        #
        # We intentionally change vr.max-rate at runtime instead of changing
        # capsfilter caps. This avoids repeated caps renegotiation while PLAYING.
        pipeline_str = f"""
            hailodevicestats
                name=stats
                interval={args.stats_interval}
                silent=true

            {camera_source} !

            queue
                leaky=downstream
                max-size-buffers=1
                max-size-time=0
                max-size-bytes=0 !

            videorate
                name=vr
                drop-only=true
                max-rate={args.initial_fps} !

            videoscale !
            videoconvert !

            capsfilter
                name=modelcaps
                caps="video/x-raw,
                    format={args.model_format},
                    width={args.model_width},
                    height={args.model_height},
                    pixel-aspect-ratio=1/1" !

            tee name=t

            t. ! queue
                leaky=downstream
                max-size-buffers=1
                max-size-time=0
                max-size-bytes=0 !
            hailonet
                name=net
                hef-path={hef_path}
                force-writable=true !
            fakesink sync=false

            {display_branch}
        """

        # Keep the multi-line string readable in source while giving GStreamer
        # a normal gst-launch-style one-liner.
        pipeline_str = " ".join(pipeline_str.split())

        if args.print_pipeline:
            print("\nGenerated GStreamer pipeline:\n")
            print(pipeline_str)
            print()

        self.pipeline = Gst.parse_launch(pipeline_str)

        # Required elements by name.
        self.stats = self.pipeline.get_by_name("stats")
        self.videorate = self.pipeline.get_by_name("vr")
        self.modelcaps = self.pipeline.get_by_name("modelcaps")

        if self.stats is None:
            raise RuntimeError("Could not find hailodevicestats element 'stats'.")

        if self.videorate is None:
            raise RuntimeError("Could not find videorate element 'vr'.")

        if self.modelcaps is None:
            raise RuntimeError("Could not find capsfilter element 'modelcaps'.")

        # Watch the GStreamer bus so errors/warnings are printed and the app
        # exits cleanly on fatal pipeline errors.
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

    def build_camera_source(self, args: argparse.Namespace, device_path: str) -> str:
        """
        Build the camera input section.

        raw mode:
            v4l2src -> video/x-raw caps -> videoconvert -> raw video

        mjpeg mode:
            v4l2src -> image/jpeg caps -> jpegdec -> videoconvert -> raw video

        Example high-FPS USB camera mode:
            --camera-encoding mjpeg --camera-width 640 --camera-height 480 --camera-fps 120
        """
        if args.camera_encoding == "mjpeg":
            return f"""
                v4l2src
                    device={device_path} !
                image/jpeg,
                    width={args.camera_width},
                    height={args.camera_height},
                    framerate={args.camera_fps}/1 !
                jpegdec !
                videoconvert !
                video/x-raw
            """

        return f"""
            v4l2src
                device={device_path} !
            video/x-raw,
                width={args.camera_width},
                height={args.camera_height},
                framerate={args.camera_fps}/1 !
            videoconvert !
            video/x-raw
        """

    def build_display_branch(self, args: argparse.Namespace) -> str:
        """
        Build the display branch.

        This branch is after videorate/modelcaps, so the on-screen video is
        the throttled stream, not the original full-rate camera stream.
        """
        display_sink = args.display_sink

        if args.no_display:
            return """
                t. ! queue !
                fakesink sync=false
            """

        if args.plain_display:
            return f"""
                t. ! queue !
                videoconvert !
                {display_sink} sync=false
            """

        return f"""
            t. ! queue !
            videoconvert !
            fpsdisplaysink
                name=fpssink
                video-sink={display_sink}
                sync=false
                text-overlay=true
        """

    def safe_get_property(self, element, property_name: str):
        """Read a GObject/GStreamer property without crashing the control loop."""
        try:
            return element.get_property(property_name)
        except TypeError as exc:
            print(f"[control] Property '{property_name}' not available: {exc}")
            return None
        except Exception as exc:
            print(f"[control] Failed to read '{property_name}': {exc}")
            return None

    def set_fps(self, new_fps: float) -> None:
        """
        Change the FPS cap by updating videorate.max-rate.

        This is intentionally less disruptive than dynamically changing caps.
        The stream caps remain stable; videorate simply drops more/fewer frames.
        """
        clamped = max(self.args.min_fps, min(self.args.max_fps, int(new_fps)))

        if clamped == self.fps:
            return

        self.fps = clamped
        self.videorate.set_property("max-rate", self.fps)

        print(f"[control] Set FPS cap to {self.fps}")

    def control_tick(self) -> bool:
        """
        Run one feedback-control iteration.

        Called once every --stats-interval seconds by the GLib main loop.
        Returns True to keep the timer active.
        """
        if self._stopping:
            return False

        power = self.safe_get_property(self.stats, "power-measurement")

        if power is None or power <= 0:
            print(
                "[control] No valid power measurement yet. "
                "The sensor may be unsupported, busy, or not ready."
            )
            return True

        # Exponential moving average:
        #   80% previous value + 20% latest sample.
        # This prevents single-sample spikes from causing immediate FPS swings.
        self.ema_power = (
            float(power)
            if self.ema_power is None
            else 0.8 * self.ema_power + 0.2 * float(power)
        )

        high = self.args.target_w + self.args.deadband_w
        low = self.args.target_w - self.args.deadband_w

        if self.ema_power > high:
            # Above target: reduce FPS relatively quickly.
            self.set_fps(self.fps * 0.90)
        elif self.ema_power < low:
            # Below target: increase FPS slowly to avoid overshoot.
            self.set_fps(self.fps + 1)

        print(
            f"[control] power={float(power):.2f} W, "
            f"ema={self.ema_power:.2f} W, "
            f"target={self.args.target_w:.2f} W, "
            f"fps={self.fps}"
        )

        return True

    def on_bus_message(self, bus, message) -> None:
        """Handle GStreamer bus messages."""
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[gstreamer] ERROR: {err}", file=sys.stderr)
            if debug:
                print(f"[gstreamer] DEBUG: {debug}", file=sys.stderr)
            self.stop()

        elif msg_type == Gst.MessageType.EOS:
            print("[gstreamer] End of stream")
            self.stop()

        elif msg_type == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"[gstreamer] WARNING: {warn}")
            if debug:
                print(f"[gstreamer] DEBUG: {debug}")

        elif msg_type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            old, new, _pending = message.parse_state_changed()
            print(
                f"[gstreamer] Pipeline state changed: "
                f"{old.value_nick} -> {new.value_nick}"
            )

    def run(self) -> None:
        """Start the pipeline and enter the GLib main loop."""
        GLib.timeout_add_seconds(self.args.stats_interval, self.control_tick)

        print("[app] Starting pipeline")
        print(
            f"[app] Camera input: {self.args.camera_encoding}, "
            f"{self.args.camera_width}x{self.args.camera_height} "
            f"@ {self.args.camera_fps} FPS"
        )
        print(
            f"[app] Model input: {self.args.model_width}x{self.args.model_height} "
            f"{self.args.model_format}"
        )
        print(
            f"[app] Power target: {self.args.target_w:.2f} W "
            f"+/- {self.args.deadband_w:.2f} W"
        )
        print(f"[app] Control/stats interval: {self.args.stats_interval} s")
        print(
            f"[app] FPS range: {self.args.min_fps}..{self.args.max_fps}, "
            f"initial={self.fps}"
        )

        if self.args.no_display:
            print("[app] Display: disabled")
        elif self.args.plain_display:
            print(f"[app] Display: {self.args.display_sink}")
        else:
            print(f"[app] Display: fpsdisplaysink -> {self.args.display_sink}")

        ret = self.pipeline.set_state(Gst.State.PLAYING)

        if ret == Gst.StateChangeReturn.FAILURE:
            print("[app] Failed to set pipeline to PLAYING", file=sys.stderr)
            self.stop()
            return

        try:
            self.loop.run()
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the pipeline and quit the GLib loop."""
        if self._stopping:
            return

        self._stopping = True

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)

        if self.loop.is_running():
            self.loop.quit()


def main() -> None:
    """Program entry point."""
    args = normalize_args(parse_args())
    controller = PowerFpsController(args)

    def handle_signal(signum, _frame) -> None:
        print(f"\n[app] Received signal {signum}, stopping")
        controller.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    controller.run()


if __name__ == "__main__":
    main()
