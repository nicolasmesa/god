# Chapter 6: The ARM Timer

In this chapter, we'll set up the ARM timer so our guest can track time and schedule tasks. This is essential for booting Linux.

## Why Timers Matter

Every operating system needs a timer for:

1. **Process Scheduling**: Switch between processes at regular intervals
2. **Timeouts**: Know when something took too long
3. **Sleep Functions**: `sleep(1)` needs to wake up after 1 second
4. **Timekeeping**: Track wall-clock time (date and time)

Without a working timer, the Linux kernel **cannot boot**. It hangs waiting for the timer during early initialization.

## ARM Generic Timer

### Overview

ARM64 CPUs have a built-in timer system called the **ARM Generic Timer**. It provides:

- A continuously incrementing counter
- Programmable compare values
- Interrupt generation when counter reaches compare value

### Timer Registers

The timer is accessed through system registers (not MMIO):

**Counter Registers:**
- `CNTPCT_EL0`: Physical counter value (always incrementing)
- `CNTVCT_EL0`: Virtual counter value

**Timer Control:**
- `CNTP_CTL_EL0`: Physical timer control (enable, mask)
- `CNTP_TVAL_EL0`: Physical timer value (countdown)
- `CNTP_CVAL_EL0`: Physical timer compare value

**Frequency:**
- `CNTFRQ_EL0`: Counter frequency in Hz

### How the Timer Works

1. The counter (`CNTPCT_EL0`) increments continuously at a fixed frequency
2. Software sets a compare value (`CNTP_CVAL_EL0`) for when to fire
3. When counter reaches compare, a timer interrupt fires
4. Software handles the interrupt and sets the next compare value

```
Counter:    0 ... 1000 ... 2000 ... 3000 ...
                        ↑
                   Compare = 2500
                        │
                   Interrupt fires!
```

## Physical vs. Virtual Timers

ARM provides two timers:

**Physical Timer (CNTP_*):**
- Uses the physical counter
- Typically used when not virtualizing

**Virtual Timer (CNTV_*):**
- Uses the virtual counter
- The virtual counter can be offset from physical
- Useful for VM migration (adjust offset to account for time paused)

For simplicity, we'll use the physical timer, but Linux typically uses the virtual timer in VMs.

## Timer Interrupts

The timer generates **PPI (Private Peripheral Interrupt)** interrupts:

| Timer | Non-Secure | Secure |
|-------|-----------|--------|
| Physical | PPI 30 (IRQ 30) | PPI 29 |
| Virtual | PPI 27 (IRQ 27) | PPI 26 |

These go to the GIC as PPIs, meaning each CPU has its own timer interrupt.

## KVM Timer Emulation

### Why KVM Handles the Timer

Like the GIC, the timer needs precise emulation. KVM handles it in-kernel for accuracy.

KVM provides:
- Emulated counter registers
- Accurate time keeping even when VM is paused
- Proper interrupt generation

### Timer Initialization

The timer mostly works automatically once the GIC is set up. KVM connects the timer interrupts to the GIC.

However, we need to ensure:
1. The GIC is initialized first
2. The vCPU knows about timer IRQs
3. The counter frequency is set

### Setting the Counter Frequency

Linux reads `CNTFRQ_EL0` to know the timer frequency. We should set this to a reasonable value (e.g., 62.5 MHz or 1 GHz).

```python
# Set counter frequency (62.5 MHz is common for ARM VMs)
CNTFRQ = 62500000

# This is done through vCPU registers
vcpu.set_system_register(CNTFRQ_EL0, CNTFRQ)
```

## Implementation

Create `src/god/devices/timer.py`:

```python
"""
ARM Generic Timer configuration.

The timer is mostly emulated by KVM. We just need to configure it properly.
"""

# Timer interrupt numbers (PPIs)
TIMER_PHYS_NONSECURE_IRQ = 30  # Physical timer, non-secure
TIMER_VIRT_IRQ = 27            # Virtual timer

# Common counter frequencies
CNTFRQ_62_5_MHZ = 62_500_000   # 62.5 MHz
CNTFRQ_1_GHZ = 1_000_000_000   # 1 GHz


class TimerConfig:
    """
    Timer configuration.

    This is mostly informational - KVM handles the actual timer emulation.
    We just need to ensure proper configuration.
    """

    def __init__(self, frequency: int = CNTFRQ_62_5_MHZ):
        self.frequency = frequency
        self.phys_irq = TIMER_PHYS_NONSECURE_IRQ
        self.virt_irq = TIMER_VIRT_IRQ

    def get_device_tree_props(self) -> dict:
        """
        Get Device Tree properties for the timer.

        Returns a dict suitable for adding to the Device Tree.
        """
        return {
            "compatible": "arm,armv8-timer",
            # Interrupt format: GIC_PPI, number, flags
            # Flags: 0x04 = level-sensitive, 0x08 = edge-triggered
            "interrupts": [
                # Secure physical timer (PPI 29)
                1, 29, 0x04,
                # Non-secure physical timer (PPI 30)
                1, 30, 0x04,
                # Virtual timer (PPI 27)
                1, 27, 0x04,
                # Hypervisor timer (PPI 26) - not used by guests
                1, 26, 0x04,
            ],
            "always-on": True,
        }
```

## Device Tree Configuration

The timer needs to be described in the Device Tree:

```dts
timer {
    compatible = "arm,armv8-timer";
    interrupts = <GIC_PPI 13 IRQ_TYPE_LEVEL_LOW>,  // Secure phys
                 <GIC_PPI 14 IRQ_TYPE_LEVEL_LOW>,  // Non-secure phys
                 <GIC_PPI 11 IRQ_TYPE_LEVEL_LOW>,  // Virtual
                 <GIC_PPI 10 IRQ_TYPE_LEVEL_LOW>;  // Hypervisor
    always-on;
};
```

Note: The interrupt numbers in Device Tree are offset by 16 from the raw PPI numbers.

## Testing

With the timer configured, Linux should boot past the early timer initialization. We'll see messages like:

```
[    0.000000] arch_timer: cp15 timer(s) running at 62.50MHz (virt).
```

## Gotchas

### Timer Must Have GIC First

The timer generates interrupts through the GIC. If the GIC isn't set up, timer interrupts won't work.

### Counter Frequency

If `CNTFRQ_EL0` is 0 or wrong, Linux may miscalculate timing. Set it to a reasonable value.

### Virtual vs. Physical

Linux typically prefers the virtual timer in VMs. Make sure both are configured even if you expect only one to be used.

## What's Next?

In this chapter, we:

1. Learned why timers are essential
2. Understood ARM's timer architecture
3. Configured the timer for our VM

In the next chapter, we'll implement virtio devices for efficient I/O. This will give us a better console and block storage for a root filesystem.

[Continue to Chapter 7: Virtio Devices →](07-virtio-devices.md)
