# Chapter 2: Creating a VM and Setting Up Memory

In this chapter, we'll create our first virtual machine and set up its memory. By the end, you'll understand how guest memory works and have a working VM ready for a vCPU.

## Virtual Memory Concepts

Before we dive into VM memory, let's make sure we understand how memory works on modern computers. This foundation is essential for understanding virtualization.

### Physical vs. Virtual Addresses

Your computer has RAM—actual physical memory chips. Every byte in RAM has a **physical address**, which is just a number: byte 0, byte 1, ... up to however much RAM you have.

But programs don't use physical addresses directly. Instead, they use **virtual addresses**. The CPU has a component called the **MMU (Memory Management Unit)** that translates virtual addresses to physical addresses.

```
Program uses             CPU's MMU             Actual memory
virtual address    →    translates it    →    at physical address
   0x1000               via page tables          0x7FFF1000
```

**Why virtual memory?**

1. **Isolation**: Each process gets its own virtual address space. Process A's address 0x1000 is completely different from Process B's address 0x1000.

2. **Abstraction**: Programs don't need to know where they're loaded in physical memory. They can always use the same addresses.

3. **Overcommit**: You can allocate more virtual memory than physical memory (the OS will use disk as backup).

4. **Security**: A process can't access another process's memory because the translation prevents it.

### Page Tables

The MMU uses a data structure called a **page table** to translate addresses. Memory is divided into **pages** (typically 4KB each), and the page table maps virtual page numbers to physical page numbers.

```
Virtual Address: 0x12345678
                 ├─────┤ ├─┤
                 Page #   Offset within page

Page Table lookup: Virtual Page 0x12345 → Physical Page 0x7FF10
Physical Address: 0x7FF10678
```

The page table is stored in memory and the CPU caches frequently-used translations in the **TLB (Translation Lookaside Buffer)**.

### How This Applies to VMs

In a virtual machine, we have **two levels** of address translation:

1. **Guest Virtual → Guest Physical**: The guest OS manages its own page tables, just like a normal OS.

2. **Guest Physical → Host Physical**: The hypervisor manages another set of page tables that translate the guest's "physical" addresses to actual physical addresses.

```
┌─────────────────────────────────────────────────────────────────┐
│                         Guest Process                            │
│                 Uses virtual addresses (0x1000...)               │
└────────────────────────────┬────────────────────────────────────┘
                             │ Guest page tables
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Guest Kernel                             │
│                 Sees "physical" addresses                        │
│                 (Guest Physical Addresses - GPAs)                │
└────────────────────────────┬────────────────────────────────────┘
                             │ Stage-2 / EPT page tables
                             ▼ (managed by hypervisor)
┌─────────────────────────────────────────────────────────────────┐
│                          Host RAM                                │
│                 Actual physical addresses                        │
│                 (Host Physical Addresses - HPAs)                 │
└─────────────────────────────────────────────────────────────────┘
```

On ARM64, this second level is called **Stage-2 translation**. The CPU handles both stages automatically—we just need to set up the tables.

## Guest Physical Address Space

### The Guest's View of Memory

From the guest's perspective, it has a physical address space starting at 0. It might think it has:

- RAM from address 0x40000000 to 0x80000000 (1 GB)
- A serial port at address 0x09000000
- An interrupt controller at address 0x08000000

But in reality, these are all virtual. The "RAM" is actually host memory that we allocate. The "serial port" is code in our VMM that handles reads/writes to that address.

### Guest Physical Addresses (GPAs)

When we talk about addresses in the guest, we use the term **Guest Physical Address (GPA)**. From the guest's point of view, these are physical addresses. From our point of view, they're virtual.

### Host Virtual Addresses (HVAs)

Our VMM runs as a normal process on the host. We allocate memory for the guest using normal memory allocation (mmap). These addresses are **Host Virtual Addresses (HVAs)**.

When we set up guest memory, we tell KVM: "When the guest accesses GPA 0x40000000, actually use host memory at HVA 0x7f1234000000."

### The Translation Chain

Here's the complete translation for a guest memory access:

```
Guest program accesses virtual address 0x12345678
        │
        ▼ (Stage-1 translation by guest OS)
Guest Physical Address (GPA) 0x40000678
        │
        ▼ (Stage-2 translation by KVM)
Host Physical Address (HPA) 0x12340678
        │
        ▼ (Host page tables - transparent to us)
Actual RAM location
```

