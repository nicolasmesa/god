# Chapter 7: Virtio Devices - Efficient Paravirtualization

In this chapter, we'll implement **virtio** devices for efficient guest-host communication. This is a meaty chapter - virtio is the standard for paravirtualized I/O in Linux VMs, and understanding it deeply will pay dividends.

## The Problem with Device Emulation

In Chapter 4, we implemented a PL011 UART. Let's think about what happens when the guest prints "Hello":

```
Guest prints "Hello" (5 characters):

Character 'H':
  1. Guest writes 'H' to UART data register → VM exit
  2. VMM handles write, outputs 'H'
  3. Return to guest

Character 'e':
  4. Guest writes 'e' to UART data register → VM exit
  5. VMM handles write, outputs 'e'
  6. Return to guest

... repeat for 'l', 'l', 'o' ...

Total: 5 VM exits for 5 characters!
```

For a serial console printing debug messages, this is fine. But imagine a disk read:

```
Guest reads 4KB from disk (emulating a real IDE controller):

  1. Guest writes command register → VM exit
  2. Guest writes LBA (Logical Block Address) byte 0 → VM exit
  3. Guest writes LBA byte 1 → VM exit
  4. Guest writes LBA byte 2 → VM exit
  5. Guest writes sector count → VM exit
  6. Guest triggers command → VM exit
  7. VMM performs actual disk I/O
  8. Guest polls status register → VM exit (repeat while busy)
  9. Guest reads data register → VM exit (repeat 4096 times for 4KB!)

Total: Potentially thousands of VM exits for one disk read!
```

Each VM exit costs hundreds to thousands of CPU cycles. This is why emulating real hardware is slow.

## What is Paravirtualization?

**Paravirtualization** (from Greek "para" = beside/alongside) means the guest *knows* it's running in a VM and *cooperates* with the hypervisor for efficiency.

Instead of pretending to be real hardware, we define a simple protocol:

```
Paravirtualized disk read:

  1. Guest puts request in shared memory:
     "Read 8 sectors starting at LBA 1000, put data at address 0x50000"

  2. Guest writes to notification register → VM exit (just ONE!)

  3. VMM reads the request from shared memory
  4. VMM performs the disk I/O
  5. VMM writes data directly to guest memory at 0x50000
  6. VMM triggers an interrupt

  7. Guest interrupt handler sees completion

Total: 1 VM exit + 1 interrupt for any size transfer!
```

The magic is **shared memory** - both guest and VMM can access it, so we don't need VM exits to transfer data.

## The Virtio Specification

**Virtio** is a standardized specification for paravirtualized devices, created by Rusty Russell in 2007 and now maintained by OASIS. It defines:

- How devices are discovered (transport layer)
- How guest and VMM communicate (virtqueues)
- Standard device types (console, block, network, GPU, etc.)

Linux has built-in virtio drivers, so we don't need to write guest-side code - we just need to implement the VMM side correctly, and Linux will talk to our devices.

## Transport Layers: How Devices Are Discovered

Before the guest can use a virtio device, it needs to *find* it. Virtio supports different **transport layers** - think of these as different ways to "plug in" the device:

### virtio-pci

The device appears on the PCI (Peripheral Component Interconnect) bus, just like a real network card or GPU would.

```
┌─────────────────────────────────────────────────────────┐
│                     PCI Bus                              │
├──────────┬──────────┬──────────┬───────────────────────┤
│ Device 0 │ Device 1 │ Device 2 │         ...           │
│ (GPU)    │ (NIC)    │ (virtio) │                       │
└──────────┴──────────┴──────────┴───────────────────────┘

Guest discovery:
  1. Scan PCI bus (read config space at each slot)
  2. Find device with vendor=0x1AF4 (Red Hat), device=0x1000+ (virtio)
  3. Read BARs (Base Address Registers) to find MMIO regions
  4. Configure device via PCI config space + MMIO
```

**Pros:**
- Works with existing PCI infrastructure
- Hot-plug support
- Multiple devices easily enumerated

**Cons:**
- Complex - must emulate PCI configuration space, BARs, etc.
- More code, more VM exits during setup

**Common on:** x86 systems (where PCI is ubiquitous)

### virtio-mmio (Memory-Mapped I/O)

The device appears at a fixed memory address. The guest is *told* where to look (via device tree or ACPI tables).

```
┌─────────────────────────────────────────────────────────┐
│              Guest Physical Address Space                │
├─────────────────────────────────────────────────────────┤
│ 0x00000000 - 0x3FFFFFFF: RAM                            │
│ 0x08000000 - 0x08000FFF: GIC (interrupt controller)     │
│ 0x09000000 - 0x09000FFF: UART                           │
│ 0x0A000000 - 0x0A000FFF: virtio-mmio device 0     ←     │
│ 0x0A001000 - 0x0A001FFF: virtio-mmio device 1     ←     │
└─────────────────────────────────────────────────────────┘

Guest discovery:
  1. Read device tree: "virtio_mmio@0A000000"
  2. Access registers at that address directly
  3. No bus scanning needed
```

**Pros:**
- Simple - just memory reads and writes
- Minimal setup code
- Natural for embedded systems

**Cons:**
- Device addresses must be communicated externally (device tree)
- Less flexible for dynamic device addition

**Common on:** ARM systems (where device tree is standard)

### virtio-ccw

Uses IBM's Channel Command Word architecture. Only relevant for IBM mainframes (s390x). We won't discuss further.

### Our Choice: virtio-mmio

We'll use **virtio-mmio** because:
1. We're on ARM, where device tree is the norm
2. It's simpler to implement
3. It's easier to understand (just memory-mapped registers)

## Data Structure Primer: Ring Buffers

Before diving into virtqueues, let's understand ring buffers - the data structure that makes virtio efficient.

### What is a Ring Buffer?

A **ring buffer** (or circular buffer) is a fixed-size array that "wraps around." When you reach the end, you continue at the beginning.

