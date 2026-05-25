"""
GB/T 27930 Charging Pile CAN Signal Simulator (模拟器).

Simulates a complete DC charging session by sending realistic CAN messages
following the GB/T 27930 protocol. Useful for testing the reader (上位机).

Usage:
  python simulator.py                          # use real USBCANFD-200U
  python simulator.py --test                   # test mode (no hardware)
  python simulator.py --device-type 99 --loop  # VirtualUSBCAN, repeat
  python simulator.py --interval 50            # 50ms between messages
"""

import sys
import time
import argparse
import struct

from gbt27930 import (
    build_can_id, make_wire_id,
    ADDR_CHARGER, ADDR_BMS, CAN_BAUDRATE,
    PF_CHM, PF_BHM, PF_CRM, PF_BRM, PF_BCP, PF_BCL, PF_BCS, PF_CCS,
    PF_BSM, PF_BST, PF_CST, PF_CTS,
    format_message, parse_message,
)
from can_interface import CanInterface


def build_msg(pf: int, ps: int, sa: int, data: bytes) -> tuple:
    """Build a wire-level CAN ID and data for a GB/T 27930 message."""
    raw_id = build_can_id(pf, ps, sa)
    wire_id = make_wire_id(raw_id)
    return wire_id, data


def make_chm() -> tuple:
    """Charger Handshake: protocol version 'V','1','2'."""
    return build_msg(PF_CHM, ADDR_BMS, ADDR_CHARGER, b'V12')


def make_crm() -> tuple:
    """Charger Recognition: charger #1."""
    data = struct.pack('<H', 1)       # charger number
    data += b'\x01'                    # has battery compartment
    data += b'\x00' * 5               # padding
    return build_msg(PF_CRM, ADDR_BMS, ADDR_CHARGER, data)


def make_bhm() -> tuple:
    """BMS Handshake: max voltage 450.0V."""
    max_voltage = int(450.0 / 0.1)  # 4500
    data = struct.pack('>H', max_voltage)
    data += b'\x00' * 6
    return build_msg(PF_BHM, ADDR_CHARGER, ADDR_BMS, data)


def make_brm() -> tuple:
    """BMS Recognition: version 1.0.0, LiFePO4 battery."""
    data = bytes([1, 0, 0])          # BMS version
    data += bytes([3])                # LiFePO4
    data += struct.pack('>H', 2000)  # rated capacity: 200.0Ah
    data += struct.pack('>H', 4800)  # rated voltage: 480.0V
    return build_msg(PF_BRM, ADDR_CHARGER, ADDR_BMS, data)


def make_bcp() -> tuple:
    """Battery Charging Parameters."""
    data = struct.pack('>H', 5000)   # max total voltage: 500.0V
    data += struct.pack('>H', 2000)  # max charge current: (2000*0.1-400)= -200A? Let me use raw value...
    data += struct.pack('>H', int((200 + 400) / 0.1))  # max chg current 200A
    data += struct.pack('>H', int((100 + 400) / 0.1))  # max discharge 100A
    data += struct.pack('>H', 600)   # nominal energy 60.0kWh
    data += struct.pack('>H', 1200)  # max charge power 120.0kW
    return build_msg(PF_BCP, ADDR_CHARGER, ADDR_BMS, data)


def make_cts() -> tuple:
    """Charger Time Sync."""
    data = bytes([0] * 7)  # simplified: all zeros
    return build_msg(PF_CTS, ADDR_BMS, ADDR_CHARGER, data)