We don't worry about the final step (host page tables)—the host OS handles that. We just need to set up the Stage-2 mapping by telling KVM which host memory backs which guest addresses.

## Memory Slots in KVM

### What is a Memory Slot?

KVM uses **memory slots** to track guest memory regions. Each slot describes a contiguous region of guest physical memory and where it's backed in host memory.

A memory slot contains:
- **Guest Physical Address (GPA)**: Where this memory appears in the guest
- **Size**: How big the region is
- **Host Virtual Address (HVA)**: Where the actual memory is in our process
- **Flags**: Special options (like marking memory as read-only)
- **Slot ID**: A number identifying this slot

### Why Multiple Slots?

We need multiple slots because:

1. **Non-contiguous guest memory**: The guest might have RAM at 0x40000000 and device memory at 0x09000000. These can't be one contiguous slot.

2. **Different properties**: Some regions might be read-only (like a ROM), others read-write (RAM).

3. **Dynamic allocation**: We might add or remove memory regions at runtime.

### The kvm_userspace_memory_region Structure

This is the C structure we pass to KVM when setting up a memory slot:

```c
struct kvm_userspace_memory_region {
    __u32 slot;           // Slot ID (0, 1, 2, ...)
    __u32 flags;          // KVM_MEM_LOG_DIRTY_PAGES, etc.
    __u64 guest_phys_addr; // GPA - where guest sees this memory
    __u64 memory_size;     // Size in bytes
    __u64 userspace_addr;  // HVA - where memory actually is
};
```

Let's define this in our bindings. We'll add to `src/god/kvm/bindings.py`:

```python
# Add to the ffi.cdef() call:
ffi.cdef("""
    // ... previous definitions ...

    // Memory region structure for KVM_SET_USER_MEMORY_REGION
    struct kvm_userspace_memory_region {
        uint32_t slot;
        uint32_t flags;
        uint64_t guest_phys_addr;
        uint64_t memory_size;
        uint64_t userspace_addr;
    };
""")
```

## Our Memory Layout Design

Every virtual machine needs a memory map—a plan for where things go in the guest physical address space. Let's design ours.

### Why This Layout?

We're following a layout similar to QEMU's "virt" machine for ARM64. This is a common layout that Linux knows how to handle.

Key principles:
1. **Devices below RAM**: Device addresses are in the low area (below 0x40000000)
2. **RAM at a known location**: RAM starts at 0x40000000 (1 GB mark)
3. **Room for expansion**: Addresses are spaced out so we can add more devices later

### The Address Map

```
Guest Physical Address Space

0x00000000 ┌─────────────────────────────────────┐
           │  Reserved / Unused                  │
0x08000000 ├─────────────────────────────────────┤
           │  GIC Distributor (64 KB)            │  ← Interrupt controller
0x08010000 ├─────────────────────────────────────┤
           │  Reserved                           │
0x080A0000 ├─────────────────────────────────────┤
           │  GIC Redistributor (128 KB per CPU) │  ← Per-CPU interrupt handling
0x080C0000 ├─────────────────────────────────────┤
           │  Reserved                           │
0x09000000 ├─────────────────────────────────────┤
           │  UART (PL011) (4 KB)                │  ← Serial console
0x09001000 ├─────────────────────────────────────┤
           │  Reserved                           │
0x0A000000 ├─────────────────────────────────────┤
           │  Virtio Device 0 (4 KB)             │  ← Paravirtualized devices
0x0A001000 ├─────────────────────────────────────┤
           │  Virtio Device 1 (4 KB)             │
0x0A002000 ├─────────────────────────────────────┤
           │  ... more virtio devices ...        │
           ├─────────────────────────────────────┤
           │  Reserved                           │
0x40000000 ├─────────────────────────────────────┤
           │                                     │
           │                                     │
           │           RAM (1 GB)                │  ← Guest memory
           │                                     │
           │                                     │
0x80000000 └─────────────────────────────────────┘
```

### Defining the Layout in Code

Create `src/god/vm/layout.py`:

```python
"""
Guest physical address space layout.

This module defines where everything goes in the guest's physical address space.
The layout is similar to QEMU's virt machine for ARM64, which Linux supports well.

All addresses are Guest Physical Addresses (GPAs).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryRegion:
    """
    Describes a region of guest physical memory.

    Attributes:
        name: Human-readable name for debugging
        base: Starting guest physical address
        size: Size in bytes
    """
    name: str
    base: int
    size: int

    @property
    def end(self) -> int:
        """Ending address (exclusive)."""
        return self.base + self.size

    def contains(self, address: int) -> bool:
        """Check if an address falls within this region."""
        return self.base <= address < self.end

    def __str__(self) -> str:
        return f"{self.name}: 0x{self.base:08x} - 0x{self.end:08x} ({self.size // 1024} KB)"


# ============================================================================
# GIC (Generic Interrupt Controller)
# ============================================================================
# The GIC is ARM's interrupt controller. We use GICv3 which has:
# - Distributor: Routes interrupts to CPUs
# - Redistributor: Per-CPU interrupt handling (one per vCPU)

GIC_DISTRIBUTOR = MemoryRegion(
    name="GIC Distributor",
    base=0x0800_0000,
    size=0x0001_0000,  # 64 KB
)

# Redistributor needs 128 KB per CPU. We'll allocate space for up to 8 CPUs.
GIC_REDISTRIBUTOR = MemoryRegion(
    name="GIC Redistributor",
    base=0x080A_0000,
    size=0x0010_0000,  # 1 MB (enough for 8 CPUs)
)


# ============================================================================
# UART (Serial Console)
# ============================================================================
# We emulate ARM's PL011 UART for serial console output.
# This is what "console=ttyAMA0" uses in the kernel command line.

UART_BASE = MemoryRegion(
    name="UART (PL011)",
    base=0x0900_0000,
    size=0x0000_1000,  # 4 KB
)

# UART interrupt number (SPI 1 = 32 + 1 = 33)
UART_IRQ = 33


# ============================================================================
# Virtio Devices
# ============================================================================
# Virtio devices use MMIO transport. Each device gets 4 KB of address space.
# We'll support up to 8 virtio devices.

VIRTIO_BASE = 0x0A00_0000
VIRTIO_SIZE = 0x0000_1000  # 4 KB per device
VIRTIO_COUNT = 8           # Maximum number of virtio devices

# IRQ range for virtio devices (SPI 16-23 = 48-55)
VIRTIO_IRQ_BASE = 48


def get_virtio_region(index: int) -> MemoryRegion:
    """Get the memory region for a virtio device by index."""
    if not 0 <= index < VIRTIO_COUNT:
        raise ValueError(f"Virtio index must be 0-{VIRTIO_COUNT-1}, got {index}")
    return MemoryRegion(
        name=f"Virtio Device {index}",
        base=VIRTIO_BASE + (index * VIRTIO_SIZE),
        size=VIRTIO_SIZE,
    )


def get_virtio_irq(index: int) -> int:
    """Get the IRQ number for a virtio device by index."""
    if not 0 <= index < VIRTIO_COUNT:
        raise ValueError(f"Virtio index must be 0-{VIRTIO_COUNT-1}, got {index}")
    return VIRTIO_IRQ_BASE + index


# ============================================================================
# RAM
# ============================================================================
# Guest RAM starts at 1 GB (0x40000000). This is a common convention for ARM.
# Default size is 1 GB, but this is configurable.

RAM_BASE = 0x4000_0000  # 1 GB
DEFAULT_RAM_SIZE = 0x4000_0000  # 1 GB


def get_ram_region(size: int = DEFAULT_RAM_SIZE) -> MemoryRegion:
    """
    Get the RAM memory region.

    Args:
        size: RAM size in bytes. Default is 1 GB.
    """
    return MemoryRegion(
        name="RAM",
        base=RAM_BASE,
        size=size,
    )


# ============================================================================
# Flash / ROM (optional)
# ============================================================================
# We could add flash memory for UEFI firmware, but we'll boot Linux directly.
# This is here for future expansion.

FLASH_BASE = 0x0000_0000
FLASH_SIZE = 0x0800_0000  # 128 MB


# ============================================================================
# Kernel and DTB Placement
# ============================================================================
# When booting Linux, we need to place:
# - The kernel Image at RAM_BASE + text_offset (usually 0x80000)
# - The Device Tree Blob (DTB) somewhere in RAM
# - The initramfs somewhere in RAM

# Kernel is loaded at RAM base + 0x80000 (512 KB offset)
# This is required by the ARM64 kernel boot protocol
KERNEL_OFFSET = 0x0008_0000

# DTB is placed 2 MB before the end of RAM (or after initramfs if present)
DTB_MAX_SIZE = 0x0020_0000  # 2 MB max for DTB


def print_layout():
    """Print the complete memory layout for debugging."""
    print("Guest Physical Address Space Layout")
    print("=" * 60)
    print()
    print(GIC_DISTRIBUTOR)
    print(GIC_REDISTRIBUTOR)
    print()
    print(UART_BASE)
    print()
    for i in range(VIRTIO_COUNT):
        region = get_virtio_region(i)
        print(f"{region} (IRQ {get_virtio_irq(i)})")
    print()
    print(get_ram_region())
    print()


# When imported, you can run: python -m god.vm.layout
if __name__ == "__main__":
    print_layout()
```