```python
# Simple ring buffer in Python
class RingBuffer:
    def __init__(self, size: int):
        self.buffer = [None] * size
        self.size = size
        self.write_idx = 0  # Where producer writes next
        self.read_idx = 0   # Where consumer reads next

    def push(self, item):
        """Add an item (producer side)."""
        self.buffer[self.write_idx] = item
        self.write_idx = (self.write_idx + 1) % self.size  # Wrap around!

    def pop(self):
        """Remove an item (consumer side)."""
        item = self.buffer[self.read_idx]
        self.read_idx = (self.read_idx + 1) % self.size  # Wrap around!
        return item

    def num_available(self) -> int:
        """How many items are waiting to be read?"""
        return (self.write_idx - self.read_idx) % self.size
```

Let's trace through an example with size=4:

```
Initial state (empty):
  buffer = [None, None, None, None]
  write_idx = 0
  read_idx = 0

  Indices:    0     1     2     3
            ┌─────┬─────┬─────┬─────┐
            │     │     │     │     │
            └─────┴─────┴─────┴─────┘
              ↑
              write_idx = read_idx = 0

After push('A'), push('B'), push('C'):
  write_idx = 3, read_idx = 0

  Indices:    0     1     2     3
            ┌─────┬─────┬─────┬─────┐
            │  A  │  B  │  C  │     │
            └─────┴─────┴─────┴─────┘
              ↑                 ↑
           read_idx=0      write_idx=3

After pop() returns 'A', pop() returns 'B':
  write_idx = 3, read_idx = 2

  Indices:    0     1     2     3
            ┌─────┬─────┬─────┬─────┐
            │  A  │  B  │  C  │     │  (A, B still in memory but "consumed")
            └─────┴─────┴─────┴─────┘
                          ↑     ↑
                      read_idx write_idx

After push('D'), push('E'):
  write_idx = 1 (wrapped!), read_idx = 2

  Indices:    0     1     2     3
            ┌─────┬─────┬─────┬─────┐
            │  E  │     │  C  │  D  │  (E wrapped to index 0)
            └─────┴─────┴─────┴─────┘
                    ↑     ↑
               write_idx read_idx

  Valid data: indices 2, 3, 0 (in that order) = C, D, E
```

### Why Ring Buffers?

1. **Fixed memory** - No allocation during operation (important for kernel code)
2. **Lock-free potential** - Producer and consumer can work independently
3. **Cache-friendly** - Sequential memory access
4. **Simple wrap logic** - Just use modulo: `(idx + 1) % size`

Virtio uses ring buffers for both the "available ring" (guest → VMM) and "used ring" (VMM → guest).

## Virtqueues: The Heart of Virtio

Each virtio device has one or more **virtqueues**. A virtqueue is the communication channel between guest and VMM.

A virtqueue has three parts:
1. **Descriptor Table** - Describes memory buffers
2. **Available Ring** - Guest tells VMM "here are new requests"
3. **Used Ring** - VMM tells guest "here are completed requests"

All three live in **guest memory**, allocated by the guest driver. The VMM accesses them directly (shared memory!).

### Part 1: The Descriptor Table

The descriptor table is an array of **descriptors**. Each descriptor points to a buffer in guest memory:

```python
from dataclasses import dataclass

# Descriptor flags
VIRTQ_DESC_F_NEXT = 1      # There's another descriptor chained after this
VIRTQ_DESC_F_WRITE = 2     # VMM should write to this buffer (vs read from it)
VIRTQ_DESC_F_INDIRECT = 4  # Buffer contains more descriptors (advanced)

@dataclass
class VirtqDesc:
    """A single descriptor in the descriptor table (16 bytes)."""
    addr: int    # Guest physical address of the buffer
    len: int     # Length of the buffer in bytes
    flags: int   # NEXT, WRITE, INDIRECT flags
    next: int    # Index of next descriptor if NEXT flag is set

# The descriptor table is just an array of these
descriptor_table: list[VirtqDesc] = [VirtqDesc(...) for _ in range(queue_size)]
```

**Memory layout (16 bytes per descriptor):**
```
Offset  Size  Field
0       8     addr   (uint64_t)
8       4     len    (uint32_t)
12      2     flags  (uint16_t)
14      2     next   (uint16_t)
```

### Descriptor Chains

A single I/O operation often involves multiple buffers. For example, a disk read needs:
- A **request header** describing what to read (guest → VMM, read-only for VMM)
- A **data buffer** for the result (VMM → guest, writeable for VMM)
- A **status byte** for success/failure (VMM → guest, writeable for VMM)

These are linked into a **chain** using the `next` field:

```python
def follow_descriptor_chain(desc_table: list[VirtqDesc], head: int) -> list[VirtqDesc]:
    """
    Follow a descriptor chain starting at index 'head'.
    Returns list of descriptors in chain order.
    """
    chain = []
    idx = head

    while True:
        desc = desc_table[idx]
        chain.append(desc)

        # Is there another descriptor in the chain?
        if desc.flags & VIRTQ_DESC_F_NEXT:
            idx = desc.next
        else:
            break  # End of chain

    return chain


# Example: A virtio-blk read request
#
# Descriptor 0: Request header (read by VMM)
#   addr = 0x1000, len = 16, flags = NEXT, next = 1
#
# Descriptor 1: Data buffer (written by VMM)
#   addr = 0x2000, len = 512, flags = NEXT | WRITE, next = 2
#
# Descriptor 2: Status byte (written by VMM)
#   addr = 0x3000, len = 1, flags = WRITE, next = 0 (ignored)
#
# Chain: 0 → 1 → 2 (stop because no NEXT flag on descriptor 2)
```

**Visual representation:**

