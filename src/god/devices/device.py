"""
Base class for emulated devices.

All devices that handle MMIO accesses should inherit from this class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class MMIOAccess:
    """
    Describes an MMIO access from the guest.

    When the guest reads from or writes to an address that isn't RAM,
    KVM exits with KVM_EXIT_MMIO. We package that information here.

    Attributes:
        address: The guest physical address accessed
        size: Access size in bytes (1, 2, 4, or 8)
        is_write: True for writes, False for reads
        data: For writes, the data being written (as int)
    """
    address: int
    size: int
    is_write: bool
    data: int = 0


@dataclass
class MMIOResult:
    """
    Result of handling an MMIO access.

    Attributes:
        data: For reads, the data to return to the guest (as int)
        handled: Whether the access was handled by a device
    """
    data: int = 0
    handled: bool = True


class Device(ABC):
    """
    Base class for all emulated devices.

    A device occupies a region of guest physical address space and handles
    reads and writes to that region. When the guest accesses an address
    in the device's range, the VMM calls read() or write().

    Subclasses must implement:
    - name: Human-readable device name
    - base_address: Where the device is in guest memory
    - size: Size of the device's MMIO region
    - read(): Handle read accesses
    - write(): Handle write accesses

    Example:
        class MyDevice(Device):
            @property
            def name(self) -> str:
                return "My Device"

            @property
            def base_address(self) -> int:
                return 0x1000_0000

            @property
            def size(self) -> int:
                return 0x1000  # 4 KB

            def read(self, offset: int, size: int) -> int:
                # Return data for reads
                return 0

            def write(self, offset: int, size: int, value: int):
                # Handle writes
                pass
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable device name for debugging."""
        pass

    @property
    @abstractmethod
    def base_address(self) -> int:
        """Base address in guest physical memory."""
        pass

    @property
    @abstractmethod
    def size(self) -> int:
        """Size of the device's MMIO region in bytes."""
        pass

    def contains(self, address: int) -> bool:
        """Check if a guest physical address falls within this device's region."""
        return self.base_address <= address < self.base_address + self.size

    def offset(self, address: int) -> int:
        """
        Convert a guest physical address to an offset within the device.

        For example, if base_address is 0x09000000 and address is 0x09000018,
        this returns 0x18.
        """
        return address - self.base_address

    @abstractmethod
    def read(self, offset: int, size: int) -> int:
        """
        Handle a read from the device.

        Args:
            offset: Offset within the device (0 = base_address)
            size: Read size in bytes (1, 2, 4, or 8)

        Returns:
            The value to return to the guest.
        """
        pass

    @abstractmethod
    def write(self, offset: int, size: int, value: int):
        """
        Handle a write to the device.

        Args:
            offset: Offset within the device (0 = base_address)
            size: Write size in bytes (1, 2, 4, or 8)
            value: The value being written
        """
        pass

    def reset(self):
        """
        Reset the device to its initial state.

        Override this if your device has state that needs resetting.
        """
        pass
