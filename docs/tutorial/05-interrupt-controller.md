# Chapter 5: The Interrupt Controller (GIC)

In this chapter, we'll set up the interrupt controller so our guest can handle asynchronous events like timer ticks and serial input.

## Why Interrupts Matter

### The Problem with Polling

Without interrupts, software must constantly check if events have occurred:

```c
// Polling - wasteful busy loop
while (1) {
    if (uart_has_data()) {
        process_uart();
    }
    if (timer_expired()) {
        handle_timer();
    }
    // CPU spins constantly, wasting power and time
}
```

This is inefficient—the CPU is always busy even when there's nothing to do.

### Interrupts to the Rescue

With interrupts, hardware signals the CPU when attention is needed:

```c
// Interrupt-driven - efficient
void uart_irq_handler() {
    process_uart();
}

void timer_irq_handler() {
    handle_timer();
}

// Main loop can sleep or do other work
while (1) {
    wait_for_interrupt();  // CPU sleeps until interrupt
}
```

The CPU can sleep, saving power, and wakes instantly when an event occurs.

## ARM's Generic Interrupt Controller (GIC)

### What is the GIC?

The **Generic Interrupt Controller** is ARM's standard way of handling interrupts. It routes interrupt signals from devices to CPU cores.

### GIC Versions

There are several GIC versions:
- **GICv2**: Older, simpler, memory-mapped CPU interface
- **GICv3**: Modern, supports more CPUs, system register CPU interface
- **GICv4**: Adds direct interrupt injection for VMs

We use **GICv3** for several reasons:
1. It's what modern ARM systems use
2. KVM supports it well
3. GICv4's direct injection feature is for passthrough devices (giving a VM direct access to real hardware), which we don't need since all our devices are emulated anyway

### GIC Components

GICv3 has three main parts:

**1. Distributor (GICD)**
- Central component, one per system
- Receives all interrupt signals
- Routes interrupts to the appropriate CPU
- Controls interrupt enable/disable and priority

**2. Redistributor (GICR)**
- One per CPU core
- Handles per-CPU interrupts (SGIs and PPIs)
- Forwards shared interrupts from Distributor

**3. CPU Interface (ICC_* System Registers)**

In GICv3, the CPU interface uses **system registers** rather than memory-mapped I/O. These are the ICC_* (Interrupt Controller CPU interface) registers:

| Register | Purpose |
|----------|---------|
| `ICC_IAR1_EL1` | Interrupt Acknowledge - read to get pending interrupt ID |
| `ICC_EOIR1_EL1` | End of Interrupt - write to signal handling complete |
| `ICC_PMR_EL1` | Priority Mask - set minimum priority to accept |
| `ICC_SRE_EL1` | System Register Enable - enables the register interface |

The guest accesses these with `MRS`/`MSR` instructions. KVM traps and emulates them.

```
┌─────────────────────────────────────────────────────────────────┐
│                         Devices                                  │
│    UART        Timer        Virtio       Other                  │
│      │           │            │            │                     │
│      └───────────┴────────────┴────────────┘                     │
│                       │                                          │
│                       ▼                                          │
│              ┌─────────────────┐                                 │
│              │   Distributor   │  (Routes interrupts)            │
│              │     (GICD)      │                                 │
│              └────────┬────────┘                                 │
│                       │                                          │
│           ┌───────────┴───────────┐                              │
│           ▼                       ▼                              │
│   ┌──────────────┐       ┌──────────────┐                       │
│   │Redistributor │       │Redistributor │  (Per-CPU)            │
│   │   (GICR 0)   │       │   (GICR 1)   │                       │
│   └──────┬───────┘       └──────┬───────┘                       │
│          ▼                      ▼                                │
│   ┌──────────────┐       ┌──────────────┐                       │
│   │     CPU 0    │       │     CPU 1    │                       │
│   │ (ICC_* regs) │       │ (ICC_* regs) │                       │
│   └──────────────┘       └──────────────┘                       │
└─────────────────────────────────────────────────────────────────┘
```

