"""
GIC (Generic Interrupt Controller) setup.

The GIC is ARM's standard interrupt controller. It routes interrupt signals
from devices (like our UART and timer) to CPU cores. Without it, the guest
can't receive asynchronous notifications - everything would have to be polled.

We use KVM's in-kernel GICv3 emulation because:
1. Interrupt handling is timing-critical (latency matters)
2. The GIC interacts directly with vCPU state
3. KVM can optimize interrupt delivery during KVM_RUN

This module handles creating and configuring the GIC. The actual interrupt
routing and delivery is handled by KVM internally.

GICv3 Components:
- Distributor (GICD): Routes interrupts to CPUs, one per system
- Redistributor (GICR): Per-CPU interrupt handling, one per vCPU
- CPU Interface: System registers (ICC_*) accessed by the guest
"""

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import (
    KVM_CREATE_DEVICE,
    KVM_SET_DEVICE_ATTR,
    KVM_IRQ_LINE,
    KVM_DEV_TYPE_ARM_VGIC_V3,
    KVM_DEV_ARM_VGIC_GRP_ADDR,
    KVM_DEV_ARM_VGIC_GRP_CTRL,
    KVM_VGIC_V3_ADDR_TYPE_DIST,
    KVM_VGIC_V3_ADDR_TYPE_REDIST,
    KVM_DEV_ARM_VGIC_CTRL_INIT,
)
from god.vm.layout import GIC_DISTRIBUTOR, GIC_REDISTRIBUTOR


class GICError(Exception):
    """Exception raised when GIC operations fail."""
    pass


