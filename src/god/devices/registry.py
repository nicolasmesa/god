"""
Device registry for MMIO dispatch.

The registry keeps track of all devices and their address ranges.
When we get a KVM_EXIT_MMIO, we look up which device handles that
address and dispatch to it.
"""

from .device import Device, MMIOAccess, MMIOResult


class DeviceRegistry:
    """
    Manages devices and dispatches MMIO accesses.

    The registry maintains a list of devices. When the VMM receives
    KVM_EXIT_MMIO, it calls handle_mmio() which finds the right device
    and dispatches to it.

    Usage:
        registry = DeviceRegistry()
        registry.register(uart)
        registry.register(virtio_blk)

        # When we get KVM_EXIT_MMIO:
        result = registry.handle_mmio(access)
    """

    def __init__(self):
        self._devices: list[Device] = []

    def register(self, device: Device):
        """
        Register a device.

        Args:
            device: The device to register.

        Raises:
            ValueError: If the device's address range overlaps with
                        an already-registered device.
        """
        # Check for overlaps with existing devices
        for existing in self._devices:
            if self._overlaps(device, existing):
                raise ValueError(
                    f"Device {device.name} (0x{device.base_address:08x}-"
                    f"0x{device.base_address + device.size:08x}) "
                    f"overlaps with {existing.name} (0x{existing.base_address:08x}-"
                    f"0x{existing.base_address + existing.size:08x})"
                )

        self._devices.append(device)
        print(f"Registered device: {device.name} at 0x{device.base_address:08x}")

    def _overlaps(self, a: Device, b: Device) -> bool:
        """Check if two devices' address ranges overlap."""
        a_end = a.base_address + a.size
        b_end = b.base_address + b.size
        # Two ranges overlap unless one ends before the other starts
        return not (a_end <= b.base_address or b_end <= a.base_address)

    def find_device(self, address: int) -> Device | None:
        """
        Find the device that handles a given address.

        Args:
            address: Guest physical address.

        Returns:
            The device, or None if no device handles this address.
        """
        for device in self._devices:
            if device.contains(address):
                return device
        return None

    def handle_mmio(self, access: MMIOAccess) -> MMIOResult:
        """
        Handle an MMIO access by dispatching to the appropriate device.

        Args:
            access: The MMIO access details from KVM.

        Returns:
            The result of handling the access. If no device handles the
            address, returns data=0 and handled=False.
        """
        device = self.find_device(access.address)

        if device is None:
            # No device at this address - warn and return zeros
            print(
                f"Warning: Unhandled MMIO {'write' if access.is_write else 'read'} "
                f"at 0x{access.address:08x}"
            )
            return MMIOResult(data=0, handled=False)

        # Calculate offset within the device
        offset = device.offset(access.address)

        if access.is_write:
            device.write(offset, access.size, access.data)
            return MMIOResult(handled=True)
        else:
            value = device.read(offset, access.size)
            return MMIOResult(data=value, handled=True)

    def reset_all(self):
        """Reset all registered devices to their initial state."""
        for device in self._devices:
            device.reset()

    @property
    def devices(self) -> list[Device]:
        """Get the list of registered devices (read-only)."""
        return list(self._devices)
