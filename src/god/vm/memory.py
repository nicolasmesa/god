"""
Guest memory management.

This module handles allocating host memory and registering it with KVM
as guest physical memory regions.
"""

from dataclasses import dataclass
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
    Represents a guest memory region registered with KVM.

    Attributes:
        slot_id: KVM slot number (used to identify this region to KVM)
        guest_address: Guest physical address (GPA) - where the guest sees it
        size: Size in bytes
        host_address: Host virtual address (HVA) - where the memory actually is
        flags: KVM memory flags (e.g., read-only)
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
            f"-> HVA 0x{self.host_address:016x}"
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

        This does two things:
        1. Allocates host memory using mmap (MAP_ANONYMOUS)
        2. Tells KVM to map that memory into the guest's address space

        Args:
            guest_address: Where this RAM should appear in the guest (GPA).
            size: Size in bytes. Must be a multiple of 4096 (page size).

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
