"""
CAN interface wrapper using ctypes to call zlgcan.dll directly.

Supports two modes:
  - Hardware mode: calls zlgcan.dll from the SDK via ctypes
  - Test mode: in-memory queue for testing without hardware
"""

import os
import time
import queue
import ctypes
from ctypes import (
    c_uint, c_ubyte, c_ushort, c_ulonglong, c_void_p, c_char,
    POINTER, byref, Structure, sizeof,
)
from dataclasses import dataclass


# --- SDK path ---
_SDK_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "zlgcan(20260414)", "zlgcan_x64"
)
_DLL_PATH = os.path.join(_SDK_DIR, "zlgcan.dll")
_KERNEL_DIR = os.path.join(_SDK_DIR, "kerneldlls")


# --- ctypes structures matching zlgcan.h ---

class ZCAN_CHANNEL_INIT_CONFIG(Structure):
    """CAN channel init config (CAN 2.0 part)."""
    _fields_ = [
        ("can_type", c_uint),       # 0 = TYPE_CAN
        ("acc_code", c_uint),
        ("acc_mask", c_uint),
        ("reserved", c_uint),
        ("filter", c_ubyte),
        ("timing0", c_ubyte),
        ("timing1", c_ubyte),
        ("mode", c_ubyte),
    ]


class CAN_FRAME(Structure):
    """can_frame from canframe.h"""
    _fields_ = [
        ("can_id", c_uint),
        ("can_dlc", c_ubyte),
        ("__pad", c_ubyte),
        ("__res0", c_ubyte),
        ("__res1", c_ubyte),
        ("data", c_ubyte * 8),
    ]


class ZCAN_Transmit_Data(Structure):
    """Tag for transmitting standard CAN frames."""
    _fields_ = [
        ("frame", CAN_FRAME),
        ("transmit_type", c_uint),
    ]


class ZCAN_Receive_Data(Structure):
    """Tag for receiving standard CAN frames."""
    _fields_ = [
        ("frame", CAN_FRAME),
        ("timestamp", c_ulonglong),  # microseconds
    ]


# --- Dataclass for our internal use ---

@dataclass
class CanFrame:
    """A received CAN frame with timestamp."""
    can_id: int          # wire-level CAN ID (with EFF/RTR/ERR flags)
    data: bytes          # payload data (0-8 bytes)
    timestamp_us: int    # microsecond timestamp
    channel: int = 0