## Implementation: Memory Allocation

Now let's implement the memory allocation. We'll use `mmap` to allocate host memory, then register it with KVM.

### Using mmap

**mmap** (memory map) is a system call that allocates memory or maps files into a process's address space. We'll use it to allocate host memory for guest RAM.

```python
# Conceptually:
ram_ptr = mmap(
    addr=None,        # Let the OS choose where
    length=1 GB,      # How much memory
    prot=PROT_READ | PROT_WRITE,  # Readable and writable
    flags=MAP_PRIVATE | MAP_ANONYMOUS,  # Private allocation, no file
    fd=-1,            # No file (anonymous)
    offset=0,         # No offset
)
```

**Flags explained:**
- `MAP_PRIVATE`: Changes are private to this mapping (not shared with other processes)
- `MAP_ANONYMOUS`: Don't back with a file—just allocate fresh memory

### Alignment Requirements

KVM has alignment requirements for memory regions:
- The host address must be page-aligned (typically 4 KB)
- The guest address must be page-aligned
- The size must be a multiple of the page size

mmap naturally returns page-aligned memory, so we mostly don't need to worry about this.

### Creating the Memory Manager

Create `src/god/vm/memory.py`:

```python
"""
Guest memory management.

This module handles allocating host memory and registering it with KVM
as guest physical memory regions.
"""

from dataclasses import dataclass, field
from typing import Optional

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import (
    KVM_SET_USER_MEMORY_REGION,
    PROT_READ,
    PROT_WRITE,
    MAP_PRIVATE,
    MAP_ANONYMOUS,
)


class MemoryError(Exception):
    """Exception raised when memory operations fail."""
    pass


@dataclass
class MemorySlot:
    """
    Represents a guest memory region.

    Attributes:
        slot_id: KVM slot number
        guest_address: Guest physical address (GPA)
        size: Size in bytes
        host_address: Host virtual address (HVA) - where memory actually is
        flags: KVM memory flags
    """
    slot_id: int
    guest_address: int
    size: int
    host_address: int
    flags: int = 0

    def __str__(self) -> str:
        return (
            f"Slot {self.slot_id}: "
            f"GPA 0x{self.guest_address:08x} - 0x{self.guest_address + self.size:08x} "
            f"({self.size // 1024 // 1024} MB) "
            f"→ HVA 0x{self.host_address:016x}"
        )


class MemoryManager:
    """
    Manages guest memory allocation and registration.

    This class handles:
    - Allocating host memory using mmap
    - Registering memory regions with KVM
    - Tracking active memory slots
    - Cleaning up memory on destruction

    Usage:
        mm = MemoryManager(vm_fd)
        mm.add_ram(guest_address=0x40000000, size=1 * 1024 * 1024 * 1024)
        mm.write(0x40000000, b"Hello!")
        data = mm.read(0x40000000, 6)
    """

    def __init__(self, vm_fd: int):
        """
        Create a memory manager for a VM.

        Args:
            vm_fd: The VM file descriptor from KVM_CREATE_VM.
        """
        self._vm_fd = vm_fd
        self._slots: dict[int, MemorySlot] = {}
        self._next_slot_id = 0

    def add_ram(self, guest_address: int, size: int) -> MemorySlot:
        """
        Allocate RAM and register it with KVM.

        Args:
            guest_address: Where this RAM should appear in the guest (GPA).
            size: Size in bytes. Must be a multiple of 4096.

        Returns:
            The MemorySlot describing this region.

        Raises:
            MemoryError: If allocation or registration fails.
        """
        # Validate alignment
        page_size = 4096
        if guest_address % page_size != 0:
            raise MemoryError(
                f"Guest address 0x{guest_address:x} must be page-aligned (4 KB)"
            )
        if size % page_size != 0:
            raise MemoryError(
                f"Size {size} must be a multiple of page size (4 KB)"
            )
        if size == 0:
            raise MemoryError("Size must be greater than 0")

        # Allocate host memory using mmap
        # MAP_ANONYMOUS: Not backed by a file, just fresh memory
        # MAP_PRIVATE: Changes are private to this mapping
        host_ptr = lib.mmap(
            ffi.NULL,  # Let the OS choose where to put it
            size,
            PROT_READ | PROT_WRITE,
            MAP_PRIVATE | MAP_ANONYMOUS,
            -1,  # No file descriptor (anonymous mapping)
            0,   # No offset
        )

        if host_ptr == ffi.cast("void *", -1):  # MAP_FAILED
            raise MemoryError(f"mmap failed: errno {get_errno()}")

        host_address = int(ffi.cast("uintptr_t", host_ptr))

        # Create the slot
        slot_id = self._next_slot_id
        self._next_slot_id += 1

        slot = MemorySlot(
            slot_id=slot_id,
            guest_address=guest_address,
            size=size,
            host_address=host_address,
        )

        # Register with KVM
        self._register_slot(slot)
        self._slots[slot_id] = slot

        return slot

    def _register_slot(self, slot: MemorySlot):
        """Register a memory slot with KVM."""
        # Build the kvm_userspace_memory_region structure
        region = ffi.new("struct kvm_userspace_memory_region *")
        region.slot = slot.slot_id
        region.flags = slot.flags
        region.guest_phys_addr = slot.guest_address
        region.memory_size = slot.size
        region.userspace_addr = slot.host_address

        # Call the ioctl
        result = lib.ioctl(self._vm_fd, KVM_SET_USER_MEMORY_REGION, region)
        if result < 0:
            # Clean up the mmap if registration failed
            lib.munmap(ffi.cast("void *", slot.host_address), slot.size)
            raise MemoryError(
                f"Failed to register memory slot: errno {get_errno()}"
            )

    def get_host_address(self, guest_address: int) -> Optional[int]:
        """
        Translate a guest physical address to a host virtual address.

        Args:
            guest_address: The guest physical address (GPA).

        Returns:
            The host virtual address (HVA), or None if the address
            is not in any registered memory region.
        """
        for slot in self._slots.values():
            if slot.guest_address <= guest_address < slot.guest_address + slot.size:
                offset = guest_address - slot.guest_address
                return slot.host_address + offset
        return None

    def read(self, guest_address: int, size: int) -> bytes:
        """
        Read bytes from guest memory.

        Args:
            guest_address: Guest physical address to read from.
            size: Number of bytes to read.

        Returns:
            The bytes read.

        Raises:
            MemoryError: If the address is not in guest memory.
        """
        host_address = self.get_host_address(guest_address)
        if host_address is None:
            raise MemoryError(
                f"Guest address 0x{guest_address:x} is not in any memory region"
            )

        # Read from host memory
        ptr = ffi.cast("uint8_t *", host_address)
        return bytes(ptr[i] for i in range(size))

    def write(self, guest_address: int, data: bytes):
        """
        Write bytes to guest memory.

        Args:
            guest_address: Guest physical address to write to.
            data: Bytes to write.

        Raises:
            MemoryError: If the address is not in guest memory.
        """
        host_address = self.get_host_address(guest_address)
        if host_address is None:
            raise MemoryError(
                f"Guest address 0x{guest_address:x} is not in any memory region"
            )

        # Write to host memory
        ptr = ffi.cast("uint8_t *", host_address)
        for i, byte in enumerate(data):
            ptr[i] = byte

    def load_file(self, guest_address: int, file_path: str) -> int:
        """
        Load a file into guest memory.

        Args:
            guest_address: Guest physical address to load at.
            file_path: Path to the file to load.

        Returns:
            Number of bytes loaded.

        Raises:
            MemoryError: If the address is not in guest memory.
            FileNotFoundError: If the file doesn't exist.
        """
        with open(file_path, "rb") as f:
            data = f.read()

        self.write(guest_address, data)
        return len(data)

    @property
    def slots(self) -> list[MemorySlot]:
        """Get all registered memory slots."""
        return list(self._slots.values())

    def cleanup(self):
        """Free all allocated memory."""
        for slot in self._slots.values():
            # Unmap the host memory
            lib.munmap(ffi.cast("void *", slot.host_address), slot.size)

            # Tell KVM to forget this slot (set size to 0)
            region = ffi.new("struct kvm_userspace_memory_region *")
            region.slot = slot.slot_id
            region.flags = 0
            region.guest_phys_addr = slot.guest_address
            region.memory_size = 0  # Size 0 means remove the slot
            region.userspace_addr = 0

            lib.ioctl(self._vm_fd, KVM_SET_USER_MEMORY_REGION, region)

        self._slots.clear()

    def __del__(self):
        """Clean up when garbage collected."""
        self.cleanup()
```

