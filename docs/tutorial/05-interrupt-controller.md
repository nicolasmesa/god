# Chapter 5: The Interrupt Controller (GIC)

In this chapter, we'll set up the interrupt controller so our guest can handle asynchronous events like timer ticks and keyboard input.

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

We'll use **GICv3** because it's what modern ARM systems use and KVM supports it well.

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

**3. CPU Interface**
- In GICv3, this uses system registers (not MMIO)
- The CPU reads/writes ICC_* registers
- Acknowledges interrupts, signals end-of-interrupt

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

## Interrupt Types

### SGIs (Software Generated Interrupts) - 0-15

**SGIs** are triggered by software, used for inter-processor communication. One CPU can send an SGI to another to get its attention.

### PPIs (Private Peripheral Interrupts) - 16-31

**PPIs** are per-CPU interrupts. Each CPU has its own instance of these. The timer is a common PPI.

Common PPIs:
- 27: Virtual timer (non-secure EL1)
- 30: Physical timer (non-secure EL1)

### SPIs (Shared Peripheral Interrupts) - 32+

**SPIs** are shared across CPUs. External devices use SPIs. The Distributor routes them to one (or more) CPUs.

Our devices use SPIs:
- 33: UART (SPI 1)
- 48-55: Virtio devices (SPI 16-23)

## Why KVM Emulates the GIC

### Timing Sensitivity

Interrupt controllers need precise timing. An interrupt must be:
1. Detected quickly
2. Routed to the right CPU
3. Acknowledged by software
4. Completed correctly

If any step is slow or wrong, the system may hang or lose interrupts.

### In-Kernel Emulation

KVM provides **in-kernel GIC emulation**. The GIC runs in kernel space, minimizing latency. We just:
1. Create the GIC device
2. Configure its addresses
3. Tell it when to inject interrupts

KVM handles the complex emulation internally.

## Implementation: Setting Up GICv3

### Creating the GIC Device

We use `KVM_CREATE_DEVICE` to create an in-kernel GIC:

```python
# Create the GIC device
device = ffi.new("struct kvm_create_device *")
device.type = KVM_DEV_TYPE_ARM_VGIC_V3
device.fd = 0
device.flags = 0

result = ioctl(vm_fd, KVM_CREATE_DEVICE, device)
gic_fd = device.fd
```

### Configuring Addresses

We set the Distributor and Redistributor addresses:

```python
# Set Distributor address
attr = ffi.new("struct kvm_device_attr *")
attr.group = KVM_DEV_ARM_VGIC_GRP_ADDR
attr.attr = KVM_VGIC_V3_ADDR_TYPE_DIST
attr.addr = ffi.cast("uint64_t", addr_ptr)
addr_ptr[0] = GIC_DIST_BASE

ioctl(gic_fd, KVM_SET_DEVICE_ATTR, attr)

# Set Redistributor address
attr.attr = KVM_VGIC_V3_ADDR_TYPE_REDIST
addr_ptr[0] = GIC_REDIST_BASE
ioctl(gic_fd, KVM_SET_DEVICE_ATTR, attr)
```

### Initializing the GIC

After configuring, we initialize:

```python
attr.group = KVM_DEV_ARM_VGIC_GRP_CTRL
attr.attr = KVM_DEV_ARM_VGIC_CTRL_INIT
ioctl(gic_fd, KVM_SET_DEVICE_ATTR, attr)
```

### Important: GIC Must Be Created Before vCPU

The GIC must be created and initialized **before** the vCPU starts running. Otherwise, the guest can't receive interrupts properly.

## Implementation Code

Create `src/god/devices/gic.py`:

```python
"""
GIC (Generic Interrupt Controller) setup.

We use KVM's in-kernel GICv3 emulation. This module handles creating
and configuring the GIC.
"""

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import (
    KVM_CREATE_DEVICE,
    KVM_SET_DEVICE_ATTR,
)
from god.vm.layout import GIC_DISTRIBUTOR, GIC_REDISTRIBUTOR


# GIC device type
KVM_DEV_TYPE_ARM_VGIC_V3 = 5

# Device attribute groups
KVM_DEV_ARM_VGIC_GRP_ADDR = 0
KVM_DEV_ARM_VGIC_GRP_CTRL = 4

# Address types
KVM_VGIC_V3_ADDR_TYPE_DIST = 0
KVM_VGIC_V3_ADDR_TYPE_REDIST = 1

# Control attributes
KVM_DEV_ARM_VGIC_CTRL_INIT = 0


class GICError(Exception):
    """Exception raised when GIC operations fail."""
    pass


class GIC:
    """
    Manages the GIC (interrupt controller).

    This sets up KVM's in-kernel GICv3 emulation.

    Usage:
        gic = GIC(vm_fd)
        gic.create()
        # Then create vCPUs
    """

    def __init__(self, vm_fd: int, num_cpus: int = 1):
        """
        Create a GIC manager.

        Args:
            vm_fd: The VM file descriptor.
            num_cpus: Number of CPUs (affects redistributor size).
        """
        self._vm_fd = vm_fd
        self._num_cpus = num_cpus
        self._fd = -1
        self._initialized = False

    def create(self):
        """
        Create and initialize the GIC.

        Must be called before creating vCPUs.
        """
        if self._initialized:
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

        # Set Distributor address
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_DIST, GIC_DISTRIBUTOR.base)

        # Set Redistributor address
        self._set_address(KVM_VGIC_V3_ADDR_TYPE_REDIST, GIC_REDISTRIBUTOR.base)

        # Initialize the GIC
        self._init()

        self._initialized = True
        print(f"GIC created: Distributor at 0x{GIC_DISTRIBUTOR.base:08x}, "
              f"Redistributor at 0x{GIC_REDISTRIBUTOR.base:08x}")

    def _set_address(self, addr_type: int, address: int):
        """Set a GIC address."""
        addr_ptr = ffi.new("uint64_t *")
        addr_ptr[0] = address

        attr = ffi.new("struct kvm_device_attr *")
        attr.group = KVM_DEV_ARM_VGIC_GRP_ADDR
        attr.attr = addr_type
        attr.addr = int(ffi.cast("uintptr_t", addr_ptr))

        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            raise GICError(f"Failed to set GIC address: errno {get_errno()}")

    def _init(self):
        """Initialize the GIC after configuration."""
        attr = ffi.new("struct kvm_device_attr *")
        attr.group = KVM_DEV_ARM_VGIC_GRP_CTRL
        attr.attr = KVM_DEV_ARM_VGIC_CTRL_INIT
        attr.addr = 0

        result = lib.ioctl(self._fd, KVM_SET_DEVICE_ATTR, attr)
        if result < 0:
            raise GICError(f"Failed to initialize GIC: errno {get_errno()}")

    @property
    def fd(self) -> int:
        """Get the GIC device file descriptor."""
        return self._fd
```

## Injecting Interrupts

To send an interrupt to the guest, we use `KVM_IRQ_LINE`:

```python
def inject_interrupt(vm_fd: int, irq: int, level: bool):
    """
    Inject an interrupt into the guest.

    Args:
        vm_fd: The VM file descriptor.
        irq: The interrupt number (SPI number + 32).
        level: True to assert, False to deassert.
    """
    irq_level = ffi.new("struct kvm_irq_level *")
    irq_level.irq = irq
    irq_level.level = 1 if level else 0

    result = lib.ioctl(vm_fd, KVM_IRQ_LINE, irq_level)
    if result < 0:
        raise GICError(f"Failed to inject interrupt: errno {get_errno()}")
```

## Testing Interrupts

With the GIC set up, our UART can now generate interrupts when data is received. The timer will also work properly.

## Gotchas

### GIC Must Be Created First

The GIC must be created before vCPUs. If you create vCPUs first, GIC initialization will fail.

### Interrupt Numbers

- SGIs: 0-15
- PPIs: 16-31
- SPIs: 32+ (this is what devices use)

When referencing a "SPI 1" (UART), use interrupt number 33 (32 + 1).

### Level vs. Edge Triggered

Most interrupts are **level-triggered**: they stay asserted until the condition is cleared. Edge-triggered interrupts pulse once.

For level-triggered, you must deassert when the condition clears, or the guest may see spurious interrupts.

## What's Next?

In this chapter, we:

1. Learned why interrupts are essential
2. Understood GIC architecture
3. Set up the GICv3 with KVM
4. Learned how to inject interrupts

In the next chapter, we'll set up the ARM timer. This is essential for Linux boot—without a working timer, the kernel can't schedule processes or track time.

[Continue to Chapter 6: The ARM Timer →](06-timer.md)