```
Descriptor Table:
┌────────────────────────────────────────────────────────────────┐
│ [0] addr=0x1000 len=16  flags=NEXT     next=1                 │──┐
├────────────────────────────────────────────────────────────────┤  │
│ [1] addr=0x2000 len=512 flags=NEXT|WR  next=2                 │←─┘──┐
├────────────────────────────────────────────────────────────────┤     │
│ [2] addr=0x3000 len=1   flags=WRITE    next=0                 │←────┘
├────────────────────────────────────────────────────────────────┤
│ [3] (unused)                                                   │
├────────────────────────────────────────────────────────────────┤
│ ...                                                            │
└────────────────────────────────────────────────────────────────┘

Guest Memory:
┌──────────────────┐
│ 0x1000: Request  │  "Read sector 42"        VMM reads this
│         header   │
├──────────────────┤
│ 0x2000: Data     │  (512 bytes for data)    VMM writes here
│         buffer   │
├──────────────────┤
│ 0x3000: Status   │  (1 byte: 0=ok, 1=err)   VMM writes here
└──────────────────┘
```

### Part 2: The Available Ring

The **available ring** is how the guest says "I have new requests for you to process."

```python
@dataclass
class VirtqAvail:
    """The available ring structure."""
    flags: int           # Usually 0 (can disable interrupts)
    idx: int             # Where guest will write next (monotonically increasing)
    ring: list[int]      # Array of descriptor head indices

# Memory layout:
# Offset  Size        Field
# 0       2           flags (uint16_t)
# 2       2           idx (uint16_t)
# 4       2*queue_sz  ring[] (uint16_t each)
```

**How the guest adds a request:**

```python
def guest_submit_request(avail: VirtqAvail, desc_head: int, queue_size: int):
    """Guest submits a new request (descriptor chain starting at desc_head)."""

    # Calculate where in the ring to write
    ring_index = avail.idx % queue_size

    # Write the descriptor head index
    avail.ring[ring_index] = desc_head

    # Memory barrier (ensure ring write is visible before idx update)
    # In real code: atomic_thread_fence(memory_order_release)

    # Increment idx to publish the new entry
    avail.idx += 1

    # Now notify the VMM (write to QueueNotify register → VM exit)
```

**How the VMM reads new requests:**

```python
def vmm_get_new_requests(avail: VirtqAvail, last_seen_idx: int, queue_size: int):
    """
    VMM checks for new requests.
    Returns list of descriptor head indices and updated last_seen_idx.
    """
    new_requests = []

    # Process all new entries since last time we checked
    while last_seen_idx != avail.idx:
        ring_index = last_seen_idx % queue_size
        desc_head = avail.ring[ring_index]
        new_requests.append(desc_head)
        last_seen_idx += 1

    return new_requests, last_seen_idx


# Example trace:
#
# Initial: avail.idx = 0, vmm.last_seen_idx = 0
#   → No new requests
#
# Guest submits request (desc chain head = 0):
#   avail.ring[0] = 0
#   avail.idx = 1
#
# Guest submits another request (desc chain head = 3):
#   avail.ring[1] = 3
#   avail.idx = 2
#
# VMM checks: last_seen_idx=0, avail.idx=2
#   → Returns [0, 3], updates last_seen_idx to 2
#
# VMM checks again: last_seen_idx=2, avail.idx=2
#   → Returns [], no change
```

### Part 3: The Used Ring

The **used ring** is how the VMM says "I finished these requests."

```python
@dataclass
class VirtqUsedElem:
    """One entry in the used ring."""
    id: int    # The descriptor chain head that was processed
    len: int   # Total bytes written to WRITE buffers

@dataclass
class VirtqUsed:
    """The used ring structure."""
    flags: int                # Usually 0
    idx: int                  # Where VMM will write next
    ring: list[VirtqUsedElem] # Array of completion entries

# Memory layout:
# Offset  Size        Field
# 0       2           flags (uint16_t)
# 2       2           idx (uint16_t)
# 4       8*queue_sz  ring[] (each entry is 4+4=8 bytes)
```

**How the VMM marks a request complete:**

```python
def vmm_complete_request(used: VirtqUsed, desc_head: int, bytes_written: int, queue_size: int):
    """VMM marks a request as complete."""

    # Calculate where in the ring to write
    ring_index = used.idx % queue_size

    # Write the completion entry
    used.ring[ring_index] = VirtqUsedElem(id=desc_head, len=bytes_written)

    # Memory barrier

    # Increment idx to publish the completion
    used.idx += 1

    # Optionally inject an interrupt to wake the guest
```

**How the guest reads completions:**

```python
def guest_process_completions(used: VirtqUsed, last_seen_idx: int, queue_size: int):
    """Guest checks for completed requests."""
    completions = []

    while last_seen_idx != used.idx:
        ring_index = last_seen_idx % queue_size
        elem = used.ring[ring_index]
        completions.append(elem)
        last_seen_idx += 1

    return completions, last_seen_idx
```

### Complete Flow: A Disk Read Request

Let's trace through a complete virtio-blk read operation:

```
SETUP (one-time):
════════════════
Guest allocates memory for:
  - Descriptor table at 0x40000000 (256 descriptors × 16 bytes = 4KB)
  - Available ring at 0x40001000
  - Used ring at 0x40002000

Guest writes these addresses to virtio-mmio registers.
Guest sets QueueReady = 1.


STEP 1: Guest Prepares Request
══════════════════════════════
Guest wants to read sector 42 into buffer at 0x50000.

Guest sets up descriptor chain:
  desc[0]: addr=0x48000, len=16, flags=NEXT, next=1      # Request header
  desc[1]: addr=0x50000, len=512, flags=NEXT|WRITE, next=2  # Data buffer
  desc[2]: addr=0x48010, len=1, flags=WRITE, next=0      # Status byte

Request header at 0x48000 contains:
  type = 0 (read)
  sector = 42


STEP 2: Guest Submits Request
═════════════════════════════
avail.ring[0] = 0      # "Start at descriptor 0"
avail.idx = 1          # "One new request"

Guest writes to QueueNotify register → VM EXIT!


STEP 3: VMM Processes Request
═════════════════════════════
VMM sees QueueNotify, checks available ring:
  last_seen_idx was 0, now avail.idx is 1
  → One new request, head = avail.ring[0] = 0

VMM follows chain starting at descriptor 0:
  desc[0]: Read request header from guest address 0x48000
           → "Read sector 42"
  desc[1]: Note this buffer (0x50000, 512 bytes) is for writing
  desc[2]: Note this buffer (0x48010, 1 byte) is for status

VMM reads sector 42 from actual disk.
VMM writes 512 bytes to guest memory at 0x50000.
VMM writes status (0 = success) to guest memory at 0x48010.


STEP 4: VMM Signals Completion
══════════════════════════════
used.ring[0] = { id: 0, len: 513 }   # Completed chain 0, wrote 513 bytes
used.idx = 1                          # "One completion"

VMM injects interrupt to guest.


STEP 5: Guest Handles Completion
════════════════════════════════
Guest interrupt handler runs.
Guest checks used ring:
  last_seen_idx was 0, now used.idx is 1
  → One completion: id=0, len=513

Guest knows request 0 is done.
Data is now available at 0x50000.
Guest frees descriptors 0, 1, 2 for reuse.
```

**Visual timeline:**

```
        Guest                           VMM
          │                              │
          │ ┌─────────────────────┐      │
          │ │ Set up descriptors  │      │
          │ │ desc[0] → [1] → [2] │      │
          │ └─────────────────────┘      │
          │                              │
          │ ┌─────────────────────┐      │
          │ │ avail.ring[0] = 0   │      │
          │ │ avail.idx = 1       │      │
          │ └─────────────────────┘      │
          │                              │
          │ Write QueueNotify            │
          │ ─────────────────────────────│──→ VM Exit
          │                              │
          │                              │ ┌─────────────────────┐
          │                              │ │ Read avail ring     │
          │                              │ │ Follow desc chain   │
          │                              │ │ Read request header │
          │                              │ │ Do actual I/O       │
          │                              │ │ Write data to guest │
          │                              │ │ Write status        │
          │                              │ └─────────────────────┘
          │                              │
          │                              │ ┌─────────────────────┐
          │                              │ │ used.ring[0] = ...  │
          │                              │ │ used.idx = 1        │
          │                              │ └─────────────────────┘
          │                              │
          │               Inject IRQ ←───│
          │                              │
          │ ┌─────────────────────┐      │
          │ │ IRQ handler runs    │      │
          │ │ Check used ring     │      │
          │ │ Process completion  │      │
          │ └─────────────────────┘      │
          │                              │
```

## Virtio MMIO Registers

Each virtio-mmio device has a 4KB register space. Here's the complete register map:

```
Offset  Name                 R/W  Description
──────  ────                 ───  ───────────
0x000   MagicValue           R    Must be 0x74726976 ("virt" in little-endian)
0x004   Version              R    Virtio version (2 for modern virtio 1.0+)
0x008   DeviceID             R    Device type (1=net, 2=blk, 3=console, 4=rng...)
0x00c   VendorID             R    Vendor identifier (we'll use 0x554D4551 "QEMU")
0x010   DeviceFeatures       R    Features bitmap (selected by DeviceFeaturesSel)
0x014   DeviceFeaturesSel    W    Select features word (0 = bits 0-31, 1 = bits 32-63)
0x020   DriverFeatures       W    Features accepted by driver
0x024   DriverFeaturesSel    W    Select driver features word
0x030   QueueSel             W    Select which queue (0, 1, ...) to configure
0x034   QueueNumMax          R    Max size of selected queue (e.g., 256)
0x038   QueueNum             W    Set size of selected queue (must be ≤ max)
0x044   QueueReady           RW   Set to 1 when queue is fully configured
0x050   QueueNotify          W    Write queue index to notify VMM of new buffers
0x060   InterruptStatus      R    Bit 0: used ring update, Bit 1: config change
0x064   InterruptACK         W    Write bits to acknowledge (clear) interrupts
0x070   Status               RW   Device status (see status bits below)
0x0fc   ConfigGeneration     R    Config generation counter
0x100   QueueDescLow         W    Descriptor table address, low 32 bits
0x104   QueueDescHigh        W    Descriptor table address, high 32 bits
0x110   QueueDriverLow       W    Available ring address, low 32 bits
0x114   QueueDriverHigh      W    Available ring address, high 32 bits
0x120   QueueDeviceLow       W    Used ring address, low 32 bits
0x124   QueueDeviceHigh      W    Used ring address, high 32 bits
0x200+  Config               RW   Device-specific configuration space
```

### Status Bits

The Status register tracks device initialization progress:

```python
VIRTIO_STATUS_ACKNOWLEDGE = 1   # Guest has found the device
VIRTIO_STATUS_DRIVER = 2        # Guest has a driver for this device
VIRTIO_STATUS_DRIVER_OK = 4     # Driver is ready to drive the device
VIRTIO_STATUS_FEATURES_OK = 8   # Feature negotiation complete
VIRTIO_STATUS_DEVICE_NEEDS_RESET = 64  # Device experienced an error
VIRTIO_STATUS_FAILED = 128      # Guest gave up on the device
```

### Device Initialization Sequence

The guest driver follows this sequence to initialize a virtio device:

```
1. RESET
   ──────
   Driver writes 0 to Status register.
   Device resets all state.

2. ACKNOWLEDGE
   ───────────
   Driver writes ACKNOWLEDGE (1) to Status.
   "I found you and you're a valid virtio device."

3. DRIVER
   ──────
   Driver writes ACKNOWLEDGE | DRIVER (3) to Status.
   "I have a driver that can handle this device type."

4. FEATURE NEGOTIATION
   ───────────────────
   Driver reads DeviceFeatures (for each bank 0, 1).
   Driver decides which features to accept.
   Driver writes DriverFeatures (for each bank 0, 1).

5. FEATURES_OK
   ───────────
   Driver writes ACKNOWLEDGE | DRIVER | FEATURES_OK (11) to Status.
   Driver re-reads Status to confirm FEATURES_OK is still set.
   (Device may reject by clearing FEATURES_OK)

6. QUEUE SETUP (for each queue)
   ───────────
   Driver writes QueueSel to select queue.
   Driver reads QueueNumMax for maximum size.
   Driver allocates memory for desc table, avail ring, used ring.
   Driver writes queue size to QueueNum.
   Driver writes addresses to QueueDesc*, QueueDriver*, QueueDevice*.
   Driver writes 1 to QueueReady.

7. DRIVER_OK
   ─────────
   Driver writes ACKNOWLEDGE | DRIVER | FEATURES_OK | DRIVER_OK (15) to Status.
   Device is now live!
```

## Implementation

Let's implement virtio-mmio step by step. We'll create:
1. `src/god/devices/virtio/__init__.py` - Package init
2. `src/god/devices/virtio/queue.py` - Virtqueue implementation
3. `src/god/devices/virtio/mmio.py` - MMIO transport
4. `src/god/devices/virtio/console.py` - Console device

### File Structure

```
src/god/devices/virtio/
├── __init__.py
├── queue.py      # Virtqueue handling
├── mmio.py       # MMIO transport layer
└── console.py    # virtio-console device
```

### Step 1: Virtqueue Implementation

Create `src/god/devices/virtio/queue.py`:

```python
"""
Virtqueue implementation.

This module handles the virtqueue data structures: descriptor table,
available ring, and used ring. It provides methods to read descriptors
from guest memory and process I/O requests.
"""

import logging
import struct
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# Descriptor flags
VIRTQ_DESC_F_NEXT = 1       # Descriptor is chained
VIRTQ_DESC_F_WRITE = 2      # Buffer is write-only for device (device writes to it)
VIRTQ_DESC_F_INDIRECT = 4   # Buffer contains indirect descriptors

# Virtqueue size limits
VIRTQ_MAX_SIZE = 256        # Maximum queue size we support


@dataclass
class VirtqDesc:
    """
    A descriptor in the descriptor table.

    Each descriptor points to a buffer in guest memory and can be
    chained to form a scatter-gather list.
    """
    addr: int    # Guest physical address of buffer
    len: int     # Length of buffer in bytes
    flags: int   # VIRTQ_DESC_F_* flags
    next: int    # Index of next descriptor (if F_NEXT is set)

    @property
    def is_chained(self) -> bool:
        """Does this descriptor have a next descriptor?"""
        return bool(self.flags & VIRTQ_DESC_F_NEXT)

    @property
    def is_write(self) -> bool:
        """Should the device write to this buffer (vs read from it)?"""
        return bool(self.flags & VIRTQ_DESC_F_WRITE)


@dataclass
class VirtqUsedElem:
    """An element in the used ring."""
    id: int    # Descriptor chain head index
    len: int   # Total bytes written to write-able descriptors


# Memory access callback type
# Takes (guest_physical_address, size) -> bytes for reads
# Takes (guest_physical_address, data: bytes) -> None for writes
MemoryReader = Callable[[int, int], bytes]
MemoryWriter = Callable[[int, bytes], None]


class Virtqueue:
    """
    A single virtqueue.

    Manages the descriptor table, available ring, and used ring.
    Provides methods to get new requests and mark them complete.
    """

    def __init__(
        self,
        index: int,
        memory_read: MemoryReader,
        memory_write: MemoryWriter,
    ):
        """
        Create a virtqueue.

        Args:
            index: Queue index (0, 1, 2, ...)
            memory_read: Callback to read guest memory
            memory_write: Callback to write guest memory
        """
        self.index = index
        self._memory_read = memory_read
        self._memory_write = memory_write

        # Queue configuration (set during device setup)
        self.num = 0              # Queue size (number of descriptors)
        self.ready = False        # Is queue configured and ready?
        self.desc_addr = 0        # Descriptor table address
        self.avail_addr = 0       # Available ring address (driver area)
        self.used_addr = 0        # Used ring address (device area)

        # Runtime state
        self._last_avail_idx = 0  # Last available index we processed

    def reset(self):
        """Reset queue to initial state."""
        self.num = 0
        self.ready = False
        self.desc_addr = 0
        self.avail_addr = 0
        self.used_addr = 0
        self._last_avail_idx = 0

    # ─────────────────────────────────────────────────────────────
    # Reading from guest memory
    # ─────────────────────────────────────────────────────────────

    def _read_u16(self, addr: int) -> int:
        """Read a 16-bit value from guest memory."""
        data = self._memory_read(addr, 2)
        return struct.unpack("<H", data)[0]

    def _read_u32(self, addr: int) -> int:
        """Read a 32-bit value from guest memory."""
        data = self._memory_read(addr, 4)
        return struct.unpack("<I", data)[0]

    def _read_u64(self, addr: int) -> int:
        """Read a 64-bit value from guest memory."""
        data = self._memory_read(addr, 8)
        return struct.unpack("<Q", data)[0]

    def _write_u16(self, addr: int, value: int):
        """Write a 16-bit value to guest memory."""
        data = struct.pack("<H", value)
        self._memory_write(addr, data)

    def _write_u32(self, addr: int, value: int):
        """Write a 32-bit value to guest memory."""
        data = struct.pack("<I", value)
        self._memory_write(addr, data)

    # ─────────────────────────────────────────────────────────────
    # Descriptor table operations
    # ─────────────────────────────────────────────────────────────

    def read_descriptor(self, index: int) -> VirtqDesc:
        """
        Read a descriptor from the descriptor table.

        Descriptor layout (16 bytes):
          Offset 0:  addr (uint64_t)
          Offset 8:  len (uint32_t)
          Offset 12: flags (uint16_t)
          Offset 14: next (uint16_t)
        """
        if index >= self.num:
            raise ValueError(f"Descriptor index {index} >= queue size {self.num}")

        # Each descriptor is 16 bytes
        desc_offset = index * 16
        desc_base = self.desc_addr + desc_offset

        addr = self._read_u64(desc_base + 0)
        length = self._read_u32(desc_base + 8)
        flags = self._read_u16(desc_base + 12)
        next_idx = self._read_u16(desc_base + 14)

        return VirtqDesc(addr=addr, len=length, flags=flags, next=next_idx)

    def follow_chain(self, head: int) -> list[VirtqDesc]:
        """
        Follow a descriptor chain starting at 'head'.

        Returns list of descriptors in chain order.
        Raises ValueError if chain is malformed (too long, cycle).
        """
        chain = []
        visited = set()
        idx = head

        while True:
            if idx in visited:
                raise ValueError(f"Descriptor chain cycle detected at {idx}")
            if len(chain) > self.num:
                raise ValueError(f"Descriptor chain too long (>{self.num})")

            visited.add(idx)
            desc = self.read_descriptor(idx)
            chain.append(desc)

            if desc.is_chained:
                idx = desc.next
            else:
                break

        return chain

    # ─────────────────────────────────────────────────────────────
    # Available ring operations
    # ─────────────────────────────────────────────────────────────

    def _avail_idx(self) -> int:
        """Read the available ring idx (where guest will write next)."""
        # Available ring layout:
        #   Offset 0: flags (uint16_t)
        #   Offset 2: idx (uint16_t)
        #   Offset 4: ring[0] (uint16_t)
        #   ...
        return self._read_u16(self.avail_addr + 2)

    def _avail_ring_entry(self, ring_index: int) -> int:
        """Read an entry from the available ring."""
        # ring[] starts at offset 4
        return self._read_u16(self.avail_addr + 4 + ring_index * 2)

    def has_new_requests(self) -> bool:
        """Check if there are new requests in the available ring."""
        return self._last_avail_idx != self._avail_idx()

    def get_next_request(self) -> int | None:
        """
        Get the next request (descriptor chain head) from the available ring.

        Returns the descriptor head index, or None if no new requests.
        Updates internal tracking of processed requests.
        """
        avail_idx = self._avail_idx()

        if self._last_avail_idx == avail_idx:
            return None  # No new requests

        # Get the descriptor head from the ring
        ring_index = self._last_avail_idx % self.num
        desc_head = self._avail_ring_entry(ring_index)

        # Mark this entry as processed
        self._last_avail_idx += 1

        logger.debug(
            f"Queue {self.index}: got request, head={desc_head}, "
            f"avail_idx={avail_idx}, processed={self._last_avail_idx}"
        )

        return desc_head

    # ─────────────────────────────────────────────────────────────
    # Used ring operations
    # ─────────────────────────────────────────────────────────────

    def _used_idx(self) -> int:
        """Read the used ring idx."""
        # Used ring layout:
        #   Offset 0: flags (uint16_t)
        #   Offset 2: idx (uint16_t)
        #   Offset 4: ring[0] (struct { uint32_t id; uint32_t len; })
        #   ...
        return self._read_u16(self.used_addr + 2)

    def _set_used_idx(self, value: int):
        """Write the used ring idx."""
        self._write_u16(self.used_addr + 2, value & 0xFFFF)

    def _write_used_ring_entry(self, ring_index: int, desc_id: int, length: int):
        """Write an entry to the used ring."""
        # Each used ring entry is 8 bytes: id (u32) + len (u32)
        entry_addr = self.used_addr + 4 + ring_index * 8
        self._write_u32(entry_addr, desc_id)
        self._write_u32(entry_addr + 4, length)

    def put_used(self, desc_head: int, bytes_written: int):
        """
        Mark a descriptor chain as used (completed).

        Args:
            desc_head: Head of the descriptor chain that was processed
            bytes_written: Total bytes written to write-able descriptors
        """
        used_idx = self._used_idx()
        ring_index = used_idx % self.num

        # Write the used entry
        self._write_used_ring_entry(ring_index, desc_head, bytes_written)

        # Increment the used index (this publishes the entry to the guest)
        self._set_used_idx(used_idx + 1)

        logger.debug(
            f"Queue {self.index}: completed request, head={desc_head}, "
            f"bytes_written={bytes_written}, used_idx={used_idx + 1}"
        )
```

### Step 2: Virtio MMIO Transport

Create `src/god/devices/virtio/mmio.py`:

```python
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
VIRTIO_MMIO_CONFIG = 0x100  # Config space starts at 0x100

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
```

### Step 3: Virtio Console Device

Create `src/god/devices/virtio/console.py`:

```python
"""
Virtio console device.

A paravirtualized console that's more efficient than UART because
it can batch multiple characters per notification.
"""

import logging
import sys
from typing import TYPE_CHECKING, Callable

from god.devices.virtio.mmio import VirtioMMIODevice, VIRTIO_DEV_CONSOLE
from god.devices.virtio.queue import VIRTQ_DESC_F_WRITE

if TYPE_CHECKING:
    from god.memory import Memory

logger = logging.getLogger(__name__)

# Virtio console feature bits
VIRTIO_CONSOLE_F_SIZE = 0        # Console has configurable size
VIRTIO_CONSOLE_F_MULTIPORT = 1   # Console has multiple ports
VIRTIO_CONSOLE_F_EMERG_WRITE = 2 # Emergency write supported

# Queue indices
VIRTIO_CONSOLE_QUEUE_RX = 0   # Receive queue (VMM → guest)
VIRTIO_CONSOLE_QUEUE_TX = 1   # Transmit queue (guest → VMM)


class VirtioConsole(VirtioMMIODevice):
    """
    Virtio console device.

    Provides a serial console using virtio for efficient batched I/O.
    Has two queues:
      - Queue 0 (RX): VMM writes input to guest
      - Queue 1 (TX): Guest writes output to VMM
    """

    def __init__(
        self,
        base_address: int,
        memory: "Memory",
        irq_callback: Callable[[], None] | None = None,
        output_callback: Callable[[bytes], None] | None = None,
    ):
        """
        Create a virtio console.

        Args:
            base_address: Base address in guest physical memory
            memory: Guest memory for virtqueue access
            irq_callback: Called when device wants to raise an interrupt
            output_callback: Called with output data from guest
        """
        super().__init__(
            base_address=base_address,
            num_queues=2,  # RX and TX
            memory=memory,
            irq_callback=irq_callback,
        )

        self._output_callback = output_callback or self._default_output
        self._input_buffer = bytearray()  # Buffered input for guest

    @property
    def name(self) -> str:
        return "virtio-console"

    @property
    def device_id(self) -> int:
        return VIRTIO_DEV_CONSOLE

    @property
    def device_features(self) -> int:
        # We support emergency write (simple early boot output)
        return (1 << VIRTIO_CONSOLE_F_EMERG_WRITE)

    def _default_output(self, data: bytes):
        """Default output handler - write to stdout."""
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def queue_notify(self, queue_index: int):
        """Handle queue notification from guest."""
        if queue_index == VIRTIO_CONSOLE_QUEUE_TX:
            self._process_tx_queue()
        elif queue_index == VIRTIO_CONSOLE_QUEUE_RX:
            # Guest made RX buffers available - try to fill them
            self._process_rx_queue()

    def _process_tx_queue(self):
        """
        Process the transmit queue (guest → VMM).

        Read data from descriptors and output it.
        """
        queue = self._queues[VIRTIO_CONSOLE_QUEUE_TX]

        if not queue.ready:
            logger.warning("TX queue not ready")
            return

        # Process all available requests
        while True:
            desc_head = queue.get_next_request()
            if desc_head is None:
                break

            # Follow the descriptor chain and collect output
            output_data = bytearray()
            chain = queue.follow_chain(desc_head)

            for desc in chain:
                # TX descriptors should be read-only (device reads from them)
                if desc.is_write:
                    logger.warning("TX descriptor unexpectedly marked as write")
                    continue

                # Read data from guest buffer
                data = self._memory.read(desc.addr, desc.len)
                output_data.extend(data)

            # Output the data
            if output_data:
                self._output_callback(bytes(output_data))

            # Mark the request as complete
            queue.put_used(desc_head, 0)

        # Raise interrupt to tell guest we processed some data
        self.raise_interrupt()

    def _process_rx_queue(self):
        """
        Process the receive queue (VMM → guest).

        If we have buffered input, write it to guest buffers.
        """
        if not self._input_buffer:
            return  # Nothing to send

        queue = self._queues[VIRTIO_CONSOLE_QUEUE_RX]

        if not queue.ready:
            return

        # Process available RX buffers
        while self._input_buffer:
            desc_head = queue.get_next_request()
            if desc_head is None:
                break  # No more buffers available

            # Follow the descriptor chain and fill buffers
            bytes_written = 0
            chain = queue.follow_chain(desc_head)

            for desc in chain:
                # RX descriptors should be write-only (device writes to them)
                if not desc.is_write:
                    logger.warning("RX descriptor not marked as write")
                    continue

                # How much can we write to this buffer?
                to_write = min(len(self._input_buffer), desc.len)
                if to_write == 0:
                    break

                # Write data to guest buffer
                data = bytes(self._input_buffer[:to_write])
                self._memory.write(desc.addr, data)
                del self._input_buffer[:to_write]
                bytes_written += to_write

            # Mark the request as complete
            queue.put_used(desc_head, bytes_written)

        # Raise interrupt to tell guest data is available
        if self._input_buffer or True:  # Always raise for now
            self.raise_interrupt()

    def send_input(self, data: bytes):
        """
        Send input data to the guest.

        Data is buffered until guest provides RX buffers.
        """
        self._input_buffer.extend(data)
        self._process_rx_queue()
```

### Step 4: Package Init

Create `src/god/devices/virtio/__init__.py`:

```python
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
```

## Testing Virtio

Testing virtio without a full Linux kernel is challenging because the setup sequence is complex. We have two options:

### Option 1: Unit Tests (What We'll Do Now)

We can test the virtio implementation by simulating the guest side in Python:

```python
# tests/test_virtio.py
"""
Test virtio device implementation by simulating guest behavior.
"""

import struct
import pytest
from god.devices.virtio import VirtioConsole
from god.devices.virtio.mmio import (
    VIRTIO_MMIO_MAGIC_VALUE,
    VIRTIO_MMIO_VERSION,
    VIRTIO_MMIO_DEVICE_ID,
    VIRTIO_MMIO_STATUS,
    VIRTIO_MMIO_QUEUE_SEL,
    VIRTIO_MMIO_QUEUE_NUM,
    VIRTIO_MMIO_QUEUE_DESC_LOW,
    VIRTIO_MMIO_QUEUE_DRIVER_LOW,
    VIRTIO_MMIO_QUEUE_DEVICE_LOW,
    VIRTIO_MMIO_QUEUE_READY,
    VIRTIO_MMIO_QUEUE_NOTIFY,
    VIRTIO_MAGIC,
    VIRTIO_DEV_CONSOLE,
    VIRTIO_STATUS_ACKNOWLEDGE,
    VIRTIO_STATUS_DRIVER,
    VIRTIO_STATUS_FEATURES_OK,
    VIRTIO_STATUS_DRIVER_OK,
)
from god.devices.virtio.queue import VIRTQ_DESC_F_NEXT


class FakeMemory:
    """Fake memory for testing."""

    def __init__(self, size: int = 0x100000):
        self._data = bytearray(size)

    def read(self, addr: int, size: int) -> bytes:
        return bytes(self._data[addr:addr + size])

    def write(self, addr: int, data: bytes):
        self._data[addr:addr + len(data)] = data


class TestVirtioMMIO:
    """Test basic MMIO register access."""

    def test_magic_value(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        magic = console.read(VIRTIO_MMIO_MAGIC_VALUE, 4)
        assert magic == VIRTIO_MAGIC

    def test_version(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        version = console.read(VIRTIO_MMIO_VERSION, 4)
        assert version == 2  # Virtio 1.0+

    def test_device_id(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        device_id = console.read(VIRTIO_MMIO_DEVICE_ID, 4)
        assert device_id == VIRTIO_DEV_CONSOLE


class TestVirtioConsole:
    """Test virtio console functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.memory = FakeMemory(0x100000)
        self.output = bytearray()
        self.irq_raised = False

        def output_callback(data: bytes):
            self.output.extend(data)

        def irq_callback():
            self.irq_raised = True

        self.console = VirtioConsole(
            base_address=0x0a000000,
            memory=self.memory,
            output_callback=output_callback,
            irq_callback=irq_callback,
        )

    def _init_device(self):
        """Simulate guest device initialization."""
        # Reset
        self.console.write(VIRTIO_MMIO_STATUS, 4, 0)

        # Acknowledge
        self.console.write(VIRTIO_MMIO_STATUS, 4, VIRTIO_STATUS_ACKNOWLEDGE)

        # Driver
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER
        )

        # Features OK
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK
        )

        # Driver OK
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER |
            VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK
        )

    def _setup_queue(self, queue_idx: int, queue_size: int, desc_addr: int, avail_addr: int, used_addr: int):
        """Set up a virtqueue."""
        # Select queue
        self.console.write(VIRTIO_MMIO_QUEUE_SEL, 4, queue_idx)

        # Set queue size
        self.console.write(VIRTIO_MMIO_QUEUE_NUM, 4, queue_size)

        # Set addresses
        self.console.write(VIRTIO_MMIO_QUEUE_DESC_LOW, 4, desc_addr & 0xFFFFFFFF)
        self.console.write(VIRTIO_MMIO_QUEUE_DRIVER_LOW, 4, avail_addr & 0xFFFFFFFF)
        self.console.write(VIRTIO_MMIO_QUEUE_DEVICE_LOW, 4, used_addr & 0xFFFFFFFF)

        # Mark ready
        self.console.write(VIRTIO_MMIO_QUEUE_READY, 4, 1)

    def _write_descriptor(self, addr: int, buf_addr: int, buf_len: int, flags: int, next_idx: int):
        """Write a descriptor to memory."""
        desc = struct.pack("<QIHH", buf_addr, buf_len, flags, next_idx)
        self.memory.write(addr, desc)

    def _write_avail_ring(self, addr: int, idx: int, entries: list[int]):
        """Write available ring to memory."""
        # flags (u16) + idx (u16)
        header = struct.pack("<HH", 0, idx)
        self.memory.write(addr, header)

        # ring entries
        for i, entry in enumerate(entries):
            entry_data = struct.pack("<H", entry)
            self.memory.write(addr + 4 + i * 2, entry_data)

    def test_console_output(self):
        """Test guest writing to console."""
        # Initialize device
        self._init_device()

        # Set up TX queue (queue 1)
        # Memory layout:
        #   0x10000: Descriptor table
        #   0x11000: Available ring
        #   0x12000: Used ring
        #   0x20000: Data buffer
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Write "Hello" to data buffer
        message = b"Hello, virtio!"
        self.memory.write(0x20000, message)

        # Set up descriptor pointing to our message
        self._write_descriptor(
            addr=0x10000,      # Descriptor 0
            buf_addr=0x20000,  # Buffer address
            buf_len=len(message),
            flags=0,           # No chaining, read-only
            next_idx=0,
        )

        # Add descriptor to available ring
        self._write_avail_ring(
            addr=0x11000,
            idx=1,        # One entry
            entries=[0],  # Descriptor 0
        )

        # Notify queue 1 (TX)
        self.console.write(VIRTIO_MMIO_QUEUE_NOTIFY, 4, 1)

        # Check output
        assert self.output == message
        assert self.irq_raised
```

### Option 2: Full Integration Test (Chapter 8)

In Chapter 8, we'll boot Linux, and the Linux virtio drivers will fully test our implementation. That's the real validation.

## What We Built

In this chapter, we:

1. **Learned why paravirtualization matters** - Batching I/O dramatically reduces VM exits
2. **Understood the virtio architecture** - Descriptor tables, available rings, used rings
3. **Implemented virtqueue handling** - Reading/writing to shared memory structures
4. **Built virtio-mmio transport** - The register interface for device configuration
5. **Created virtio-console** - A working paravirtualized console

## Performance Comparison

Let's revisit our "Hello, World!" comparison:

**PL011 UART (Chapter 4):**
```
"Hello, World!\n" = 14 characters
Each character = 1 VM exit
Total: 14 VM exits minimum
```

**Virtio Console:**
```
"Hello, World!\n" = 14 characters
Guest writes to buffer, adds descriptor, notifies = 1 VM exit
VMM processes, updates used ring, raises IRQ = 1 interrupt
Total: 1 VM exit + 1 interrupt for any size message!
```

For larger messages (logging, file transfers), the difference is even more dramatic. A 4KB log message:
- UART: 4096 VM exits
- Virtio: 1 VM exit

## What's Next?

In the next chapter, we'll boot Linux! We'll bring together everything we've built:
- Memory management
- vCPU and run loop
- GIC (interrupts)
- Timer
- Virtio console

And finally see a real Linux kernel running in our VM.

[Continue to Chapter 8: Booting Linux →](08-booting-linux.md)
