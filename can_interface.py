"""
CAN interface wrapper around ZLG zlgcan library.

Supports two modes:
  - Hardware mode: uses real ZLG CAN device (USBCANFD-200U, etc.)
  - Test mode: in-memory queue for testing without hardware
"""

import time
import queue
from dataclasses import dataclass


@dataclass
class CanFrame:
    """A received CAN frame with timestamp."""
    can_id: int      # wire-level CAN ID (with EFF/RTR/ERR flags)
    data: bytes      # payload data (0-8 bytes for CAN)
    timestamp_us: int  # microsecond timestamp
    channel: int = 0


class CanInterface:
    """Abstract CAN bus interface.

    Hardware mode connects to a real ZLG device.
    Test mode uses an in-memory queue (for testing without hardware).

    Usage:
        with CanInterface(device_type=41, device_index=0, channel=0) as can:
            can.send(can_id=0x9826F456, data=b'\x56\x31\x2E\x32')
            frame = can.receive(timeout_ms=1000)
    """

    # ZLG device type constants
    ZCAN_USBCANFD_200U = 41
    ZCAN_VIRTUAL_DEVICE = 99
    ZCAN_TYPE_CAN = 0
    ZCAN_TYPE_CANFD = 1

    def __init__(self, device_type: int = 41, device_index: int = 0,
                 channel: int = 0, baudrate: int = 250000,
                 mode: str = "hw"):
        self.device_type = device_type
        self.device_index = device_index
        self.channel = channel
        self.baudrate = baudrate
        self.mode = mode

        self._zcanlib = None
        self._device_handle = None
        self._channel_handle = None
        self._rx_queue: queue.Queue | None = None
        self._opened = False

    @classmethod
    def create_pair(cls) -> tuple:
        """Create two CanInterface instances connected via an in-memory queue.

        Returns (can_a, can_b) where frames sent on can_a appear on can_b's rx
        and vice versa.
        """
        q_ab = queue.Queue()
        q_ba = queue.Queue()
        can_a = CanInterface(mode="test")
        can_b = CanInterface(mode="test")
        can_a._rx_queue = q_ba
        can_b._rx_queue = q_ab
        can_a._send_queue = q_ab
        can_b._send_queue = q_ba
        return can_a, can_b

    def open(self):
        """Open device and start CAN channel."""
        if self._opened:
            return
        if self.mode == "test":
            if self._rx_queue is None:
                self._rx_queue = queue.Queue()
            self._opened = True
            return

        try:
            from zlgcan import ZCAN, ZCAN_CHANNEL_INIT_CONFIG
        except ImportError:
            raise ImportError(
                "zlgcan package is required for hardware mode. "
                "Install with: pip install zlgcan"
            )

        self._zcanlib = ZCAN()
        self._device_handle = self._zcanlib.OpenDevice(
            self.device_type, self.device_index, 0
        )
        if self._device_handle == 0 or self._device_handle is None:
            raise RuntimeError(
                f"Failed to open device type={self.device_type} "
                f"index={self.device_index}. Check device connection."
            )

        # Configure baud rate for USBCANFD devices
        try:
            self._zcanlib.SetValue(
                self._device_handle,
                f"{self.channel}/canfd_abit_baud_rate",
                str(self.baudrate).encode()
            )
        except Exception:
            pass  # SetValue may not be supported for all device types

        # Initialize CAN channel
        chn_cfg = ZCAN_CHANNEL_INIT_CONFIG()
        chn_cfg.can_type = self.ZCAN_TYPE_CAN
        # For classic CAN: set filter to accept all frames
        chn_cfg.can.filter = 0  # 0 = dual filter mode
        chn_cfg.can.acc_code = 0x00000000  # accept all
        chn_cfg.can.acc_mask = 0xFFFFFFFF  # mask all bits
        chn_cfg.can.mode = 0  # normal mode

        self._channel_handle = self._zcanlib.InitCAN(
            self._device_handle, self.channel, chn_cfg
        )
        if self._channel_handle == 0 or self._channel_handle is None:
            raise RuntimeError(f"Failed to init CAN channel {self.channel}")

        if self._zcanlib.StartCAN(self._channel_handle) != 1:
            raise RuntimeError(f"Failed to start CAN channel {self.channel}")

        self._opened = True

    def close(self):
        """Stop CAN and close device."""
        if not self._opened:
            return
        if self.mode == "test":
            self._opened = False
            return
        if self._channel_handle:
            self._zcanlib.ResetCAN(self._channel_handle)
            self._channel_handle = None
        if self._device_handle:
            self._zcanlib.CloseDevice(self._device_handle)
            self._device_handle = None
        self._opened = False

    def send(self, can_id: int, data: bytes) -> bool:
        """Send a CAN frame. Returns True on success."""
        if not self._opened:
            return False

        # Pad data to 8 bytes for standard CAN
        if len(data) < 8:
            data = data + b'\x00' * (8 - len(data))

        if self.mode == "test":
            frame = CanFrame(
                can_id=can_id,
                data=data,
                timestamp_us=int(time.time() * 1_000_000),
                channel=self.channel,
            )
            if hasattr(self, '_send_queue'):
                self._send_queue.put(frame)
            return True

        try:
            from zlgcan import ZCAN_Transmit_Data
            tx = ZCAN_Transmit_Data()
            tx.frame.can_id = can_id
            tx.frame.can_dlc = min(len(data), 8)
            tx.frame.data = (data[:8]).ljust(8, b'\x00')
            tx.transmit_type = 0  # normal send
            result = self._zcanlib.Transmit(
                self._channel_handle, tx, 1
            )
            return result == 1
        except Exception:
            return False

    def receive(self, timeout_ms: int = 100) -> CanFrame | None:
        """Receive a single CAN frame. Returns None on timeout."""
        if not self._opened:
            return None

        if self.mode == "test":
            try:
                return self._rx_queue.get(timeout=timeout_ms / 1000.0)
            except queue.Empty:
                return None

        try:
            rcv_num = self._zcanlib.GetReceiveNum(
                self._channel_handle, self.ZCAN_TYPE_CAN
            )
            if rcv_num <= 0:
                return None

            msgs, count = self._zcanlib.Receive(
                self._channel_handle, rcv_num, timeout_ms
            )
            if count <= 0:
                return None

            msg = msgs[0]
            dlc = msg.frame.can_dlc
            data_len = min(dlc, 8)
            return CanFrame(
                can_id=msg.frame.can_id,
                data=bytes(msg.frame.data[:data_len]),
                timestamp_us=msg.timestamp,
                channel=self.channel,
            )
        except Exception:
            return None

    def flush_rx(self):
        """Clear all pending received frames."""
        if self.mode == "test":
            while not self._rx_queue.empty():
                try:
                    self._rx_queue.get_nowait()
                except queue.Empty:
                    break
            return
        if self._channel_handle:
            self._zcanlib.ClearBuffer(self._channel_handle)

    @property
    def is_open(self) -> bool:
        return self._opened

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
