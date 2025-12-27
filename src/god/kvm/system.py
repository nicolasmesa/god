"""
KVM system-level operations.

This module handles opening /dev/kvm and performing system-level queries
like checking the API version and supported capabilities.
"""

from .bindings import ffi, get_errno, lib
from .constants import (
    KVM_CHECK_EXTENSION,
    KVM_GET_API_VERSION,
    KVM_GET_VCPU_MMAP_SIZE,
    O_CLOEXEC,
    O_RDWR,
)


class KVMError(Exception):
    """Exception raised when a KVM operation fails."""

    pass


class KVMSystem:
    """
    Represents the KVM system interface (/dev/kvm).

    This class handles opening /dev/kvm and provides methods for system-level
    operations like checking capabilities and creating VMs.

    Usage:
        kvm = KVMSystem()
        print(f"KVM API version: {kvm.api_version}")
        print(f"Max vCPUs: {kvm.check_extension(KVM_CAP_MAX_VCPUS)}")
    """

    # The expected KVM API version
    # This has been stable since 2007 - if it changes, something is very wrong
    EXPECTED_API_VERSION = 12

    def __init__(self, device_path: str = "/dev/kvm"):
        """
        Open the KVM device.

        Args:
            device_path: Path to the KVM device file. Almost always /dev/kvm.

        Raises:
            KVMError: If /dev/kvm cannot be opened or the API version is wrong.
        """
        self._device_path = device_path
        self._fd = -1

        # Open /dev/kvm
        # O_RDWR: We need both read and write access
        # O_CLOEXEC: Close this FD if we exec another program (security best practice)
        self._fd = lib.open(device_path.encode(), O_RDWR | O_CLOEXEC)

        if self._fd < 0:
            errno = get_errno()
            if errno == 2:  # ENOENT - file not found
                raise KVMError(
                    f"KVM device not found at {device_path}. "
                    "Is KVM available on this system? "
                    "On Linux, check if the kvm module is loaded: lsmod | grep kvm"
                )
            elif errno == 13:  # EACCES - permission denied
                raise KVMError(
                    f"Permission denied opening {device_path}. "
                    "Try adding yourself to the 'kvm' group: sudo usermod -aG kvm $USER"
                )
            else:
                raise KVMError(f"Failed to open {device_path}: errno {errno}")

        # Check API version
        # Note: cffi varargs require an explicit third argument, even for ioctls
        # that don't need data. We pass 0 cast to int.
        self._api_version = lib.ioctl(self._fd, KVM_GET_API_VERSION, ffi.cast("int", 0))
        if self._api_version < 0:
            self.close()
            raise KVMError(f"Failed to get KVM API version: errno {get_errno()}")

        if self._api_version != self.EXPECTED_API_VERSION:
            self.close()
            raise KVMError(
                f"Unexpected KVM API version {self._api_version}, "
                f"expected {self.EXPECTED_API_VERSION}. "
                "This version of the VMM may not be compatible with your kernel."
            )

    def close(self):
        """Close the KVM device."""
        if self._fd >= 0:
            lib.close(self._fd)
            self._fd = -1

    def __enter__(self):
        """Support for 'with' statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the device when exiting 'with' block."""
        self.close()
        return False

    @property
    def fd(self) -> int:
        """
        Get the file descriptor for /dev/kvm.

        This is useful when you need to make ioctl calls directly.
        """
        if self._fd < 0:
            raise KVMError("KVM device is closed")
        return self._fd

    @property
    def api_version(self) -> int:
        """
        Get the KVM API version.

        This should always be 12 for modern kernels.
        """
        return self._api_version

    def check_extension(self, capability: int) -> int:
        """
        Check if a KVM extension/capability is supported.

        Args:
            capability: The capability number (KVM_CAP_*).

        Returns:
            The capability value. 0 means not supported, >0 means supported
            (the exact value may have meaning depending on the capability).
        """
        # The capability number is passed as the third argument to the ioctl
        result = lib.ioctl(self._fd, KVM_CHECK_EXTENSION, ffi.cast("int", capability))
        if result < 0:
            # Some capabilities return -1 for "not supported" instead of 0
            return 0
        return result

    def get_vcpu_mmap_size(self) -> int:
        """
        Get the size of the memory area to mmap for each vCPU.

        When we create a vCPU, we need to mmap a region of memory that
        contains the kvm_run structure. This tells us how big that region is.

        Returns:
            Size in bytes.
        """
        size = lib.ioctl(self._fd, KVM_GET_VCPU_MMAP_SIZE, ffi.cast("int", 0))
        if size < 0:
            raise KVMError(f"Failed to get vCPU mmap size: errno {get_errno()}")
        return size
