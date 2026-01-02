# Chapter 6: The ARM Timer

In this chapter, we'll configure the ARM timer so our guest can track time and schedule tasks. This is essential for booting Linux—without a working timer, the kernel hangs during early initialization.

## Why Timers Matter

### The Heartbeat of an Operating System

Every operating system needs a timer for:

1. **Process Scheduling**: The kernel switches between processes at regular intervals. Without a timer, a single process could monopolize the CPU forever.

2. **Timeouts**: Network connections, disk I/O, and system calls all need to know when something took too long. "Wait up to 5 seconds for a response" requires measuring time.

3. **Sleep Functions**: When you call `sleep(1)`, the kernel needs to wake your process after 1 second. It sets a timer, puts your process to sleep, and the timer interrupt wakes it.

4. **Timekeeping**: The system clock ("what time is it?") advances based on timer ticks. Without a timer, `date` would be frozen.

### What Happens Without a Timer

If the timer isn't configured correctly, Linux hangs early in boot:

```
[    0.000000] Booting Linux on physical CPU 0x0
[    0.000000] Linux version 6.x.x ...
[    0.000000] ...
<hangs here, waiting for timer>
```

The kernel reaches [`arch_timer_of_init()`](https://github.com/torvalds/linux/blob/master/drivers/clocksource/arm_arch_timer.c#L1129) and waits for the timer to respond. It never does. Boot fails.

This function is registered via Device Tree matching:
```c
TIMER_OF_DECLARE(armv8_arch_timer, "arm,armv8-timer", arch_timer_of_init);
```

When Linux parses a Device Tree node with `compatible = "arm,armv8-timer"`, this function is automatically called during early boot to initialize the timer subsystem.

## ARM Generic Timer Architecture

### Overview

ARM64 CPUs have a built-in timer system called the **ARM Generic Timer**. Unlike the UART (which we emulate) or even the GIC (which KVM emulates for us), the timer is tightly integrated with the CPU itself.

The Generic Timer provides:
- A continuously incrementing **counter** (think: stopwatch that never stops)
- Programmable **compare values** (think: alarm clock settings)
- **Interrupt generation** when the counter reaches the compare value

### System Registers, Not MMIO

Here's something important: the timer uses **system registers**, not memory-mapped I/O.

Remember how the UART lives at address `0x09000000`? The guest accesses it with load/store instructions, and KVM traps those accesses so we can emulate them.

The timer is different. It uses special CPU registers accessed via `MRS` (Move to Register from System register) and `MSR` (Move to System register from Register) instructions:

```asm
// Read the current counter value
mrs x0, CNTPCT_EL0    // x0 = physical counter

// Set when the timer should fire
msr CNTP_CVAL_EL0, x1 // Fire when counter reaches x1

// Enable the timer
mov x2, #1
msr CNTP_CTL_EL0, x2  // Enable bit = 1
```

This is why there's no timer entry in our `layout.py`—the timer doesn't occupy any address space. KVM traps access to these system registers and handles them internally.

### The Counter Registers

The timer has two counters that increment continuously at a fixed frequency:

| Register | Name | Description |
|----------|------|-------------|
| `CNTPCT_EL0` | Physical Count | The "real" counter. Increments at hardware frequency. |
| `CNTVCT_EL0` | Virtual Count | `CNTPCT - CNTVOFF`. Can be offset from physical. |

These are **read-only**—you can't set them, only read the current value. They tick upward relentlessly, like a stopwatch that started when the system powered on.

### Counter Frequency

How fast does the counter increment? That depends on the hardware. The frequency is stored in `CNTFRQ_EL0`:

```asm
mrs x0, CNTFRQ_EL0    // x0 = frequency in Hz
```

Common values:
- **24 MHz** (our Lima VM on Apple Silicon)
- **62.5 MHz** (QEMU's virt machine)
- **1 GHz** (ARMv8.6+ standardized this)

At 24 MHz, the counter increments 24 million times per second. Each tick is about 42 nanoseconds.

**Important**: We don't set this frequency—the hardware determines it, and KVM exposes the host's actual frequency to the guest. The guest reads `CNTFRQ_EL0` to learn what it is. If we lied about the frequency, all guest timing would be wrong (`sleep(1)` might take 2 seconds).

### The Timer Registers

The counters tell you "what time is it now." The timer registers let you say "wake me up at time X":

| Register | Name | Purpose |
|----------|------|---------|
| `CNTP_CTL_EL0` | Control | Enable/disable the timer, mask interrupts |
| `CNTP_CVAL_EL0` | Compare Value | Fire when counter reaches this (absolute) |
| `CNTP_TVAL_EL0` | Timer Value | Fire in this many ticks (relative) |

**CVAL vs TVAL—Two Ways to Set an Alarm**

You can set the timer two ways:

**CVAL (Compare Value)** - Absolute time:
```asm
// "Fire when counter reaches 1,000,000"
mov x0, #1000000
msr CNTP_CVAL_EL0, x0
```

**TVAL (Timer Value)** - Relative time:
```asm
// "Fire in 24,000 ticks" (1ms at 24MHz)
mov x0, #24000
msr CNTP_TVAL_EL0, x0
// Internally sets CVAL = CNTPCT + 24000
```

The relationship: `TVAL = CVAL - CNTPCT`

When you write to TVAL, the hardware automatically computes `CVAL = CNTPCT + TVAL`. TVAL then counts down as the counter advances. When TVAL goes from 0 to -1 (i.e., when CNTPCT reaches CVAL), the timer fires.

**When to use which:**
- **TVAL**: Simple one-shot delays. "Fire in N ticks."
- **CVAL**: Periodic timers. After handling an interrupt, just add the period to CVAL for the next one, avoiding drift.

### Timer Operation

Here's how a timer interrupt works:

```
Time →

Counter:     1000 ... 1100 ... 1200 ... 1300 ...
                              ↑
                         CVAL = 1250
                              │
                         Interrupt fires!
```

1. Software writes `CVAL = 1250`
2. Software enables the timer (`CTL.ENABLE = 1`)
3. Counter keeps incrementing
4. When counter reaches 1250, hardware asserts the interrupt line
5. Interrupt handler runs, sets next CVAL, acknowledges interrupt

## Physical vs Virtual Timers

ARM provides **two complete timer systems**: physical and virtual.

### Physical Timer (CNTP_*)

Uses the physical counter (`CNTPCT_EL0`). The counter value is the same across all CPUs and VMs—it's the actual hardware counter.

Registers: `CNTP_CTL_EL0`, `CNTP_CVAL_EL0`, `CNTP_TVAL_EL0`

### Virtual Timer (CNTV_*)

Uses the virtual counter (`CNTVCT_EL0`). The virtual counter equals the physical counter minus an offset:

```
CNTVCT = CNTPCT - CNTVOFF
```

The offset (`CNTVOFF_EL2`) is controlled by the hypervisor (EL2). Guests can't see or modify it.

Registers: `CNTV_CTL_EL0`, `CNTV_CVAL_EL0`, `CNTV_TVAL_EL0`

### Why VMs Prefer the Virtual Timer

Linux running inside a VM typically uses the **virtual timer**. Here's why:

**1. VM Migration**

Imagine a VM running on Host A for 10 seconds, then migrated to Host B. The physical counters on Host A and B aren't synchronized—they started counting at different times.

Without the virtual offset, the guest would see the counter jump (or go backward!) after migration. With the virtual timer, the hypervisor adjusts `CNTVOFF` so the guest sees smooth, continuous time.

**2. VM Pause/Resume**

If you pause a VM for 5 seconds, the physical counter keeps ticking. When you resume, the guest would suddenly see 5 seconds pass instantly.

With the virtual timer, the hypervisor adds 5 seconds to `CNTVOFF`, so from the guest's perspective, no time passed while paused.

**3. Per-VM Time**

Each VM can have its own virtual counter offset. They each see time starting from when *they* booted, not from when the host booted.

**4. Convention**

The physical timer is typically reserved for the hypervisor itself. Giving guests the virtual timer keeps things clean.

### How Linux Chooses the Virtual Timer

You might wonder: how does Linux know to use the virtual timer instead of the physical one? The answer is in [`arch_timer_select_ppi()`](https://github.com/torvalds/linux/blob/master/drivers/clocksource/arm_arch_timer.c#L450):

```c
static enum arch_timer_ppi_nr __init arch_timer_select_ppi(void)
{
    if (is_kernel_in_hyp_mode())
        return ARCH_TIMER_HYP_PPI;      // Running at EL2 → use hypervisor timer

    if (!is_hyp_mode_available() && arch_timer_ppi[ARCH_TIMER_VIRT_PPI])
        return ARCH_TIMER_VIRT_PPI;     // Can't access EL2 → use virtual timer

    if (IS_ENABLED(CONFIG_ARM64))
        return ARCH_TIMER_PHYS_NONSECURE_PPI;  // Bare metal ARM64

    return ARCH_TIMER_PHYS_SECURE_PPI;  // ARM32 secure
}
```

**The logic for a KVM guest:**
1. `is_kernel_in_hyp_mode()` → FALSE (guest runs at EL1, not EL2)
2. `is_hyp_mode_available()` → FALSE (guest can't access hypervisor mode)
3. Virtual timer PPI (27) is present in Device Tree → **use virtual timer**

Linux doesn't explicitly detect "I'm in a VM." It detects "I'm at EL1, I can't access EL2, and the virtual timer is available." This naturally leads to using the virtual timer in guests.

### Our VMM vs KVM: Who Does What?

It's important to understand the division of responsibilities:

```
┌─────────────────────────────────────────────────┐
│  Our VMM (god)                     [Userspace]  │
│  - Creates VM via /dev/kvm                      │
│  - Configures devices, handles MMIO             │
│  - Provides Device Tree to guest                │
│  - Can adjust CNTVOFF via ioctl (for migration) │
└─────────────────────────────────────────────────┘
                      │ ioctl()
                      ▼
┌─────────────────────────────────────────────────┐
│  KVM (kernel module)                     [EL2]  │
│  - Runs at EL2 (actual hypervisor privilege)    │
│  - Manages CNTVOFF_EL2 register directly        │
│  - Traps guest timer register access            │
│  - Delivers timer interrupts via GIC            │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│  Guest Linux                             [EL1]  │
│  - Uses virtual timer (CNTV_*)                  │
│  - Sees CNTVCT = CNTPCT - CNTVOFF               │
└─────────────────────────────────────────────────┘
```

**KVM is the actual hypervisor** (runs at EL2, touches hardware registers). We're the **VMM (Virtual Machine Monitor)**—we orchestrate, but KVM does the privileged work.

When you create a vCPU, KVM automatically:
- Sets up the virtual timer infrastructure
- Initializes `CNTVOFF_EL2` (typically to make the guest see time from zero)
- Traps guest access to timer system registers
- Delivers timer interrupts (PPI 27) via the GIC

We don't need to configure anything special for basic operation—we just provide the Device Tree so Linux discovers the timer.

### Managing CNTVOFF: The Virtual Counter Offset

The `CNTVOFF_EL2` register is the key to virtual timer magic. The relationship is:

```
CNTVCT_EL0 (what guest sees) = CNTPCT_EL0 (physical counter) - CNTVOFF_EL2
```

**Who sets it?**
- **KVM** sets it automatically when the VM starts
- **Userspace VMM** (us) *can* modify it via `KVM_ARM_VCPU_TIMER_OFFSET` ioctl—but only when the VM is paused

**Can it change at runtime?**
Yes, but **only when the VM is stopped**. From the kernel documentation:
> "Userspace is not supposed to update that register while the guest is running. Time will either move forward (best case) or backward (really bad idea)."

**How is it calculated during migration?**

When migrating a VM from Host A to Host B:

```
On source host (before migration):
1. Pause VM
2. guest_time = current CNTVCT value the guest sees
3. Save guest_time in migration state

On destination host (after migration):
4. new_physical = read CNTPCT on new host
5. Set CNTVOFF = new_physical - guest_time
   (This makes the guest see the same time it had before)
6. Resume VM
```

The guest's view of time continues smoothly even though the physical counters on the two hosts have no relation to each other.

**Pause/Resume accounting:**

When you pause a VM, the physical counter keeps ticking. To prevent the guest from seeing a time jump on resume:

```
pause_duration = time_resumed - time_paused
new_CNTVOFF = old_CNTVOFF + pause_duration
```

KVM and QEMU can handle this automatically (QEMU has a `kvm-no-adjvtime` option to control it).

## Timer Interrupts

The timer generates **PPI (Private Peripheral Interrupt)** signals. We covered PPIs in Chapter 5—they're per-CPU interrupts in the range 16-31.

### Timer PPI Numbers

ARM defines four timer interrupts:

| Timer | PPI Number | Use |
|-------|------------|-----|
| Secure Physical | 29 | EL3 (TrustZone secure world) |
| Non-secure Physical | 30 | EL1/EL2 (normal world) |
| Virtual | 27 | EL1 guests (this is what Linux uses) |
| Hypervisor | 26 | EL2 (hypervisor's own timer) |

### Why "Non-secure"?

ARM TrustZone divides the system into two worlds:

```
┌─────────────────────────────────────────────────────────────┐
│                    Secure World                              │
│  EL3: Secure Monitor                                         │
│  S-EL1: Secure OS (OP-TEE, etc.)                            │
│                                                              │
│  Timer: Secure Physical (PPI 29)                            │
└─────────────────────────────────────────────────────────────┘
                           ↕ World Switch
┌─────────────────────────────────────────────────────────────┐
│                  Non-Secure World                            │
│  EL2: Hypervisor (KVM)                                       │
│  EL1: Guest OS (Linux)                                       │
│  EL0: User applications                                      │
│                                                              │
│  Timer: Non-secure Physical (PPI 30) or Virtual (PPI 27)    │
└─────────────────────────────────────────────────────────────┘
```

Our guest Linux runs in the non-secure world. It can't access secure resources. The virtual timer (PPI 27) is what it actually uses.

We describe all four timers in the Device Tree for completeness, but Linux will choose the virtual timer.

## Device Tree Configuration

### The Timer Node

Linux discovers the timer through the Device Tree. Here's what the node looks like:

```dts
timer {
    compatible = "arm,armv8-timer";
    interrupts = <GIC_PPI 13 IRQ_TYPE_LEVEL_LOW>,
                 <GIC_PPI 14 IRQ_TYPE_LEVEL_LOW>,
                 <GIC_PPI 11 IRQ_TYPE_LEVEL_LOW>,
                 <GIC_PPI 10 IRQ_TYPE_LEVEL_LOW>;
    always-on;
};
```

Wait—where did 13, 14, 11, 10 come from? The PPI numbers are 29, 30, 27, 26!

### The Device Tree PPI Offset

Device Tree interrupt specifiers for GIC use three cells:

```
<type> <number> <flags>
  1      13      0x04
```

| Cell | Meaning |
|------|---------|
| Type | 0 = SPI, 1 = PPI |
| Number | Interrupt number *within that type* |
| Flags | Trigger configuration |

Here's the key: when the type is PPI (1), the number is the **PPI-relative number**, not the absolute GIC interrupt number.

PPIs occupy GIC interrupts 16-31. In Device Tree, we specify which PPI (0-15), and the kernel adds 16:

| Timer | Raw PPI | Device Tree Number | Calculation |
|-------|---------|-------------------|-------------|
| Secure Physical | 29 | 13 | 29 - 16 = 13 |
| Non-secure Physical | 30 | 14 | 30 - 16 = 14 |
| Virtual | 27 | 11 | 27 - 16 = 11 |
| Hypervisor | 26 | 10 | 26 - 16 = 10 |

This convention avoids redundancy—the type field already says "this is a PPI," so there's no need to encode the 16 offset in the number too.

### The Flags Field

The flags specify the interrupt **trigger type**. This is an important hardware concept:

**Level-Triggered** (what timers use):
```
Signal: ─────┐          ┌─────
             │__________│
             ↑          ↑
          Asserted   Deasserted

While the signal is LOW, the interrupt is active.
The CPU sees the interrupt as long as the condition exists.
You MUST clear the condition to stop the interrupt.
```

**Edge-Triggered** (alternative):
```
Signal: ─────┐__________┌─────
             ↓          ↑
          Falling    Rising
           edge       edge

Interrupt fires ONCE at the transition.
Even if signal stays low, no repeated interrupt.
```

| Value | Meaning |
|-------|---------|
| `0x04` (`IRQ_TYPE_LEVEL_LOW`) | Level-sensitive, active-low |
| `0x08` (`IRQ_TYPE_EDGE_RISING`) | Edge-triggered, rising edge |

**Why timers use level-triggered:**

The timer condition ("counter >= compare value") *persists* once true. The interrupt stays asserted until software handles it by either:
- Setting the timer's IMASK bit (mask the interrupt)
- Clearing ENABLE
- Writing a new TVAL/CVAL (which resets the compare condition)

If you just acknowledge the interrupt at the GIC without doing one of these, the interrupt fires again immediately—because the condition is still true!

### No clock-frequency Needed

You might see some Device Trees with a `clock-frequency` property:

```dts
timer {
    compatible = "arm,armv8-timer";
    clock-frequency = <24000000>;  // DON'T DO THIS
    ...
};
```

This is **strongly discouraged**. From the ARM binding documentation:

> "The clock-frequency property should only be present where necessary to work around broken firmware which does not configure CNTFRQ on all CPUs to a uniform correct value."

With KVM, the firmware is fine—KVM correctly sets `CNTFRQ_EL0` based on the host's timer. The guest reads the frequency directly from the register. We don't need (or want) to specify it in Device Tree.

## Implementation

The timer is mostly handled by KVM. Our job is minimal: provide Device Tree properties so Linux can discover and configure its timer driver.

Create `src/god/devices/timer.py`:

```python
"""
ARM Generic Timer configuration.

The ARM Generic Timer is built into every ARM64 CPU. Unlike the UART (which
we emulate via MMIO) or the GIC (which KVM emulates), the timer is tightly
coupled to the CPU and accessed via system registers (MRS/MSR instructions).

KVM handles timer emulation automatically:
- Guest reads/writes to timer system registers are trapped and emulated
- Timer interrupts are delivered through the GIC
- The counter frequency matches the host hardware

Our only job is to describe the timer in the Device Tree so Linux knows:
1. The timer exists (compatible = "arm,armv8-timer")
2. Which interrupts it uses (the four timer PPIs)

The frequency is NOT specified in Device Tree—Linux reads CNTFRQ_EL0 directly.
"""

# Timer PPI numbers (as seen by the GIC)
# These are Private Peripheral Interrupts - each CPU has its own
TIMER_PPI_SECURE_PHYS = 29      # Secure physical timer (EL3)
TIMER_PPI_NONSECURE_PHYS = 30   # Non-secure physical timer (EL1)
TIMER_PPI_VIRTUAL = 27          # Virtual timer (what Linux uses in VMs)
TIMER_PPI_HYPERVISOR = 26       # Hypervisor timer (EL2)

# Device Tree uses PPI-relative numbers (subtract 16 from raw PPI)
# This is because PPIs are GIC interrupts 16-31, and DT specifies
# the index within the PPI range, not the absolute interrupt number
_DT_PPI_OFFSET = 16


class Timer:
    """
    ARM Generic Timer configuration.

    This class doesn't emulate anything—KVM handles the timer. It provides:
    1. Constants for timer interrupt numbers
    2. Device Tree properties for Linux to discover the timer

    Usage:
        timer = Timer()
        dt_props = timer.get_device_tree_props()
        # Add dt_props to your Device Tree generation
    """

    def __init__(self):
        """Create timer configuration."""
        # Store the PPI numbers for reference
        self.ppi_secure_phys = TIMER_PPI_SECURE_PHYS
        self.ppi_nonsecure_phys = TIMER_PPI_NONSECURE_PHYS
        self.ppi_virtual = TIMER_PPI_VIRTUAL
        self.ppi_hypervisor = TIMER_PPI_HYPERVISOR

    def get_device_tree_props(self) -> dict:
        """
        Get Device Tree properties for the timer node.

        Returns a dict that can be used with a Device Tree library:

            timer_node = fdt.add_node(root, "timer")
            for key, value in timer.get_device_tree_props().items():
                fdt.set_property(timer_node, key, value)

        The interrupts are specified as:
            <type> <number> <flags>

        Where:
            type = 1 for PPI
            number = PPI number - 16 (Device Tree convention)
            flags = 0x04 for level-sensitive, active-low
        """
        # Convert raw PPI numbers to Device Tree format
        def ppi_to_dt(ppi: int) -> tuple[int, int, int]:
            return (1, ppi - _DT_PPI_OFFSET, 0x04)

        return {
            "compatible": "arm,armv8-timer",
            "interrupts": [
                *ppi_to_dt(self.ppi_secure_phys),      # Secure physical
                *ppi_to_dt(self.ppi_nonsecure_phys),   # Non-secure physical
                *ppi_to_dt(self.ppi_virtual),          # Virtual (Linux uses this)
                *ppi_to_dt(self.ppi_hypervisor),       # Hypervisor
            ],
            "always-on": True,
        }

    def __repr__(self) -> str:
        return (
            f"Timer(virtual_ppi={self.ppi_virtual}, "
            f"phys_ppi={self.ppi_nonsecure_phys})"
        )
```

Update `src/god/devices/__init__.py` to export the timer:

```python
"""
Device emulation package.

This package provides emulated devices for the VMM.

There are two types of devices:
1. MMIO devices (Device subclasses): Emulated in user-space, handle guest
   memory accesses. Examples: UART, virtio devices.
2. In-kernel devices: Emulated by KVM for performance. Examples: GIC, timer.

The GIC and timer are special—they're in-kernel devices that we configure
but don't emulate ourselves. KVM handles the actual emulation.
"""

from .device import Device, MMIOAccess, MMIOResult
from .registry import DeviceRegistry
from .uart import PL011UART
from .gic import GIC, GICError
from .timer import Timer, TIMER_PPI_VIRTUAL, TIMER_PPI_NONSECURE_PHYS

__all__ = [
    # MMIO device infrastructure
    "Device",
    "MMIOAccess",
    "MMIOResult",
    "DeviceRegistry",
    # MMIO devices
    "PL011UART",
    # In-kernel devices
    "GIC",
    "GICError",
    "Timer",
    "TIMER_PPI_VIRTUAL",
    "TIMER_PPI_NONSECURE_PHYS",
]
```

## Testing the Timer

Let's write a guest program that demonstrates the timer working. This program will:
1. Read the counter frequency (`CNTFRQ_EL0`)
2. Read the current counter value (`CNTVCT_EL0`)
3. Wait 100ms by polling the counter
4. Read the counter again to show elapsed time
5. Test writing and reading the TVAL register

This is a simple test that doesn't use interrupts—it just verifies that the timer registers are accessible and working correctly.

Create `tests/guest_code/timer_test.S`:

```asm
/*
 * timer_test.S - Test the ARM Generic Timer
 *
 * This program demonstrates that the timer counter is working:
 * 1. Reads CNTFRQ_EL0 to get the timer frequency
 * 2. Reads CNTVCT_EL0 to get current counter value
 * 3. Waits for 100ms by polling the counter
 * 4. Reads counter again and shows elapsed ticks
 * 5. Tests writing and reading the TVAL register
 *
 * This test uses polling (not interrupts) to verify the timer works.
 */

    .global _start

    /* UART constants */
    .equ UART_BASE, 0x09000000
    .equ UART_DR,   0x000

_start:
    /* Set up stack pointer */
    ldr     x0, =0x44000000
    mov     sp, x0

    /* Print banner */
    adr     x0, msg_banner
    bl      print_string

    /* ============================================ */
    /* Read and display counter frequency           */
    /* ============================================ */
    adr     x0, msg_freq
    bl      print_string

    mrs     x19, CNTFRQ_EL0         /* x19 = frequency (save for later) */
    mov     x0, x19
    bl      print_hex
    adr     x0, msg_hz
    bl      print_string

    /* Calculate and print MHz */
    ldr     x0, =1000000
    udiv    x0, x19, x0             /* MHz (integer part) */
    bl      print_decimal
    adr     x0, msg_mhz
    bl      print_string

    /* ============================================ */
    /* Read initial counter value                   */
    /* ============================================ */
    adr     x0, msg_counter1
    bl      print_string

    mrs     x20, CNTVCT_EL0         /* x20 = starting counter value */
    mov     x0, x20
    bl      print_hex
    bl      print_newline

    /* ============================================ */
    /* Wait 100ms by polling the counter            */
    /* target = start + (frequency / 10)            */
    /* ============================================ */
    adr     x0, msg_waiting
    bl      print_string

    /* Calculate ticks for 100ms: frequency / 10 */
    mov     x0, #10
    udiv    x21, x19, x0            /* x21 = ticks for 100ms */

    /* Calculate target counter value */
    add     x22, x20, x21           /* x22 = target = start + ticks_100ms */

wait_loop:
    mrs     x0, CNTVCT_EL0          /* Read current counter */
    cmp     x0, x22                 /* Compare with target */
    b.lt    wait_loop               /* Loop until counter >= target */

    /* ============================================ */
    /* Read counter value after waiting             */
    /* ============================================ */
    adr     x0, msg_counter2
    bl      print_string

    mrs     x23, CNTVCT_EL0         /* x23 = ending counter value */
    mov     x0, x23
    bl      print_hex
    bl      print_newline

    /* ============================================ */
    /* Calculate and display elapsed ticks          */
    /* ============================================ */
    adr     x0, msg_elapsed
    bl      print_string

    sub     x24, x23, x20           /* x24 = elapsed ticks */
    mov     x0, x24
    bl      print_hex
    adr     x0, msg_ticks
    bl      print_string

    /* Calculate actual milliseconds */
    /* ms = (elapsed * 1000) / frequency */
    mov     x0, #1000
    mul     x0, x24, x0             /* elapsed * 1000 */
    udiv    x0, x0, x19             /* / frequency = ms */
    bl      print_decimal
    adr     x0, msg_ms
    bl      print_string

    /* ============================================ */
    /* Test setting and reading TVAL                */
    /* ============================================ */
    adr     x0, msg_tval_test
    bl      print_string

    /* Set TVAL to 1,000,000 ticks */
    ldr     x0, =1000000
    msr     CNTV_TVAL_EL0, x0

    /* Read it back immediately */
    mrs     x0, CNTV_TVAL_EL0
    bl      print_hex
    adr     x0, msg_tval_read
    bl      print_string

    /* Success! */
    adr     x0, msg_success
    bl      print_string

    /* Exit via PSCI SYSTEM_OFF */
    mov     x0, #0x0008
    movk    x0, #0x8400, lsl #16
    hvc     #0

    b       .

    /* ============================================ */
    /* Utility Functions (print_string, print_hex,  */
    /* print_decimal, print_newline)                */
    /* ============================================ */

print_string:
    stp     x29, x30, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
1:  ldrb    w11, [x9], #1
    cbz     w11, 2f
    str     w11, [x10, #UART_DR]
    b       1b
2:  ldp     x29, x30, [sp], #16
    ret

print_hex:
    stp     x29, x30, [sp, #-16]!
    stp     x9, x10, [sp, #-16]!
    stp     x11, x12, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
    mov     x11, #60
1:  lsr     x12, x9, x11
    and     x12, x12, #0xf
    cmp     x12, #10
    b.lt    2f
    add     x12, x12, #('a' - 10)
    b       3f
2:  add     x12, x12, #'0'
3:  str     w12, [x10, #UART_DR]
    subs    x11, x11, #4
    b.ge    1b
    ldp     x11, x12, [sp], #16
    ldp     x9, x10, [sp], #16
    ldp     x29, x30, [sp], #16
    ret

print_decimal:
    /* Simple decimal printing for small numbers */
    stp     x29, x30, [sp, #-16]!
    stp     x9, x10, [sp, #-16]!
    stp     x11, x12, [sp, #-16]!
    stp     x13, x14, [sp, #-16]!
    mov     x9, x0
    ldr     x10, =UART_BASE
    mov     x13, #0
    ldr     x11, =100000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 1f
    cbz     x13, 2f
1:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
2:  ldr     x11, =10000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 3f
    cbz     x13, 4f
3:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
4:  mov     x11, #1000
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 5f
    cbz     x13, 6f
5:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
6:  mov     x11, #100
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 7f
    cbz     x13, 8f
7:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
    mov     x13, #1
8:  mov     x11, #10
    udiv    x12, x9, x11
    msub    x9, x12, x11, x9
    cbnz    x12, 9f
    cbz     x13, 10f
9:  add     x12, x12, #'0'
    str     w12, [x10, #UART_DR]
10: add     x12, x9, #'0'
    str     w12, [x10, #UART_DR]
    ldp     x13, x14, [sp], #16
    ldp     x11, x12, [sp], #16
    ldp     x9, x10, [sp], #16
    ldp     x29, x30, [sp], #16
    ret

print_newline:
    stp     x29, x30, [sp, #-16]!
    ldr     x10, =UART_BASE
    mov     w11, #'\n'
    str     w11, [x10, #UART_DR]
    ldp     x29, x30, [sp], #16
    ret

    /* Data Section */
    .align 4
msg_banner:
    .asciz "=== ARM Timer Test ===\n\n"
msg_freq:
    .asciz "Timer frequency: 0x"
msg_hz:
    .asciz " Hz ("
msg_mhz:
    .asciz " MHz)\n"
msg_counter1:
    .asciz "Counter (start): 0x"
msg_waiting:
    .asciz "Waiting 100ms (polling counter)...\n"
msg_counter2:
    .asciz "Counter (end):   0x"
msg_elapsed:
    .asciz "Elapsed:         0x"
msg_ticks:
    .asciz " ticks ("
msg_ms:
    .asciz " ms)\n\n"
msg_tval_test:
    .asciz "Testing TVAL register...\n"
msg_tval_read:
    .asciz " (wrote 1000000, read back)\n\n"
msg_success:
    .asciz "Timer test PASSED!\n"
```

### What This Test Does

1. **Reads the frequency**: `CNTFRQ_EL0` tells us the timer's tick rate (24 MHz on our system)
2. **Reads the counter**: `CNTVCT_EL0` is the current virtual counter value
3. **Waits 100ms**: Polls the counter until 100ms worth of ticks have elapsed
4. **Shows elapsed ticks**: Calculates how many ticks passed during the wait
5. **Tests TVAL**: Writes to `CNTV_TVAL_EL0` and reads it back to verify the register works

### Building and Running

```bash
# Assemble
aarch64-linux-gnu-as -o tests/guest_code/timer_test.o tests/guest_code/timer_test.S

# Link
aarch64-linux-gnu-ld -nostdlib -static -Ttext=0x40080000 \
    -o tests/guest_code/timer_test tests/guest_code/timer_test.o

# Create binary
aarch64-linux-gnu-objcopy -O binary \
    tests/guest_code/timer_test tests/guest_code/timer_test.bin

# Run
sudo uv run god run tests/guest_code/timer_test.bin
```

Expected output:
```
=== ARM Timer Test ===

Timer frequency: 0x00000000016e3600 Hz (24 MHz)
Counter (start): 0x000000000003174c
Waiting 100ms (polling counter)...
Counter (end):   0x00000000002835dd
Elapsed:         0x0000000000251e91 ticks (101 ms)

Testing TVAL register...
00000000000f4240 (wrote 1000000, read back)

Timer test PASSED!
```

The exact counter values will vary each run, but you should see:
- The frequency matches your host (24 MHz on Apple Silicon)
- The elapsed time should be ~100ms (the test polls until 100ms of counter ticks have passed)
- The TVAL register accepts and returns the value you wrote

## Gotchas

### Virtual vs Physical Counter

When reading the counter:
- `CNTPCT_EL0` - Physical counter (same across all VMs)
- `CNTVCT_EL0` - Virtual counter (per-VM, what Linux uses)

For consistency, use the virtual counter in guest code.

### Timer Interrupt is Level-Triggered

When using timer interrupts (not shown in our simple test), the interrupt stays asserted until you handle it. You must either:
- Disable the timer (`CTL.ENABLE = 0`)
- Mask the interrupt (`CTL.IMASK = 1`)
- Write a new TVAL/CVAL (resets the condition)

If you just acknowledge the interrupt at the GIC without doing one of these, the interrupt will immediately fire again.

### Large Immediate Values

ARM64 can't load arbitrary 64-bit values in a single instruction. For large constants like `1000000`, use `ldr x0, =1000000` (assembler generates a literal pool) instead of `mov x0, #1000000` (which fails for values that can't be encoded).

## What's Next?

In this chapter, we:

1. Learned why timers are essential for operating systems
2. Understood the ARM Generic Timer architecture (counters, timers, system registers)
3. Explored physical vs virtual timers and why VMs prefer virtual
4. Configured the timer Device Tree properties with correct PPI offsets
5. Wrote a test program demonstrating timer interrupts

With the GIC and timer in place, we have the essential interrupt infrastructure for Linux. In the next chapter, we'll put everything together and boot a real Linux kernel!

[Continue to Chapter 7: Booting Linux →](07-booting-linux.md)
