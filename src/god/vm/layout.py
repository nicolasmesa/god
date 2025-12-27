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

UART = MemoryRegion(
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
    print(UART)
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