class ChargingSession:
    """Generates a realistic GB/T 27930 charging session over time."""

    def __init__(self):
        self._start_time = time.time()
        self._phase = "handshake"
        self._seq = 0

    def elapsed_sec(self) -> float:
        return time.time() - self._start_time

    def next_messages(self) -> list:
        """Return list of (can_id, data) tuples to send this tick."""
        self._seq += 1
        elapsed = self.elapsed_sec()
        msgs = []

        # Phase transitions based on elapsed time
        if elapsed < 1.0:
            self._phase = "handshake"
        elif elapsed < 2.0:
            self._phase = "identification"
        elif elapsed < 3.0:
            self._phase = "parameters"
        elif elapsed < 25.0:
            self._phase = "charging"
        elif elapsed < 27.0:
            self._phase = "stopping"
        else:
            self._phase = "done"

        if self._phase == "handshake":
            msgs.append(make_chm())
            # BMS also responds
            msgs.append(make_bhm())

        elif self._phase == "identification":
            msgs.append(make_crm())
            msgs.append(make_brm())

        elif self._phase == "parameters":
            msgs.append(make_bcp())
            msgs.append(make_cts())

        elif self._phase == "charging":
            # Simulate changing values over time
            charge_time = elapsed - 3.0  # seconds into charging phase
            soc = min(95, 30 + int(charge_time / 20.0 * 100))  # ~0.33%/s ramp

            # BCL — Battery Charging Demand (BMS → Charger)
            demand_voltage = int((400.0 + soc * 0.5) / 0.1)  # increasing voltage
            demand_current = int((125.0 - soc * 0.8 + 400) / 0.1)  # decreasing current
            bcl_data = struct.pack('>H', demand_voltage)
            bcl_data += struct.pack('>H', demand_current)
            bcl_data += bytes([0x01])  # CC mode
            bcl_data += b'\x00' * 3
            msgs.append(build_msg(PF_BCL, ADDR_CHARGER, ADDR_BMS, bcl_data))

            # BCS — Battery Charging Status (BMS → Charger)
            measured_v = int((395.0 + soc * 0.55) / 0.1)
            measured_i = int((120.0 - soc * 0.7 + 400) / 0.1)
            bcs_data = struct.pack('>H', measured_v)      # bytes 0-1
            bcs_data += struct.pack('>H', measured_i)      # bytes 2-3
            bcs_data += b'\x00'                             # byte 4: status flags
            bcs_data += b'\x00'                             # byte 5: reserved
            bcs_data += bytes([soc])                        # byte 6: SOC (%)
            bcs_data += struct.pack('>H', max(0, int((95 - soc) * 0.4)))  # bytes 7-8: remaining min
            msgs.append(build_msg(PF_BCS, ADDR_CHARGER, ADDR_BMS, bcs_data))

            # CCS — Charger Charging Status (Charger → BMS)
            ccs_data = struct.pack('>H', measured_v)
            ccs_data += struct.pack('>H', measured_i)
            ccs_data += struct.pack('>H', int(charge_time // 60))  # elapsed min
            ccs_data += bytes([0x01])  # allow charge
            ccs_data += b'\x00'
            msgs.append(build_msg(PF_CCS, ADDR_BMS, ADDR_CHARGER, ccs_data))

            # BSM — Battery Status Message (BMS → Charger)
            num_cells = 10
            bsm_data = bytes([num_cells])
            for i in range(num_cells):
                cell_v = int((3.5 + soc * 0.006 + i * 0.005) / 0.001)
                bsm_data += struct.pack('>H', cell_v)
            # Temperature probes
            temp_count = 4
            bsm_data += bytes([temp_count])
            temps = [int(25 + soc * 0.15 + i * 2) + 50 for i in range(temp_count)]
            bsm_data += bytes(temps)
            msgs.append(build_msg(PF_BSM, ADDR_CHARGER, ADDR_BMS, bsm_data))

        elif self._phase == "stopping":
            # CST — Charger Stop Charging
            cst_data = bytes([1]) + b'\x00' * 7  # Normal stop
            msgs.append(build_msg(PF_CST, ADDR_BMS, ADDR_CHARGER, cst_data))
            # BST — BMS Stop Charging
            bst_data = bytes([1]) + b'\x00' * 7
            msgs.append(build_msg(PF_BST, ADDR_CHARGER, ADDR_BMS, bst_data))

        return msgs


def main():
    parser = argparse.ArgumentParser(
        description="GB/T 27930 Charging Pile CAN Signal Simulator"
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
    parser.add_argument("--interval", type=int, default=100,
                        help="Delay between message groups in ms (default: 100)")
    parser.add_argument("--loop", action="store_true",
                        help="Repeat charging session indefinitely")
    parser.add_argument("--count", type=int, default=0,
                        help="Number of message groups to send (0 = until session done)")
    args = parser.parse_args()

    mode = "test" if args.test else "hw"
    device_name = {41: "USBCANFD-200U", 99: "VirtualUSBCAN"}.get(
        args.device_type, f"DeviceType({args.device_type})"
    )

    print(f"=== GB/T 27930 Charging Pile CAN Simulator ===")
    print(f"Device: {device_name}  Index: {args.device_index}  "
          f"Channel: {args.channel}  Baud: {args.baudrate}")
    print(f"Mode: {mode.upper()}  Interval: {args.interval}ms")
    print(f"{'Looping' if args.loop else 'Single session'}")
    print()

    can = CanInterface(
        device_type=args.device_type,
        device_index=args.device_index,
        channel=args.channel,
        baudrate=args.baudrate,
        mode=mode,
    )

    sent_count = 0
    session_count = 0

    try:
        can.open()

        while True:
            session = ChargingSession()
            done = False

            while not done:
                msgs = session.next_messages()
                if not msgs:
                    done = True
                    break

                for can_id, data in msgs:
                    ts = time.strftime("%H:%M:%S", time.localtime())
                    sent_count += 1

                    # Parse for display
                    parsed = parse_message(can_id, data)
                    line = format_message(parsed)
                    print(f"[SEND] [{ts}] #{sent_count} {line}")

                    ok = can.send(can_id, data)
                    if not ok:
                        print(f"  [ERROR] Send failed for {parsed['name']}")

                time.sleep(args.interval / 1000.0)

                if args.count > 0 and sent_count >= args.count:
                    done = True
                    break

            session_count += 1
            if not args.loop:
                break
            print(f"\n--- Session {session_count} complete, restarting... ---\n")

    except KeyboardInterrupt:
        print("\n\nSimulator stopped by user.")
    finally:
        can.close()
        print(f"\nTotal messages sent: {sent_count}")


if __name__ == "__main__":
    main()
