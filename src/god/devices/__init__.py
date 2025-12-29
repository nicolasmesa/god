"""
Device emulation package.

This package provides emulated devices for the VMM. Devices handle
MMIO (Memory-Mapped I/O) accesses from the guest.
"""

from .device import Device, MMIOAccess, MMIOResult
from .registry import DeviceRegistry
from .uart import PL011UART

__all__ = [
    "Device",
    "MMIOAccess",
    "MMIOResult",
    "DeviceRegistry",
    "PL011UART",
]
