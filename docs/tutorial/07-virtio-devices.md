# Chapter 7: Virtio Devices - Efficient Paravirtualization

In this chapter, we'll implement virtio devices for efficient guest-host communication. Virtio is the standard for paravirtualized devices in Linux VMs.

## The Problem with Device Emulation

Emulating real hardware is slow. Consider a disk read:

**Emulating a real disk controller:**
1. Guest writes command to register → VM exit
2. Guest writes LBA to register → VM exit
3. Guest writes sector count → VM exit
4. Guest triggers command → VM exit
5. VMM performs actual I/O
6. VMM updates status register
7. Guest polls status → VM exit
8. Guest reads data → multiple VM exits

That's many VM exits for a single disk operation!

## What is Paravirtualization?

**Paravirtualization** means the guest knows it's in a VM and cooperates with the hypervisor for efficiency.

Instead of emulating real hardware, we define a simple, efficient protocol:
- Guest puts requests in shared memory
- Guest notifies VMM with a single write
- VMM processes all requests
- VMM notifies guest with an interrupt
- Guest processes responses

**One VM exit** instead of many!

## The Virtio Specification

**Virtio** is a standard specification for paravirtualized devices. It defines:
- How devices are discovered
- How guest and VMM communicate
- Standard device types (console, block, network, etc.)

Linux has built-in virtio drivers, so we don't need to write guest-side code.

## Virtio Architecture

### Transport Layers

Virtio can use different transport layers:
- **virtio-pci**: Devices appear as PCI devices (common on x86)
- **virtio-mmio**: Devices appear at MMIO addresses (common on ARM)
- **virtio-ccw**: For IBM s390x

We'll use **virtio-mmio** since it's simpler and natural for ARM.

### The Virtqueue

The core of virtio is the **virtqueue** - a ring buffer for guest-host communication.

Each virtqueue has three parts:

**1. Descriptor Table**
Each descriptor describes a buffer:
- Address in guest memory
- Length
- Flags (read/write, chained)
- Next descriptor index (for chains)

**2. Available Ring**
Guest → VMM: "Here are requests ready for processing"
- Contains indices into the descriptor table

**3. Used Ring**
VMM → Guest: "Here are completed requests"
- Contains indices and bytes written

```
┌────────────────────────────────────────────────────────────┐
│                    Guest Memory                             │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Descriptor Table                         │  │
│  │  ┌────────┬────────┬────────┬────────┐               │  │
│  │  │ Desc 0 │ Desc 1 │ Desc 2 │  ...   │               │  │
│  │  └────────┴────────┴────────┴────────┘               │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────┐ ┌──────────────────────┐        │
│  │    Available Ring    │ │      Used Ring       │        │
│  │  (Guest → VMM)       │ │   (VMM → Guest)      │        │
│  │                      │ │                      │        │
│  │  [idx: 3]            │ │  [idx: 2]            │        │
│  │  ring[0] = 0         │ │  ring[0] = {0, 512}  │        │
│  │  ring[1] = 2         │ │  ring[1] = {2, 256}  │        │
│  │  ring[2] = 5         │ │                      │        │
│  └──────────────────────┘ └──────────────────────┘        │
└────────────────────────────────────────────────────────────┘
```

### Communication Flow

1. Guest allocates buffers in memory
2. Guest creates descriptors pointing to buffers
3. Guest adds descriptor indices to Available Ring
4. Guest writes to notification register → **VM exit**
5. VMM reads Available Ring
6. VMM processes descriptors (reads from / writes to buffers)
7. VMM adds used descriptors to Used Ring
8. VMM injects interrupt
9. Guest reads Used Ring and processes results

## Virtio MMIO Registers

Each virtio-mmio device has a 4KB register space:

```
Offset   Name              Description
------   ----              -----------
0x000    MagicValue        Must read 0x74726976 ("virt")
0x004    Version           Virtio version (1 or 2)
0x008    DeviceID          Device type (1=net, 2=blk, 3=console...)
0x00c    VendorID          Vendor identifier
0x010    DeviceFeatures    Features the device supports
0x014    DeviceFeaturesSel Feature bank selector
0x020    DriverFeatures    Features the driver accepts
0x024    DriverFeaturesSel Feature bank selector
0x030    QueueSel          Select which queue to configure
0x034    QueueNumMax       Max size of selected queue
0x038    QueueNum          Set size of selected queue
0x044    QueueReady        Mark queue as ready
0x050    QueueNotify       Notify device of new buffers
0x060    InterruptStatus   Interrupt status
0x064    InterruptACK      Acknowledge interrupts
0x070    Status            Device status
0x100    QueueDescLow      Descriptor table address (low 32 bits)
0x104    QueueDescHigh     Descriptor table address (high 32 bits)
0x110    QueueDriverLow    Available ring address (low)
0x114    QueueDriverHigh   Available ring address (high)
0x120    QueueDeviceLow    Used ring address (low)
0x124    QueueDeviceHigh   Used ring address (high)
```

## Implementing Virtio MMIO

Create `src/god/devices/virtio/mmio.py`:

```python
"""
Virtio MMIO transport layer.

This implements the virtio-mmio transport for device discovery and
queue configuration.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from god.devices.device import Device


# Virtio MMIO register offsets
VIRTIO_MMIO_MAGIC_VALUE = 0x000
VIRTIO_MMIO_VERSION = 0x004
VIRTIO_MMIO_DEVICE_ID = 0x008
VIRTIO_MMIO_VENDOR_ID = 0x00c
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

# Magic value (reads as "virt")
VIRTIO_MAGIC = 0x74726976

# Virtio device types
VIRTIO_DEV_NET = 1
VIRTIO_DEV_BLK = 2
VIRTIO_DEV_CONSOLE = 3
VIRTIO_DEV_RNG = 4

# Status bits
VIRTIO_STATUS_ACKNOWLEDGE = 1
VIRTIO_STATUS_DRIVER = 2
VIRTIO_STATUS_DRIVER_OK = 4
VIRTIO_STATUS_FEATURES_OK = 8


@dataclass
class VirtQueue:
    """Represents a single virtqueue."""
    num: int = 0          # Queue size
    ready: bool = False   # Queue is configured and ready
    desc_addr: int = 0    # Descriptor table address
    driver_addr: int = 0  # Available ring address
    device_addr: int = 0  # Used ring address


class VirtioDevice(Device, ABC):
    """
    Base class for virtio devices.

    Subclasses implement specific device types (block, console, etc.)
    """

    def __init__(self, base_address: int, device_type: int, num_queues: int = 1):
        self._base_address = base_address
        self._device_type = device_type
        self._size = 0x1000  # 4KB per device

        # Device state
        self._status = 0
        self._features = 0
        self._driver_features = 0
        self._interrupt_status = 0

        # Queues
        self._queues = [VirtQueue() for _ in range(num_queues)]
        self._queue_sel = 0

    @property
    def base_address(self) -> int:
        return self._base_address

    @property
    def size(self) -> int:
        return self._size

    @property
    @abstractmethod
    def device_features(self) -> int:
        """Features this device supports."""
        pass

    @abstractmethod
    def queue_notify(self, queue_index: int):
        """Called when guest notifies a queue has new buffers."""
        pass

    def read(self, offset: int, size: int) -> int:
        if offset == VIRTIO_MMIO_MAGIC_VALUE:
            return VIRTIO_MAGIC
        elif offset == VIRTIO_MMIO_VERSION:
            return 2  # Virtio version 1.0+
        elif offset == VIRTIO_MMIO_DEVICE_ID:
            return self._device_type
        elif offset == VIRTIO_MMIO_VENDOR_ID:
            return 0x554D4551  # "QEMU" - common convention
        elif offset == VIRTIO_MMIO_DEVICE_FEATURES:
            return self.device_features
        elif offset == VIRTIO_MMIO_QUEUE_NUM_MAX:
            return 256  # Max queue size
        elif offset == VIRTIO_MMIO_INTERRUPT_STATUS:
            return self._interrupt_status
        elif offset == VIRTIO_MMIO_STATUS:
            return self._status
        # Add more registers as needed
        return 0

    def write(self, offset: int, size: int, value: int):
        if offset == VIRTIO_MMIO_STATUS:
            self._status = value
            if value == 0:
                self.reset()
        elif offset == VIRTIO_MMIO_QUEUE_SEL:
            self._queue_sel = value
        elif offset == VIRTIO_MMIO_QUEUE_NUM:
            if self._queue_sel < len(self._queues):
                self._queues[self._queue_sel].num = value
        elif offset == VIRTIO_MMIO_QUEUE_READY:
            if self._queue_sel < len(self._queues):
                self._queues[self._queue_sel].ready = bool(value)
        elif offset == VIRTIO_MMIO_QUEUE_NOTIFY:
            self.queue_notify(value)
        elif offset == VIRTIO_MMIO_INTERRUPT_ACK:
            self._interrupt_status &= ~value
        # Add address registers and more as needed

    def reset(self):
        self._status = 0
        self._features = 0
        self._driver_features = 0
        self._interrupt_status = 0
        for q in self._queues:
            q.num = 0
            q.ready = False
            q.desc_addr = 0
            q.driver_addr = 0
            q.device_addr = 0
```