class CanInterface:
    """Abstract CAN bus interface.

    Hardware mode calls zlgcan.dll via ctypes.
    Test mode uses in-memory queues.

    Usage:
        with CanInterface(device_type=41, device_index=0, channel=0) as can:
            can.send(can_id=0x9826F456, data=b'V12')
            frame = can.receive(timeout_ms=1000)
    """

    # Device type constants from zlgcan.h
    ZCAN_USBCANFD_200U = 41
    ZCAN_VIRTUAL_DEVICE = 99
    TYPE_CAN = 0

    CAN_EFF_FLAG = 0x80000000

    # --- DLL function signatures ---
    _dll = None

    @classmethod
    def _load_dll(cls):
        """Load zlgcan.dll and set up function signatures (lazy, once)."""
        if cls._dll is not None:
            return

        # Add kernel DLL directory to PATH so zlgcan.dll can find them
        os.environ["PATH"] = _KERNEL_DIR + os.pathsep + os.environ.get("PATH", "")
        # Set working DLL search path
        try:
            ctypes.windll.kernel32.SetDllDirectoryW(_KERNEL_DIR)
        except Exception:
            pass

        dll = ctypes.WinDLL(_DLL_PATH)

        # ZCAN_OpenDevice
        dll.ZCAN_OpenDevice.argtypes = [c_uint, c_uint, c_uint]
        dll.ZCAN_OpenDevice.restype = c_void_p

        # ZCAN_CloseDevice
        dll.ZCAN_CloseDevice.argtypes = [c_void_p]
        dll.ZCAN_CloseDevice.restype = c_uint

        # ZCAN_InitCAN
        dll.ZCAN_InitCAN.argtypes = [c_void_p, c_uint, POINTER(ZCAN_CHANNEL_INIT_CONFIG)]
        dll.ZCAN_InitCAN.restype = c_void_p

        # ZCAN_StartCAN
        dll.ZCAN_StartCAN.argtypes = [c_void_p]
        dll.ZCAN_StartCAN.restype = c_uint

        # ZCAN_ResetCAN
        dll.ZCAN_ResetCAN.argtypes = [c_void_p]
        dll.ZCAN_ResetCAN.restype = c_uint

        # ZCAN_ClearBuffer
        dll.ZCAN_ClearBuffer.argtypes = [c_void_p]
        dll.ZCAN_ClearBuffer.restype = c_uint

        # ZCAN_GetReceiveNum
        dll.ZCAN_GetReceiveNum.argtypes = [c_void_p, c_ubyte]
        dll.ZCAN_GetReceiveNum.restype = c_uint

        # ZCAN_Transmit
        dll.ZCAN_Transmit.argtypes = [c_void_p, POINTER(ZCAN_Transmit_Data), c_uint]
        dll.ZCAN_Transmit.restype = c_uint

        # ZCAN_Receive
        dll.ZCAN_Receive.argtypes = [c_void_p, POINTER(ZCAN_Receive_Data), c_uint, c_uint]
        dll.ZCAN_Receive.restype = c_uint

        cls._dll = dll

    def __init__(self, device_type: int = 41, device_index: int = 0,
                 channel: int = 0, baudrate: int = 250000,
                 mode: str = "hw"):
        self.device_type = device_type
        self.device_index = device_index
        self.channel = channel
        self.baudrate = baudrate
        self.mode = mode

        self._device_handle = None
        self._channel_handle = None
        self._rx_queue: queue.Queue | None = None
        self._send_queue: queue.Queue | None = None
        self._opened = False

    @classmethod
    def create_pair(cls) -> tuple:
        """Create two CanInterface instances connected via in-memory queues."""
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

        self._load_dll()
        dll = self._dll

        # Open device
        self._device_handle = dll.ZCAN_OpenDevice(
            self.device_type, self.device_index, 0
        )
        if not self._device_handle:
            raise RuntimeError(
                f"Failed to open ZLG device type={self.device_type} "
                f"index={self.device_index}. Is the device connected?"
            )

        # Init CAN channel — accept all frames, normal mode
        cfg = ZCAN_CHANNEL_INIT_CONFIG()
        cfg.can_type = self.TYPE_CAN
        cfg.acc_code = 0x00000000
        cfg.acc_mask = 0xFFFFFFFF
        cfg.filter = 0       # dual filter mode
        cfg.mode = 0         # normal
        # timing0/timing1 = 0 → use default baud from device

        self._channel_handle = dll.ZCAN_InitCAN(
            self._device_handle, self.channel, byref(cfg)
        )
        if not self._channel_handle:
            dll.ZCAN_CloseDevice(self._device_handle)
            self._device_handle = None
            raise RuntimeError(f"Failed to init CAN channel {self.channel}")

        # Start CAN
        if dll.ZCAN_StartCAN(self._channel_handle) != 1:
            dll.ZCAN_ResetCAN(self._channel_handle)
            dll.ZCAN_CloseDevice(self._device_handle)
            self._device_handle = None
            self._channel_handle = None
            raise RuntimeError(f"Failed to start CAN channel {self.channel}")

        self._opened = True

    def close(self):
        """Stop CAN and close device."""
        if not self._opened:
            return

        if self.mode == "test":
            self._opened = False
            return

        dll = self._dll
        if self._channel_handle:
            dll.ZCAN_ResetCAN(self._channel_handle)
            self._channel_handle = None
        if self._device_handle:
            dll.ZCAN_CloseDevice(self._device_handle)
            self._device_handle = None
        self._opened = False

    def send(self, can_id: int, data: bytes) -> bool:
        """Send a CAN frame. Returns True on success."""
        if not self._opened:
            return False

        if self.mode == "test":
            frame = CanFrame(
                can_id=can_id,
                data=data,
                timestamp_us=int(time.time() * 1_000_000),
                channel=self.channel,
            )
            if self._send_queue is not None:
                self._send_queue.put(frame)
            return True

        dll = self._dll
        tx = ZCAN_Transmit_Data()
        tx.frame.can_id = can_id
        tx.frame.can_dlc = min(len(data), 8)
        padded = (data[:8]).ljust(8, b'\x00')
        for i, b in enumerate(padded):
            tx.frame.data[i] = b
        tx.transmit_type = 0

        return dll.ZCAN_Transmit(self._channel_handle, byref(tx), 1) == 1

    def receive(self, timeout_ms: int = 100) -> CanFrame | None:
        """Receive a single CAN frame. Returns None on timeout."""
        if not self._opened:
            return None

        if self.mode == "test":
            try:
                return self._rx_queue.get(timeout=timeout_ms / 1000.0)
            except queue.Empty:
                return None

        dll = self._dll

        # Check how many frames are available
        rcv_num = dll.ZCAN_GetReceiveNum(self._channel_handle, 0)  # TYPE_CAN=0
        if rcv_num == 0:
            return None

        rx = ZCAN_Receive_Data()
        count = dll.ZCAN_Receive(self._channel_handle, byref(rx), 1, timeout_ms)
        if count == 0:
            return None

        dlc = rx.frame.can_dlc
        data_len = min(dlc, 8)
        data = bytes(rx.frame.data[:data_len])

        return CanFrame(
            can_id=rx.frame.can_id,
            data=data,
            timestamp_us=rx.timestamp,
            channel=self.channel,
        )

    def flush_rx(self):
        """Clear all pending received frames."""
        if self.mode == "test":
            if self._rx_queue:
                while not self._rx_queue.empty():
                    try:
                        self._rx_queue.get_nowait()
                    except queue.Empty:
                        break
            return
        if self._channel_handle and self._dll:
            self._dll.ZCAN_ClearBuffer(self._channel_handle)

    @property
    def is_open(self) -> bool:
        return self._opened

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
