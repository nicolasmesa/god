"""
Virtio MMIO transport layer.

This implements the memory-mapped register interface for virtio devices.
Specific device types (console, block, etc.) inherit from VirtioMMIODevice.
"""

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING

from god.devices.device import Device
from god.devices.virtio.queue import Virtqueue

if TYPE_CHECKING:
    from god.memory import Memory

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Register offsets
# ─────────────────────────────────────────────────────────────────────────────

VIRTIO_MMIO_MAGIC_VALUE = 0x000
VIRTIO_MMIO_VERSION = 0x004
VIRTIO_MMIO_DEVICE_ID = 0x008
VIRTIO_MMIO_VENDOR_ID = 0x00C
VIRTIO_MMIO_DEVICE_FEATURES = 0x010
VIRTIO_MMIO_DEVICE_FEATURES_SEL = 0x014
VIRTIO_MMIO_DRIVER_FEATURES = 0x020
VIRTIO_MMIO_DRIVER_FEATURES_SEL = 0x024
VIRTIO_MMIO_QUEUE_SEL = 0x030
VIRTIO_MMIO_QUEUE_NUM_MAX = 0x034
VIRTIO_MMIO_QUEUE_NUM = 0x038
VIRTIO_MMIO_QUEUE_READY = 0x044
VIRTIO_MMIO_QUEUE_NOTIFY = 0x050
VIRTIO_MMIO_INTERRUPT_STATUS = 0x060
VIRTIO_MMIO_INTERRUPT_ACK = 0x064
VIRTIO_MMIO_STATUS = 0x070
VIRTIO_MMIO_QUEUE_DESC_LOW = 0x100
VIRTIO_MMIO_QUEUE_DESC_HIGH = 0x104
VIRTIO_MMIO_QUEUE_DRIVER_LOW = 0x110
VIRTIO_MMIO_QUEUE_DRIVER_HIGH = 0x114
VIRTIO_MMIO_QUEUE_DEVICE_LOW = 0x120
VIRTIO_MMIO_QUEUE_DEVICE_HIGH = 0x124
VIRTIO_MMIO_CONFIG_GENERATION = 0x0FC
VIRTIO_MMIO_CONFIG = 0x200  # Config space starts at 0x200

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Magic value - reads as "virt" in little-endian ASCII
VIRTIO_MAGIC = 0x74726976

# Virtio version (2 = modern virtio 1.0+)
VIRTIO_VERSION = 2

# Virtio device types
VIRTIO_DEV_NET = 1
VIRTIO_DEV_BLK = 2
VIRTIO_DEV_CONSOLE = 3
VIRTIO_DEV_RNG = 4
VIRTIO_DEV_BALLOON = 5
VIRTIO_DEV_SCSI = 8
VIRTIO_DEV_9P = 9
VIRTIO_DEV_VSOCK = 19

# Device status bits
VIRTIO_STATUS_ACKNOWLEDGE = 1
VIRTIO_STATUS_DRIVER = 2
VIRTIO_STATUS_DRIVER_OK = 4
VIRTIO_STATUS_FEATURES_OK = 8
VIRTIO_STATUS_DEVICE_NEEDS_RESET = 64
VIRTIO_STATUS_FAILED = 128

# Interrupt status bits
VIRTIO_INT_USED_RING = 1
VIRTIO_INT_CONFIG_CHANGE = 2