## Implementation: The VM Class

Now let's create the main VM class that ties everything together. Create `src/god/vm/vm.py`:

```python
"""
Virtual Machine management.

This module provides the main VM class that represents a virtual machine
and manages its resources (memory, vCPUs, devices).
"""

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import KVM_CREATE_VM, O_CLOEXEC
from god.kvm.system import KVMSystem, KVMError
from .memory import MemoryManager, MemorySlot
from .layout import RAM_BASE, DEFAULT_RAM_SIZE


class VMError(Exception):
    """Exception raised when VM operations fail."""
    pass


class VirtualMachine:
    """
    Represents a virtual machine.

    This class manages the VM lifecycle and provides access to its resources.

    Usage:
        with KVMSystem() as kvm:
            vm = VirtualMachine(kvm, ram_size=1 * 1024 * 1024 * 1024)
            # ... set up vCPU, load code, run ...
            vm.close()
    """

    def __init__(self, kvm: KVMSystem, ram_size: int = DEFAULT_RAM_SIZE):
        """
        Create a new virtual machine.

        Args:
            kvm: An open KVMSystem instance.
            ram_size: Size of RAM in bytes. Default is 1 GB.

        Raises:
            VMError: If VM creation fails.
        """
        self._kvm = kvm
        self._fd = -1
        self._memory: MemoryManager | None = None
        self._ram_size = ram_size
        self._closed = False

        # Create the VM
        # The argument is the machine type. 0 means default.
        self._fd = lib.ioctl(kvm.fd, KVM_CREATE_VM, 0)
        if self._fd < 0:
            raise VMError(f"Failed to create VM: errno {get_errno()}")

        # Set up memory manager
        self._memory = MemoryManager(self._fd)

        # Allocate RAM
        self._ram_slot = self._memory.add_ram(RAM_BASE, ram_size)

    @property
    def fd(self) -> int:
        """Get the VM file descriptor."""
        if self._fd < 0:
            raise VMError("VM is closed")
        return self._fd

    @property
    def memory(self) -> MemoryManager:
        """Get the memory manager."""
        if self._memory is None:
            raise VMError("VM is closed")
        return self._memory

    @property
    def ram_base(self) -> int:
        """Get the base address of RAM in guest physical memory."""
        return RAM_BASE

    @property
    def ram_size(self) -> int:
        """Get the size of RAM in bytes."""
        return self._ram_size

    def close(self):
        """
        Close the VM and free all resources.

        This cleans up memory and closes the file descriptor.
        """
        if self._closed:
            return

        self._closed = True

        # Clean up memory
        if self._memory is not None:
            self._memory.cleanup()
            self._memory = None

        # Close VM file descriptor
        if self._fd >= 0:
            lib.close(self._fd)
            self._fd = -1

    def __enter__(self):
        """Support for 'with' statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the VM when exiting 'with' block."""
        self.close()
        return False

    def __del__(self):
        """Clean up when garbage collected."""
        self.close()

    def __str__(self) -> str:
        return (
            f"VirtualMachine(ram={self._ram_size // 1024 // 1024} MB, "
            f"fd={self._fd})"
        )
```

