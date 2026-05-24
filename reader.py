"""
GB/T 27930 CAN Bus Upper Computer (上位机).

Reads CAN data from a charging pile via ZLG USBCANFD-200U, parses GB/T 27930
messages, and displays them in real-time.

Usage:
  python reader.py                           # use real USBCANFD-200U
  python reader.py --test                    # test mode (no hardware)
  python reader.py --self-test               # self-test: sim + reader in one process
  python reader.py --raw                     # also show raw (non-GB/T 27930) frames
  python reader.py --log session.log         # save output to file
"""

import sys
import time
import argparse
import threading
from collections import Counter

from gbt27930 import (
    CAN_EFF_FLAG, strip_eff, is_extended,
    parse_message, format_message, identify_can_id,
)
from can_interface import CanInterface, CanFrame


def run_reader(can: CanInterface, args):
    """Main reader loop."""
    frame_count = 0
    msg_counts = Counter()
    unknown_ids = set()
    start_time = time.time()

    while True:
        frame = can.receive(timeout_ms=50)
        if frame is None:
            continue

        frame_count += 1
        ts = time.strftime("%H:%M:%S", time.localtime(
            frame.timestamp_us / 1_000_000
        ))
        ts_us = frame.timestamp_us % 1_000_000

        # Parse and display
        if is_extended(frame.can_id):
            raw_id = strip_eff(frame.can_id)
            parsed = parse_message(frame.can_id, frame.data)
            name = parsed["name"]

            if identify_can_id(raw_id):
                # Known GB/T 27930 message
                msg_counts[name] += 1
                line = format_message(parsed)
                output = f"[{ts}.{ts_us:06d}] #{frame_count:5d} {line}"
                if args.log_file:
                    args.log_file.write(output + "\n")
                print(output)
            elif args.raw:
                # Unknown extended frame
                unknown_ids.add(raw_id)
                output = (
                    f"[{ts}.{ts_us:06d}] #{frame_count:5d} "
                    f"RAW EXT | ID=0x{raw_id:08X} "
                    f"| data=[{frame.data.hex(' ').upper()}]"
                )
                if args.log_file:
                    args.log_file.write(output + "\n")
                print(output)
        elif args.raw:
            # Standard frame (non GB/T 27930)
            output = (
                f"[{ts}.{ts_us:06d}] #{frame_count:5d} "
                f"RAW STD | ID=0x{frame.can_id:03X} "
                f"| data=[{frame.data.hex(' ').upper()}]"
            )
            if args.log_file:
                args.log_file.write(output + "\n")
            print(output)

    return frame_count, msg_counts, unknown_ids


def self_test():
    """Run simulator and reader in one process for testing."""
    can_sim, can_rdr = CanInterface.create_pair()

    # Start simulator in background thread
    stopped = threading.Event()

    def sim_thread():
        from simulator import ChargingSession

        can_sim.open()
        session = ChargingSession()
        while not stopped.is_set():
            msgs = session.next_messages()
            if not msgs:
                break
            for can_id, data in msgs:
                if stopped.is_set():
                    break
                can_sim.send(can_id, data)
            time.sleep(0.1)
        can_sim.close()

    print("=" * 70)
    print("  GB/T 27930 CAN Reader — SELF-TEST MODE")
    print("  Simulator + Reader running in one process")
    print("=" * 70)
    print()

    sim = threading.Thread(target=sim_thread, daemon=True)
    sim.start()

    can_rdr.open()
    frame_count = 0
    msg_counts = Counter()
    start_time = time.time()

    try:
        while sim.is_alive():
            frame = can_rdr.receive(timeout_ms=100)
            if frame is None:
                continue

            frame_count += 1
            ts = time.strftime("%H:%M:%S", time.localtime(
                frame.timestamp_us / 1_000_000
            ))
            ts_us = frame.timestamp_us % 1_000_000

            if is_extended(frame.can_id):
                raw_id = strip_eff(frame.can_id)
                parsed = parse_message(frame.can_id, frame.data)
                name = parsed["name"]
                if identify_can_id(raw_id):
                    msg_counts[name] += 1
                    print(f"[{ts}.{ts_us:06d}] #{frame_count:5d} {format_message(parsed)}")
                else:
                    print(f"[{ts}.{ts_us:06d}] #{frame_count:5d} "
                          f"UNKNOWN | ID=0x{raw_id:08X} | "
                          f"data=[{frame.data.hex(' ').upper()}]")
            else:
                print(f"[{ts}.{ts_us:06d}] #{frame_count:5d} "
                      f"RAW | ID=0x{frame.can_id:03X} | "
                      f"data=[{frame.data.hex(' ').upper()}]")

    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        sim.join(timeout=2)
        can_rdr.close()

    elapsed = time.time() - start_time
    _print_summary(frame_count, msg_counts, elapsed)


def _print_summary(frame_count, msg_counts, elapsed):
    """Print session summary."""
    print()
    print("=" * 70)
    print(f"  Session Summary")
    print(f"  Duration: {elapsed:.1f}s | Total frames: {frame_count}")
    print("=" * 70)
    if msg_counts:
        print("  Message counts:")
        for name, count in sorted(msg_counts.items()):
            bar = "#" * min(count, 50)
            print(f"    {name:6s} {count:4d} {bar}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="GB/T 27930 CAN Bus Upper Computer (上位机)"
    )
    parser.add_argument("--device-type", type=int, default=41,
                        help="ZLG device type (default: 41 = USBCANFD-200U)")
    parser.add_argument("--device-index", type=int, default=0,
                        help="Device index (default: 0)")
    parser.add_argument("--channel", type=int, default=0,
                        help="CAN channel (default: 0)")
    parser.add_argument("--baudrate", type=int, default=250000,
                        help="CAN baud rate (default: 250000)")
    parser.add_argument("--test", action="store_true",
                        help="Use test mode (in-memory queue, no hardware)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run simulator + reader self-test in one process")
    parser.add_argument("--raw", action="store_true",
                        help="Also display raw/unknown CAN frames")
    parser.add_argument("--log", type=str, default="",
                        help="Save output to a log file")
    args = parser.parse_args()

    # Self-test mode: no hardware needed
    if args.self_test:
        self_test()
        return

    mode = "test" if args.test else "hw"
    device_name = {41: "USBCANFD-200U", 99: "VirtualUSBCAN"}.get(
        args.device_type, f"DeviceType({args.device_type})"
    )

    print("=" * 70)
    print(f"  GB/T 27930 CAN Bus Upper Computer (上位机)")
    print(f"  Device: {device_name}  Index: {args.device_index}  "
          f"Channel: {args.channel}  Baud: {args.baudrate}")
    print(f"  Mode: {mode.upper()}")
    if args.raw:
        print(f"  Raw frames: ON")
    if args.log:
        print(f"  Log file: {args.log}")
    print("=" * 70)
    print()
    print("Listening on CAN bus... Press Ctrl+C to stop.")
    print()

    # Open log file if specified
    log_file = None
    if args.log:
        log_file = open(args.log, "w", encoding="utf-8")
    args.log_file = log_file

    can = CanInterface(
        device_type=args.device_type,
        device_index=args.device_index,
        channel=args.channel,
        baudrate=args.baudrate,
        mode=mode,
    )

    frame_count = 0
    msg_counts = Counter()
    start_time = time.time()

    try:
        can.open()
        frame_count, msg_counts, _ = run_reader(can, args)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
    finally:
        can.close()
        if log_file:
            log_file.close()
        elapsed = time.time() - start_time
        _print_summary(frame_count, msg_counts, elapsed)


if __name__ == "__main__":
    main()