class VirtioMMIODevice(Device):
    """
    Base class for virtio MMIO devices.

    Handles the common MMIO register interface. Subclasses implement
    device-specific behavior (device_id, features, queue handling).

    Attributes:
        memory: Reference to guest memory for virtqueue access
    """

    def __init__(
        self,
        base_address: int,
        num_queues: int,
        memory: "Memory",
        irq_callback=None,
    ):
        """
        Initialize a virtio MMIO device.

        Args:
            base_address: Base address in guest physical memory
            num_queues: Number of virtqueues this device uses
            memory: Guest memory object for virtqueue access
            irq_callback: Optional callback to inject interrupts
        """
        self._base_address = base_address
        self._size = 0x200  # 512 bytes (registers + some config space)
        self._memory = memory
        self._irq_callback = irq_callback

        # Feature negotiation state
        self._device_features_sel = 0  # Which 32-bit feature word to read
        self._driver_features_sel = 0  # Which 32-bit feature word to write
        self._driver_features = 0      # Features accepted by driver (64-bit)

        # Device state
        self._status = 0
        self._interrupt_status = 0
        self._config_generation = 0

        # Queue selection and queues
        self._queue_sel = 0
        self._queues = [
            Virtqueue(
                index=i,
                memory_read=self._read_guest_memory,
                memory_write=self._write_guest_memory,
            )
            for i in range(num_queues)
        ]

    # ─────────────────────────────────────────────────────────────
    # Abstract methods - subclasses must implement
    # ─────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def device_id(self) -> int:
        """Virtio device type ID (e.g., VIRTIO_DEV_CONSOLE)."""
        pass

    @property
    @abstractmethod
    def device_features(self) -> int:
        """Features this device supports (64-bit bitmap)."""
        pass

    @abstractmethod
    def queue_notify(self, queue_index: int):
        """
        Called when guest notifies a queue has new buffers.

        Subclass should process the queue's requests.
        """
        pass

    def read_config(self, offset: int, size: int) -> int:
        """
        Read from device-specific config space.

        Override in subclass if device has config space.
        Default returns 0.
        """
        return 0

    def write_config(self, offset: int, size: int, value: int):
        """
        Write to device-specific config space.

        Override in subclass if device has writable config.
        Default does nothing.
        """
        pass

    # ─────────────────────────────────────────────────────────────
    # Device interface implementation
    # ─────────────────────────────────────────────────────────────

    @property
    def base_address(self) -> int:
        return self._base_address

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        return f"virtio-mmio (type {self.device_id})"

    # ─────────────────────────────────────────────────────────────
    # Memory access helpers
    # ─────────────────────────────────────────────────────────────

    def _read_guest_memory(self, addr: int, size: int) -> bytes:
        """Read bytes from guest physical memory."""
        return self._memory.read(addr, size)

    def _write_guest_memory(self, addr: int, data: bytes):
        """Write bytes to guest physical memory."""
        self._memory.write(addr, data)

    # ─────────────────────────────────────────────────────────────
    # Queue management
    # ─────────────────────────────────────────────────────────────

    @property
    def _selected_queue(self) -> Virtqueue | None:
        """Get the currently selected queue, or None if invalid."""
        if 0 <= self._queue_sel < len(self._queues):
            return self._queues[self._queue_sel]
        logger.warning(f"Invalid queue selection: {self._queue_sel}")
        return None

    # ─────────────────────────────────────────────────────────────
    # Interrupt handling
    # ─────────────────────────────────────────────────────────────

    def raise_interrupt(self, reason: int = VIRTIO_INT_USED_RING):
        """
        Raise an interrupt to the guest.

        Args:
            reason: VIRTIO_INT_USED_RING or VIRTIO_INT_CONFIG_CHANGE
        """
        self._interrupt_status |= reason
        if self._irq_callback:
            self._irq_callback()

    # ─────────────────────────────────────────────────────────────
    # MMIO read handler
    # ─────────────────────────────────────────────────────────────

    def read(self, offset: int, size: int) -> int:
        """Handle MMIO read from guest."""

        if offset == VIRTIO_MMIO_MAGIC_VALUE:
            return VIRTIO_MAGIC

        elif offset == VIRTIO_MMIO_VERSION:
            return VIRTIO_VERSION

        elif offset == VIRTIO_MMIO_DEVICE_ID:
            return self.device_id

        elif offset == VIRTIO_MMIO_VENDOR_ID:
            # "QEMU" in little-endian - common convention
            return 0x554D4551

        elif offset == VIRTIO_MMIO_DEVICE_FEATURES:
            # Return 32 bits of features based on selector
            features = self.device_features
            if self._device_features_sel == 0:
                return features & 0xFFFFFFFF
            elif self._device_features_sel == 1:
                return (features >> 32) & 0xFFFFFFFF
            return 0

        elif offset == VIRTIO_MMIO_QUEUE_NUM_MAX:
            return 256  # Max queue size

        elif offset == VIRTIO_MMIO_QUEUE_READY:
            queue = self._selected_queue
            return 1 if queue and queue.ready else 0

        elif offset == VIRTIO_MMIO_INTERRUPT_STATUS:
            return self._interrupt_status

        elif offset == VIRTIO_MMIO_STATUS:
            return self._status

        elif offset == VIRTIO_MMIO_CONFIG_GENERATION:
            return self._config_generation

        elif offset >= VIRTIO_MMIO_CONFIG:
            # Device-specific config space
            config_offset = offset - VIRTIO_MMIO_CONFIG
            return self.read_config(config_offset, size)

        else:
            logger.debug(f"Unhandled virtio read at offset 0x{offset:03x}")
            return 0

    # ─────────────────────────────────────────────────────────────
    # MMIO write handler
    # ─────────────────────────────────────────────────────────────

    def write(self, offset: int, size: int, value: int):
        """Handle MMIO write from guest."""

        if offset == VIRTIO_MMIO_DEVICE_FEATURES_SEL:
            self._device_features_sel = value

        elif offset == VIRTIO_MMIO_DRIVER_FEATURES:
            # Store 32 bits of driver features based on selector
            if self._driver_features_sel == 0:
                self._driver_features = (self._driver_features & 0xFFFFFFFF00000000) | value
            elif self._driver_features_sel == 1:
                self._driver_features = (self._driver_features & 0x00000000FFFFFFFF) | (value << 32)

        elif offset == VIRTIO_MMIO_DRIVER_FEATURES_SEL:
            self._driver_features_sel = value

        elif offset == VIRTIO_MMIO_QUEUE_SEL:
            self._queue_sel = value

        elif offset == VIRTIO_MMIO_QUEUE_NUM:
            queue = self._selected_queue
            if queue:
                queue.num = value

        elif offset == VIRTIO_MMIO_QUEUE_READY:
            queue = self._selected_queue
            if queue:
                queue.ready = bool(value)
                if value:
                    logger.info(
                        f"Queue {self._queue_sel} ready: "
                        f"desc=0x{queue.desc_addr:x}, "
                        f"avail=0x{queue.avail_addr:x}, "
                        f"used=0x{queue.used_addr:x}, "
                        f"num={queue.num}"
                    )

        elif offset == VIRTIO_MMIO_QUEUE_NOTIFY:
            # Guest is notifying us about a specific queue
            queue_index = value
            if 0 <= queue_index < len(self._queues):
                self.queue_notify(queue_index)
            else:
                logger.warning(f"Invalid queue notify: {queue_index}")

        elif offset == VIRTIO_MMIO_INTERRUPT_ACK:
            # Guest acknowledges interrupt bits
            self._interrupt_status &= ~value

        elif offset == VIRTIO_MMIO_STATUS:
            self._status = value
            if value == 0:
                # Reset device
                logger.info("Virtio device reset")
                self.reset()

        elif offset == VIRTIO_MMIO_QUEUE_DESC_LOW:
            queue = self._selected_queue
            if queue:
                queue.desc_addr = (queue.desc_addr & 0xFFFFFFFF00000000) | value

        elif offset == VIRTIO_MMIO_QUEUE_DESC_HIGH:
            queue = self._selected_queue
            if queue:
                queue.desc_addr = (queue.desc_addr & 0x00000000FFFFFFFF) | (value << 32)

        elif offset == VIRTIO_MMIO_QUEUE_DRIVER_LOW:
            queue = self._selected_queue
            if queue:
                queue.avail_addr = (queue.avail_addr & 0xFFFFFFFF00000000) | value

        elif offset == VIRTIO_MMIO_QUEUE_DRIVER_HIGH:
            queue = self._selected_queue
            if queue:
                queue.avail_addr = (queue.avail_addr & 0x00000000FFFFFFFF) | (value << 32)

        elif offset == VIRTIO_MMIO_QUEUE_DEVICE_LOW:
            queue = self._selected_queue
            if queue:
                queue.used_addr = (queue.used_addr & 0xFFFFFFFF00000000) | value

        elif offset == VIRTIO_MMIO_QUEUE_DEVICE_HIGH:
            queue = self._selected_queue
            if queue:
                queue.used_addr = (queue.used_addr & 0x00000000FFFFFFFF) | (value << 32)

        elif offset >= VIRTIO_MMIO_CONFIG:
            # Device-specific config space
            config_offset = offset - VIRTIO_MMIO_CONFIG
            self.write_config(config_offset, size, value)

        else:
            logger.debug(f"Unhandled virtio write at offset 0x{offset:03x}: 0x{value:x}")

    def reset(self):
        """Reset device to initial state."""
        self._status = 0
        self._interrupt_status = 0
        self._device_features_sel = 0
        self._driver_features_sel = 0
        self._driver_features = 0
        self._queue_sel = 0
        for queue in self._queues:
            queue.reset()