## Testing

### Verifying Memory Setup

Let's add a command to test our VM creation. Update `src/god/cli.py` to add a test command:

```python
# Add this import at the top
from typing import Optional

# Add a new command
@app.command("test-vm")
def test_vm(
    ram_mb: int = typer.Option(
        1024,
        "--ram",
        "-r",
        help="RAM size in megabytes",
    ),
):
    """
    Test VM creation and memory setup.

    Creates a VM, allocates memory, writes some data, reads it back,
    and verifies everything works.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")
    print()

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                print(f"VM created: {vm}")
                print()
                print("Memory slots:")
                for slot in vm.memory.slots:
                    print(f"  {slot}")
                print()

                # Write some data to memory
                test_address = vm.ram_base
                test_data = b"Hello from the VMM!"

                print(f"Writing test data to 0x{test_address:08x}...")
                vm.memory.write(test_address, test_data)

                # Read it back
                print(f"Reading back from 0x{test_address:08x}...")
                read_back = vm.memory.read(test_address, len(test_data))

                if read_back == test_data:
                    print(f"Success! Read: {read_back}")
                else:
                    print(f"MISMATCH! Wrote: {test_data}, Read: {read_back}")
                    raise typer.Exit(code=1)

                print()
                print("VM test passed!")

    except (KVMError, VMError) as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1)
```

