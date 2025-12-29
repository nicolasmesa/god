"""
Device emulation package.

This package provides emulated devices for the VMM.

There are two types of devices:
1. MMIO devices (Device subclasses): Emulated in user-space, handle guest
   memory accesses. Examples: UART, virtio devices.
2. In-kernel devices: Emulated by KVM for performance. Examples: GIC, timer.

The GIC is special - it's an in-kernel device that we configure but don't
emulate ourselves. KVM handles the actual interrupt routing.
"""

from .device import Device, MMIOAccess, MMIOResult
from .registry import DeviceRegistry
from .uart import PL011UART
from .gic import GIC, GICError

__all__ = [
    # MMIO device infrastructure
    "Device",
    "MMIOAccess",
    "MMIOResult",
    "DeviceRegistry",
    # MMIO devices
    "PL011UART",
    # In-kernel devices
    "GIC",
    "GICError",
]