class GIC:
    """
    Manages the GIC (Generic Interrupt Controller).

    This sets up KVM's in-kernel GICv3 emulation. The GIC routes interrupt
    signals from devices (UART, timer, etc.) to CPU cores.

    The initialization sequence is:
    1. Create GIC and set addresses (create()) - before vCPUs
    2. Create all vCPU(s)
    3. Finalize GIC (finalize()) - after all vCPUs

    The GIC must be created before vCPUs exist, but finalized after all
    vCPUs are created. This is because finalization sets up per-CPU
    redistributors for each vCPU.

    Usage (with VMRunner - recommended):
        runner = VMRunner(vm, kvm)  # GIC created here
        runner.create_vcpu()
        runner.create_vcpu()
        runner.run()  # GIC finalized here, then execution starts
        runner.gic.inject_irq(33, level=True)

    Usage (manual - rare):
        gic = GIC(vm.fd)
        gic.create()              # Before vCPUs
        vcpu0 = VCPU(...)
        vcpu1 = VCPU(...)
        gic.finalize()            # After all vCPUs
        gic.inject_irq(33)
    """

    def __init__(self, vm_fd: int, num_cpus: int = 1):
        """
        Create a GIC manager.

        Args:
            vm_fd: The VM file descriptor.
            num_cpus: Number of CPUs (affects redistributor size).
                      Each CPU needs 128KB of redistributor space.
        """
        self._vm_fd = vm_fd
        self._num_cpus = num_cpus
        self._fd = -1  # GIC device file descriptor (set by create())
        self._created = False
        self._finalized = False

    def create(self) -> None:
        """
        Create the GIC device and set its addresses.

        This performs two steps:
        1. Create the GIC device (KVM_CREATE_DEVICE)
        2. Configure the addresses (Distributor and Redistributor)

        After calling create(), you must:
        1. Create all vCPUs
        2. Call finalize() to complete GIC initialization

        Raises:
            GICError: If any step fails.
        """
        if self._created:
            return

        # Step 1: Create the GIC device
        self._create_device()

        # Step 2: Configure addresses
        # These tell KVM where the GIC appears in the guest's physical address space.
        # The guest will access these addresses to configure interrupts.
        self._set_distributor_address(GIC_DISTRIBUTOR.base)
        self._set_redistributor_address(GIC_REDISTRIBUTOR.base)

        self._created = True
        print(f"GIC created: Distributor @ 0x{GIC_DISTRIBUTOR.base:08x}, "
              f"Redistributor @ 0x{GIC_REDISTRIBUTOR.base:08x}")

    def finalize(self) -> None:
        """
        Finalize the GIC after vCPU creation.

        IMPORTANT: This must be called AFTER all vCPUs are created!

        The GIC needs to know about all vCPUs to set up the per-CPU
        redistributors. If you call this before creating vCPUs, or
        create vCPUs after calling this, things will break.

        Raises:
            GICError: If create() wasn't called first, or if finalization fails.
        """
        if self._finalized:
            return

        if not self._created:
            raise GICError("Must call create() before finalize()")

        # Initialize the GIC - this finalizes the configuration
        self._init_device()

        self._finalized = True
        print("GIC finalized")

    def _create_device(self) -> None:
        """Create the in-kernel GIC device."""
        # Allocate the structure
        device = ffi.new("struct kvm_create_device *")
        device.type = KVM_DEV_TYPE_ARM_VGIC_V3
        device.fd = 0      # Output: kernel fills this in
        device.flags = 0   # No special flags

        # Ask KVM to create the device
        result = lib.ioctl(self._vm_fd, KVM_CREATE_DEVICE, device)
        if result < 0:
            errno = get_errno()
            if errno == 19:  # ENODEV - device type not supported
                raise GICError(
                    "GICv3 not supported. Is this an ARM64 system with KVM?"
                )
            raise GICError(f"Failed to create GIC device: errno {errno}")

        # Save the device file descriptor
        # We'll use this for all subsequent GIC configuration
        self._fd = device.fd

    def _set_distributor_address(self, address: int) -> None:
        """
        Set the Distributor base address.

        The Distributor is the central component that receives all interrupts
        and routes them to the appropriate CPU. There's one per system.

        Args:
            address: Guest physical address for the Distributor (64KB region).
        """
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_DIST, address)

    def _set_redistributor_address(self, address: int) -> None:
        """
        Set the Redistributor base address.

        The Redistributor handles per-CPU interrupts. Each vCPU gets its own
        Redistributor region (128KB per CPU for GICv3).

        Args:
            address: Guest physical address for Redistributors.
        """
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_REDIST, address)

    def _set_address(self, addr_type: int, address: int) -> None:
        """
        Set a GIC component address using KVM_SET_DEVICE_ATTR.

        This is the key-value interface for device configuration:
        - group = KVM_DEV_ARM_VGIC_GRP_ADDR (we're setting addresses)
        - attr = which address (Distributor or Redistributor)
        - addr = pointer to the actual address value

        Args:
            addr_type: KVM_VGIC_V3_ADDR_TYPE_DIST or KVM_VGIC_V3_ADDR_TYPE_REDIST
            address: The guest physical address to set.
        """
        # Create a pointer to hold the address value
        # The API wants a pointer, not the value directly
        addr_ptr = ffi.new("uint64_t *")
        addr_ptr[0] = address

        # Set up the attribute structure
        attr = ffi.new("struct kvm_device_attr *")
        attr.flags = 0
        attr.group = KVM_DEV_ARM_VGIC_GRP_ADDR  # "I'm setting an address"
        attr.attr = addr_type                    # "Specifically, this component"
        attr.addr = int(ffi.cast("uintptr_t", addr_ptr))  # "Here's the value"

        # Tell the kernel
        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            component = "Distributor" if addr_type == KVM_VGIC_V3_ADDR_TYPE_DIST else "Redistributor"
            raise GICError(
                f"Failed to set {component} address to 0x{address:08x}: "
                f"errno {get_errno()}"
            )

    def _init_device(self) -> None:
        """
        Initialize the GIC after configuration.

        This tells KVM: "I'm done configuring, finalize the GIC."
        After this call, the GIC is ready to route interrupts.
        """
        attr = ffi.new("struct kvm_device_attr *")
        attr.flags = 0
        attr.group = KVM_DEV_ARM_VGIC_GRP_CTRL  # "Control operation"
        attr.attr = KVM_DEV_ARM_VGIC_CTRL_INIT  # "Initialize"
        attr.addr = 0  # No value needed for init

        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            raise GICError(f"Failed to initialize GIC: errno {get_errno()}")

    def inject_irq(self, irq: int, level: bool = True) -> None:
        """
        Inject an interrupt into the guest.

        This asserts or deasserts an interrupt line. For level-triggered
        interrupts (most common), you must deassert when the condition clears.

        Interrupt number ranges (GIC interrupt IDs):
        - 0-15: SGIs (Software Generated Interrupts) - CPU-to-CPU signaling
        - 16-31: PPIs (Private Peripheral Interrupts) - per-CPU (e.g., timer)
        - 32+: SPIs (Shared Peripheral Interrupts) - external devices

        For devices, use the full GIC interrupt ID: UART is SPI 1 = IRQ 33.

        NOTE: KVM_IRQ_LINE expects the GSI (SPI number), not the full GIC ID.
        This method handles the conversion: pass the GIC interrupt ID (e.g., 33)
        and we'll convert it to the GSI (e.g., 1) for KVM.

        Args:
            irq: The GIC interrupt number (for SPIs: 32 + SPI_number).
            level: True to assert (raise), False to deassert (lower).

        Example:
            # UART has data, signal the guest (IRQ 33 = SPI 1)
            gic.inject_irq(33, level=True)

            # Guest read the data, clear the interrupt
            gic.inject_irq(33, level=False)
        """
        if not self._finalized:
            raise GICError("GIC not finalized - call finalize() first")

        # ARM KVM_IRQ_LINE uses a specific bit encoding:
        #   bits 31-24: irq_type (0=SPI via routing, 1=SPI via GIC, 2=PPI)
        #   bits 23-16: vcpu_index (ignored for SPIs)
        #   bits 15-0:  irq_id (the actual interrupt number)
        #
        # For SPIs (irq >= 32): irq_type=1, irq_id=irq
        # For PPIs (16-31): irq_type=2, irq_id=irq, vcpu_index matters
        KVM_ARM_IRQ_TYPE_SPI = 1
        KVM_ARM_IRQ_TYPE_PPI = 2

        if irq >= 32:
            # SPI - Shared Peripheral Interrupt
            encoded_irq = (KVM_ARM_IRQ_TYPE_SPI << 24) | irq
        elif irq >= 16:
            # PPI - Private Peripheral Interrupt (per-CPU)
            encoded_irq = (KVM_ARM_IRQ_TYPE_PPI << 24) | irq
        else:
            # SGI - not typically used via KVM_IRQ_LINE
            encoded_irq = irq

        irq_level = ffi.new("struct kvm_irq_level *")
        irq_level.irq = encoded_irq
        irq_level.level = 1 if level else 0

        result = lib.ioctl(self._vm_fd, KVM_IRQ_LINE, irq_level)
        if result < 0:
            action = "assert" if level else "deassert"
            raise GICError(f"Failed to {action} IRQ {irq} (encoded 0x{encoded_irq:08x}): errno {get_errno()}")

    @property
    def fd(self) -> int:
        """Get the GIC device file descriptor."""
        return self._fd

    @property
    def created(self) -> bool:
        """Check if the GIC device has been created."""
        return self._created

    @property
    def finalized(self) -> bool:
        """Check if the GIC has been finalized (ready for use)."""
        return self._finalized

    def close(self) -> None:
        """Close the GIC device file descriptor."""
        if self._fd >= 0:
            lib.close(self._fd)
            self._fd = -1
            self._created = False
            self._finalized = False
