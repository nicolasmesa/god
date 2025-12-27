"""
Virtual Machine management.

This module provides the main VM class that represents a virtual machine
and manages its resources (memory, vCPUs, devices).
"""

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import KVM_CREATE_VM
from god.kvm.system import KVMSystem
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
        self._fd = lib.ioctl(kvm.fd, KVM_CREATE_VM, ffi.cast("int", 0))
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