## Virtio Block Device

Create `src/god/devices/virtio/block.py`:

```python
"""
Virtio block device (disk).

This provides a block device backed by a file on the host.
"""

from .mmio import VirtioDevice, VIRTIO_DEV_BLK


class VirtioBlock(VirtioDevice):
    """
    Virtio block device.

    Provides disk access backed by a host file.
    """

    def __init__(self, base_address: int, image_path: str):
        super().__init__(base_address, VIRTIO_DEV_BLK, num_queues=1)
        self._image_path = image_path
        self._image_file = None
        self._capacity = 0

    @property
    def name(self) -> str:
        return f"virtio-blk ({self._image_path})"

    @property
    def device_features(self) -> int:
        # Basic features
        return 0

    def open(self):
        """Open the backing image file."""
        self._image_file = open(self._image_path, "r+b")
        self._image_file.seek(0, 2)  # Seek to end
        self._capacity = self._image_file.tell() // 512  # Sectors

    def queue_notify(self, queue_index: int):
        """Handle queue notification - process I/O requests."""
        # Would read descriptors, perform I/O, update used ring
        pass  # Implementation would go here
```

## Virtio Console

Create `src/god/devices/virtio/console.py`:

```python
"""
Virtio console device.

A more efficient console than PL011 UART.
"""

from .mmio import VirtioDevice, VIRTIO_DEV_CONSOLE


class VirtioConsole(VirtioDevice):
    """
    Virtio console device.

    Provides batched console I/O for better performance than UART.
    """

    def __init__(self, base_address: int):
        # Console has 2 queues: receive (0) and transmit (1)
        super().__init__(base_address, VIRTIO_DEV_CONSOLE, num_queues=2)

    @property
    def name(self) -> str:
        return "virtio-console"

    @property
    def device_features(self) -> int:
        return 0  # Basic console

    def queue_notify(self, queue_index: int):
        """Handle queue notification."""
        if queue_index == 1:
            # Transmit queue - would read data and output
            pass
```

## Performance Comparison

**PL011 UART (Chapter 4):**
- One VM exit per character
- "Hello, World!\n" = 14+ exits

**Virtio Console:**
- One VM exit per notify
- "Hello, World!\n" = 1-2 exits
- Can batch many characters

For disk I/O, the difference is even more dramatic!

## What's Next?

In this chapter, we:

1. Learned why paravirtualization is more efficient
2. Understood the virtio specification
3. Implemented the virtio-mmio transport
4. Created virtio-block and virtio-console devices

In the next chapter, we'll boot Linux! We'll bring everything together: GIC, timer, devices, and finally see Linux running in our VM.

[Continue to Chapter 8: Booting Linux →](08-booting-linux.md)