## Interrupt Types and Numbering

ARM organizes interrupts into three ranges:

### SGIs (Software Generated Interrupts) - 0-15

**SGIs** are triggered by software, used for inter-processor communication. One CPU can send an SGI to another to get its attention.

### PPIs (Private Peripheral Interrupts) - 16-31

**PPIs** are per-CPU interrupts. Each CPU has its own instance of these.

Common PPIs:
- 27: Virtual timer (we'll use this in the next chapter)
- 30: Physical timer

### SPIs (Shared Peripheral Interrupts) - 32+

**SPIs** are shared across CPUs. External devices use SPIs. The Distributor routes them to one (or more) CPUs.

Our devices use SPIs:
- 33: UART (SPI 1, meaning 32 + 1)
- 48-55: Virtio devices (SPI 16-23)

**Why the +32?** When documentation refers to "SPI 1", that's the SPI number within the SPI range. To get the actual interrupt number used with KVM, add 32 (the start of the SPI range). So UART at "SPI 1" = interrupt 33.

## In-Kernel vs User-Space Device Emulation

You might wonder: why do we use `KVM_CREATE_DEVICE` for the GIC, but we emulated the UART ourselves with MMIO exits?

The difference is **where the emulation runs**:

| Device | Emulation | Why |
|--------|-----------|-----|
| **GIC** | In-kernel (KVM) | Timing-critical. Interrupt latency affects system stability. |
| **UART** | User-space (our code) | Timing-tolerant. A few microseconds delay in serial output doesn't matter. |

`KVM_CREATE_DEVICE` creates in-kernel devices that KVM emulates itself:
- No VM exit needed for many operations
- Lower latency
- Kernel can optimize internal paths

Our UART uses MMIO exits: guest accesses `0x09000000`, KVM exits to us, we handle it, return. This adds latency but gives us flexibility.

The GIC *must* be in-kernel because:
1. Interrupt delivery timing is critical
2. The GIC interacts directly with vCPU state
3. The kernel needs to inject interrupts during `KVM_RUN`

## Implementation: Setting Up GICv3

### The Initialization Sequence

The GIC has a specific initialization order:

1. Create the GIC device and set addresses (before vCPUs)
2. Create **all** vCPU(s)
3. Finalize the GIC (after all vCPUs)

The GIC must be created before vCPUs, but finalized after all vCPUs exist. This is because finalization sets up per-CPU redistributors for each vCPU.

VMRunner handles this automatically:
- GIC is created in the VMRunner constructor
- GIC is finalized when `run()` is called

### Adding KVM Structures

First, we need to add the C structures for GIC operations to `src/god/kvm/bindings.py`:

```python
# Add to ffi.cdef():

    // =========================================================================
    // In-kernel device structures (GIC, etc.)
    // =========================================================================

    // Structure for creating an in-kernel device (KVM_CREATE_DEVICE)
    struct kvm_create_device {
        uint32_t type;   // Input: device type (e.g., KVM_DEV_TYPE_ARM_VGIC_V3)
        uint32_t fd;     // Output: file descriptor for the created device
        uint32_t flags;  // Input: creation flags
    };

    // Structure for getting/setting device attributes (KVM_SET_DEVICE_ATTR)
    struct kvm_device_attr {
        uint32_t flags;  // Flags (currently unused, set to 0)
        uint32_t group;  // Attribute group (namespace)
        uint64_t attr;   // Specific attribute within the group
        uint64_t addr;   // Pointer to the value
    };

    // Structure for injecting interrupts (KVM_IRQ_LINE)
    struct kvm_irq_level {
        uint32_t irq;    // Interrupt number
        uint32_t level;  // 1 = assert, 0 = deassert
    };
```

### Adding KVM Constants

Add the GIC-related constants to `src/god/kvm/constants.py`:

```python
# VM ioctls
KVM_IRQ_LINE = _IOW(KVMIO, 0x61, 8)

# Device ioctls (on device fd from KVM_CREATE_DEVICE)
KVM_SET_DEVICE_ATTR = _IOW(KVMIO, 0xE1, 24)

# Device types
KVM_DEV_TYPE_ARM_VGIC_V3 = 7

# GIC attribute groups
KVM_DEV_ARM_VGIC_GRP_ADDR = 0
KVM_DEV_ARM_VGIC_GRP_CTRL = 4

# GIC address types
KVM_VGIC_V3_ADDR_TYPE_DIST = 2
KVM_VGIC_V3_ADDR_TYPE_REDIST = 3

# GIC control attributes
KVM_DEV_ARM_VGIC_CTRL_INIT = 0
```

### Creating the GIC Device

We use `KVM_CREATE_DEVICE` to create an in-kernel GIC:

```python
# Create the GIC device
device = ffi.new("struct kvm_create_device *")
device.type = KVM_DEV_TYPE_ARM_VGIC_V3  # Request GICv3
device.fd = 0      # Output: kernel fills this in
device.flags = 0   # No special flags

result = ioctl(vm_fd, KVM_CREATE_DEVICE, device)
gic_fd = device.fd  # File descriptor for subsequent operations
```

The kernel has built-in knowledge of device types. When you pass `KVM_DEV_TYPE_ARM_VGIC_V3`, it creates the GIC emulation infrastructure and returns a file descriptor to control it.

### Configuring Addresses

We use a key-value interface (`kvm_device_attr`) to configure the GIC. Think of it as:

```
device.set(group="addresses", key="distributor", value=0x08000000)
device.set(group="addresses", key="redistributor", value=0x080a0000)
```

In code:

```python
def set_gic_address(gic_fd, addr_type, address):
    # Create a pointer to hold the address value
    addr_ptr = ffi.new("uint64_t *")
    addr_ptr[0] = address

    # Set up the attribute structure
    attr = ffi.new("struct kvm_device_attr *")
    attr.flags = 0
    attr.group = KVM_DEV_ARM_VGIC_GRP_ADDR  # "I'm setting an address"
    attr.attr = addr_type                    # "Which component"
    attr.addr = int(ffi.cast("uintptr_t", addr_ptr))  # Pointer to value

    ioctl(gic_fd, KVM_SET_DEVICE_ATTR, attr)

# Set Distributor address (0x08000000)
set_gic_address(gic_fd, KVM_VGIC_V3_ADDR_TYPE_DIST, 0x08000000)

# Set Redistributor address (0x080a0000)
set_gic_address(gic_fd, KVM_VGIC_V3_ADDR_TYPE_REDIST, 0x080a0000)
```

The naming is a bit confusing: `attr.addr` is "the address of the data" (a pointer), not "the address we're setting" (the value).

### Finalizing the GIC

After creating vCPUs, we finalize the GIC:

```python
attr = ffi.new("struct kvm_device_attr *")
attr.flags = 0
attr.group = KVM_DEV_ARM_VGIC_GRP_CTRL  # "Control operation"
attr.attr = KVM_DEV_ARM_VGIC_CTRL_INIT  # "Initialize/finalize"
attr.addr = 0  # No value needed

ioctl(gic_fd, KVM_SET_DEVICE_ATTR, attr)
```

This tells KVM: "I'm done configuring, finalize the GIC." After this, interrupts can be delivered.

## Injecting Interrupts

To send an interrupt to the guest, we use `KVM_IRQ_LINE`:

```python
def inject_irq(vm_fd, irq, level):
    """
    Inject an interrupt into the guest.

    Args:
        vm_fd: The VM file descriptor
        irq: The interrupt number (SPI number + 32 for devices)
        level: True to assert (raise), False to deassert (lower)
    """
    irq_level = ffi.new("struct kvm_irq_level *")
    irq_level.irq = irq
    irq_level.level = 1 if level else 0

    ioctl(vm_fd, KVM_IRQ_LINE, irq_level)
```

### Assert and Deassert

These are interrupt signal terms:

| Term | Meaning | Analogy |
|------|---------|---------|
| **Assert** | Activate the interrupt signal | Raising your hand |
| **Deassert** | Deactivate the interrupt signal | Lowering your hand |

For **level-triggered** interrupts (most common), the signal stays asserted as long as the condition exists:

```python
# UART received data
inject_irq(vm_fd, 33, level=True)   # Assert: "Hey CPU, I have data!"

# Guest read the data, UART buffer is now empty
inject_irq(vm_fd, 33, level=False)  # Deassert: "Handled, nothing more"
```

If you forget to deassert, the CPU keeps seeing the interrupt as pending and may get stuck in an interrupt loop.

## Implementation Code

Create `src/god/devices/gic.py`:

```python
"""
GIC (Generic Interrupt Controller) setup.

The GIC is ARM's standard interrupt controller. It routes interrupt signals
from devices to CPU cores. We use KVM's in-kernel GICv3 emulation.

The initialization sequence is:
1. Create GIC and set addresses (create())
2. Create all vCPU(s)
3. Finalize GIC (finalize())

VMRunner handles this automatically - finalize() is called when run() starts.
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

    Usage (with VMRunner - recommended):
        runner = VMRunner(vm, kvm)  # GIC created here
        runner.create_vcpu()
        runner.create_vcpu()
        runner.run()  # GIC finalized here
        runner.gic.inject_irq(33)

    Usage (manual):
        gic = GIC(vm.fd)
        gic.create()          # Before vCPUs
        vcpu0 = VCPU(...)
        vcpu1 = VCPU(...)
        gic.finalize()        # After all vCPUs
        gic.inject_irq(33)
    """

    def __init__(self, vm_fd: int, num_cpus: int = 1):
        self._vm_fd = vm_fd
        self._num_cpus = num_cpus
        self._fd = -1
        self._created = False
        self._finalized = False

    def create(self) -> None:
        """Create the GIC device and set addresses."""
        if self._created:
            return

        # Create the GIC device
        device = ffi.new("struct kvm_create_device *")
        device.type = KVM_DEV_TYPE_ARM_VGIC_V3
        device.fd = 0
        device.flags = 0

        result = lib.ioctl(self._vm_fd, KVM_CREATE_DEVICE, device)
        if result < 0:
            raise GICError(f"Failed to create GIC: errno {get_errno()}")

        self._fd = device.fd

        # Set addresses
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_DIST, GIC_DISTRIBUTOR.base)
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_REDIST, GIC_REDISTRIBUTOR.base)

        self._created = True

    def finalize(self) -> None:
        """
        Finalize the GIC after all vCPUs are created.

        This sets up per-CPU redistributors. Call this only after
        creating all vCPUs you need.
        """
        if self._finalized:
            return
        if not self._created:
            raise GICError("Must call create() before finalize()")

        attr = ffi.new("struct kvm_device_attr *")
        attr.flags = 0
        attr.group = KVM_DEV_ARM_VGIC_GRP_CTRL
        attr.attr = KVM_DEV_ARM_VGIC_CTRL_INIT
        attr.addr = 0

        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            raise GICError(f"Failed to finalize GIC: errno {get_errno()}")

        self._finalized = True

    def _set_address(self, addr_type: int, address: int) -> None:
        """Set a GIC component address."""
        addr_ptr = ffi.new("uint64_t *")
        addr_ptr[0] = address

        attr = ffi.new("struct kvm_device_attr *")
        attr.flags = 0
        attr.group = KVM_DEV_ARM_VGIC_GRP_ADDR
        attr.attr = addr_type
        attr.addr = int(ffi.cast("uintptr_t", addr_ptr))

        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            raise GICError(f"Failed to set GIC address: errno {get_errno()}")

    def inject_irq(self, irq: int, level: bool = True) -> None:
        """
        Inject an interrupt into the guest.

        Args:
            irq: Interrupt number (SPI number + 32 for devices)
            level: True to assert, False to deassert
        """
        if not self._finalized:
            raise GICError("GIC not finalized")

        irq_level = ffi.new("struct kvm_irq_level *")
        irq_level.irq = irq
        irq_level.level = 1 if level else 0

        result = lib.ioctl(self._vm_fd, KVM_IRQ_LINE, irq_level)
        if result < 0:
            raise GICError(f"Failed to inject IRQ {irq}: errno {get_errno()}")

    @property
    def finalized(self) -> bool:
        """Check if the GIC has been finalized."""
        return self._finalized
```

## Integration with VMRunner

Now we need to integrate the GIC into VMRunner. The initialization sequence is:

1. **In `__init__()`**: Create GIC device and set addresses (before vCPUs)
2. **`create_vcpu()` calls**: Create vCPUs (GIC already exists)
3. **On `run()`**: Finalize the GIC (after all vCPUs exist)

### Update the Import

In `src/god/vcpu/runner.py`, update the import from devices:

```python
from god.devices import DeviceRegistry, MMIOAccess, GIC
```

### Update `__init__()`

Add GIC creation to the VMRunner constructor. Add the `create_gic` parameter and create the GIC at the end of `__init__()`:

```python
def __init__(
    self,
    vm: VirtualMachine,
    kvm: KVMSystem,
    devices: DeviceRegistry | None = None,
    create_gic: bool = True,  # Add this parameter
):
    """
    Create a runner for a VM.

    This sets up the VM infrastructure including the GIC (interrupt
    controller). The GIC must exist before vCPUs can be created.

    Args:
        vm: The VirtualMachine to run.
        kvm: The KVMSystem instance.
        devices: Device registry for MMIO handling. If not provided,
                 a new empty registry is created.
        create_gic: If True (default), create the GIC automatically.
                    Set to False only if you want to manage the GIC
                    yourself (rare).
    """
    self._vm = vm
    self._kvm = kvm
    self._devices = devices if devices is not None else DeviceRegistry()
    self._vcpus: list[VCPU] = []
    self._gic: GIC | None = None

    # Create the GIC (interrupt controller)
    # This must happen before any vCPUs are created.
    if create_gic:
        self._gic = GIC(self._vm.fd)
        self._gic.create()
```

### Add the `gic` Property

Add a property to access the GIC:

```python
@property
def gic(self) -> GIC | None:
    """Get the GIC (interrupt controller)."""
    return self._gic
```

### Update `run()` to Finalize the GIC

At the start of the `run()` method, before the run loop begins, add GIC finalization:

```python
def run(self, max_exits: int = 100000, quiet: bool = False) -> dict:
    """..."""
    if not self._vcpus:
        raise RunnerError("No vCPUs created - call create_vcpu() first")

    # Finalize GIC before running
    # This must happen after all vCPUs are created so the GIC
    # can set up per-CPU redistributors for each one.
    if self._gic is not None and not self._gic.finalized:
        self._gic.finalize()

    # ... rest of run loop unchanged
```

### Update the devices `__init__.py`

Export the GIC class from `src/god/devices/__init__.py`:

```python
from .gic import GIC, GICError
```

### Usage Example

With these changes, VMRunner handles the GIC automatically:

```python
runner = VMRunner(vm, kvm)  # GIC created here
vcpu0 = runner.create_vcpu()
vcpu1 = runner.create_vcpu()

vcpu0.set_pc(entry_point)

runner.run()  # GIC finalized here

# Inject interrupts
runner.gic.inject_irq(33, level=True)
```

## What's Next?

In this chapter, we:

1. Learned why interrupts are essential for efficient I/O
2. Understood GIC architecture (Distributor, Redistributor, CPU Interface)
3. Set up the GICv3 with the correct initialization sequence
4. Learned how to inject interrupts with assert/deassert

With the GIC in place, the guest can now receive asynchronous notifications. In the next chapter, we'll set up the ARM timer. This is essential for Linux boot—without a working timer, the kernel can't schedule processes or track time.

[Continue to Chapter 6: The ARM Timer →](06-timer.md)