### Running the Test

Inside the Lima VM:

```bash
cd /path/to/workspace/veleiro-god
uv run god test-vm
```

Expected output:

```
Creating VM with 1024 MB RAM...

VM created: VirtualMachine(ram=1024 MB, fd=4)

Memory slots:
  Slot 0: GPA 0x40000000 - 0x80000000 (1024 MB) → HVA 0x0000ffff80000000

Writing test data to 0x40000000...
Reading back from 0x40000000...
Success! Read: b'Hello from the VMM!'

VM test passed!
```

### Verifying in /proc

You can also verify the memory allocation by looking at `/proc/<pid>/maps` while the VM is running. Add a pause to the test:

```python
# Temporarily add this to test-vm:
input("Press Enter to continue...")
```

Then in another terminal:

```bash
# Find the process
pgrep -f "god test-vm"

# Look at its memory map
cat /proc/<PID>/maps | grep anon
```

You should see a ~1 GB anonymous mapping.

## Deep Dive: Stage-2 Translation on ARM

If you're curious about how ARM handles the second level of address translation, here's a deeper look.

### The IPA (Intermediate Physical Address)

ARM calls guest physical addresses **IPAs (Intermediate Physical Addresses)**. The term "intermediate" reflects that these addresses are between guest virtual and host physical.

### VTTBR_EL2

KVM sets up a Stage-2 page table and points to it using the **VTTBR_EL2** register (Virtualization Translation Table Base Register at Exception Level 2).

When the guest accesses memory:
1. Guest virtual → Guest physical (Stage-1, using guest's TTBR1_EL1)
2. Guest physical (IPA) → Host physical (Stage-2, using VTTBR_EL2)

The CPU handles both translations automatically in hardware.

### IPA Size

The `KVM_CAP_ARM_VM_IPA_SIZE` capability tells us how many bits of IPA the CPU supports. Common values:
- 40 bits: 1 TB of guest physical address space
- 48 bits: 256 TB of guest physical address space

This limits how much "RAM" we can give the guest, but 1 TB is plenty!

## Gotchas

### Memory Alignment

Memory regions must be page-aligned (4 KB). The mmap call naturally returns aligned memory, but if you're computing addresses manually, be careful.

```python
# Wrong - not aligned
guest_addr = 0x40000100

# Right - aligned
guest_addr = 0x40000000
```

### Overlapping Regions

KVM doesn't allow overlapping memory regions in the same slot. If you try to create a slot that overlaps with an existing one, you'll get an error.

### Size Must Be Non-Zero

A memory region with size 0 is special—it means "remove this slot." Don't accidentally create zero-sized regions.

### Maximum Memory Size

The guest physical address space is limited by the IPA size (check `KVM_CAP_ARM_VM_IPA_SIZE`). On most systems, this is at least 40 bits (1 TB), so it's rarely a problem.

Also, allocating very large amounts of memory may fail if the host doesn't have enough RAM. The mmap call will fail with ENOMEM.

## What's Next?

In this chapter, we:

1. Learned how virtual memory works and how it applies to VMs
2. Understood memory slots and how KVM tracks guest memory
3. Designed our memory layout
4. Implemented memory allocation and registration
5. Created the VM class
6. Tested that everything works

In the next chapter, we'll create a virtual CPU and run our first guest code. We'll learn about the run loop—the heart of any VMM—and see actual code executing in our virtual machine.

[Continue to Chapter 3: Virtual CPUs and the Run Loop →](03-vcpu-run-loop.md)
