"""
Virtio device implementations.

This package provides paravirtualized devices using the virtio standard:
- VirtioConsole: Efficient console I/O
- VirtioBlock: Block device (disk) - TODO
- VirtioNet: Network device - TODO
"""

from god.devices.virtio.console import VirtioConsole
from god.devices.virtio.mmio import (
    VirtioMMIODevice,
    VIRTIO_DEV_NET,
    VIRTIO_DEV_BLK,
    VIRTIO_DEV_CONSOLE,
    VIRTIO_DEV_RNG,
)
from god.devices.virtio.queue import Virtqueue, VirtqDesc

__all__ = [
    "VirtioConsole",
    "VirtioMMIODevice",
    "Virtqueue",
    "VirtqDesc",
    "VIRTIO_DEV_NET",
    "VIRTIO_DEV_BLK",
    "VIRTIO_DEV_CONSOLE",
    "VIRTIO_DEV_RNG",
]
