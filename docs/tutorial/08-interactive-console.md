# Chapter 8: Interactive Console

In this chapter, we'll add input support to our UART so the guest can receive keyboard input from the host. This transforms our Linux VM from a one-way display into an interactive system where you can type commands.

## The Problem: Output Without Input

After Chapter 7, our Linux VM boots successfully and prints a welcome message:

```
==========================================
  Welcome to our VMM!
  Linux 6.12.0 on aarch64
==========================================

Boot successful! System running.
Halting now...
```

Then it immediately calls `poweroff -f` and shuts down. Why? Because **our UART only supports output**. The shell can't work because:

1. The shell tries to read input from `/dev/ttyAMA0`
2. The UART's receive buffer is always empty
3. The shell would spin forever waiting for input
4. So the init script just powers off instead of starting a shell

To make Linux truly interactive, we need to complete the UART—adding the ability for the guest to receive characters from the host.

## What We'll Build

By the end of this chapter:

1. The guest can receive keyboard input through the UART
2. Input triggers interrupts (the efficient way)
3. The host terminal is in raw mode for proper character handling
4. The run loop monitors stdin for input
5. A real shell runs and accepts commands

```
Before:                             After:
┌────────────────────────┐         ┌────────────────────────┐
│   Guest (Linux)        │         │   Guest (Linux)        │
│   ┌──────────────┐     │         │   ┌──────────────┐     │
│   │    Shell     │     │         │   │    Shell     │     │
│   └──────┬───────┘     │         │   └──────┬───────┘     │
│          │ read()      │         │          │ read()      │
│          ▼             │         │          ▼             │
│   ┌──────────────┐     │         │   ┌──────────────┐     │
│   │     UART     │     │         │   │     UART     │◄────┼─── Input!
│   └──────┬───────┘     │         │   └──────┬───────┘     │
│          │ write()     │         │          │ write()     │
└──────────┼─────────────┘         └──────────┼─────────────┘
           ▼                                  ▼
      Host stdout                        Host stdout
```

## Key Terminology

| Term | Definition |
|------|------------|
| **RXFE** | Receive FIFO Empty - Flag Register bit (bit 4) indicating no data available to read |
| **RXIS** | Receive Interrupt Status - bit in RIS register indicating receive interrupt pending |
| **Raw mode** | Terminal mode where input is sent immediately without line buffering or processing |
| **Cooked mode** | Default terminal mode with line editing, buffering, and signal handling |
| **select()** | Unix system call that waits for activity on multiple file descriptors |
| **SIGINT** | Signal sent when user presses Ctrl+C (interrupt signal) |
| **IRQ** | Interrupt Request - a signal from hardware requesting CPU attention |
| **Level-triggered** | Interrupt signaling where the line is held high as long as the condition exists |
| **WFI** | Wait For Interrupt - ARM instruction that puts CPU in low-power state until interrupt |
| **immediate_exit** | Flag in kvm_run structure that causes KVM_RUN to return immediately with EINTR |
| **setitimer()** | System call to set up recurring timer signals (SIGALRM) |
| **GSI** | Global System Interrupt - interrupt number used by KVM for routing |

## How Terminal Input Works

### Terminal Modes

When you type in a terminal, the characters don't necessarily go directly to your program. The terminal driver processes them first:

**Cooked Mode (Default)**
- Characters are buffered until you press Enter
- Line editing works (backspace, Ctrl+U to clear line)
- Special characters generate signals (Ctrl+C → SIGINT)
- This is convenient for interactive programs but wrong for raw I/O

**Raw Mode**
- Every character is sent immediately
- No line editing—the program gets raw keystrokes
- Special keys like arrow keys work (send escape sequences)
- This is what we need for a VMM serial console

### Why Raw Mode Matters

Without raw mode:
```
User types: l s Enter
Terminal sends: "ls\n" (after Enter)
```

With raw mode:
```
User types: l
Terminal sends: "l"
User types: s
Terminal sends: "s"
User types: Enter
Terminal sends: "\n"
```

For an interactive console, we want raw mode so:
1. Characters appear immediately as typed
2. Arrow keys, tab completion, etc. work
3. The guest handles all line editing (the shell does this)

### stdin as a File Descriptor

On Unix, stdin is file descriptor 0. We can use `select()` to check if it has data available without blocking. This lets us check for input between vCPU runs.

## Polling vs Interrupts

### The Polling Approach (Bad)

Without interrupts, the guest must constantly check if input is available:

```c
// Guest code - polling approach
while (1) {
    uint32_t flags = *(volatile uint32_t*)UART_FR;
    if (!(flags & RXFE)) {  // RXFE = 0 means data available
        char c = *(volatile char*)UART_DR;
        process_character(c);
    }
    // Loop forever checking...
}
```

**Why polling is terrible:**

1. **CPU waste**: The guest burns 100% CPU just checking for input
2. **No power saving**: The vCPU never reaches WFI (Wait For Interrupt)
3. **Thermal issues**: Real hardware would overheat
4. **Battery drain**: Mobile devices would die quickly
5. **VM exit overhead**: Each MMIO access causes a VM exit

### The Interrupt-Driven Approach (Good)

With interrupts, the guest sleeps until input arrives:

```c
// Guest code - interrupt-driven approach
void uart_irq_handler() {
    while (!(*(volatile uint32_t*)UART_FR & RXFE)) {
        char c = *(volatile char*)UART_DR;
        process_character(c);
    }
    // Clear interrupt when buffer empty
}

// Main code
while (1) {
    wfi();  // Sleep until interrupt
}
```

**Why interrupts are better:**

1. **Efficient**: CPU sleeps when idle
2. **Power-friendly**: WFI puts the core in low-power state
3. **Responsive**: Wake-up is nearly instant when data arrives
4. **Fewer VM exits**: No constant MMIO polling

This is why real hardware uses interrupts, and why we must implement them properly.

## PL011 UART Receive Interrupts

### UART Interrupt Registers

The PL011 has several registers for interrupt handling:

| Offset | Register | Purpose |
|--------|----------|---------|
| 0x038 | IMSC | Interrupt Mask Set/Clear - which interrupts are enabled |
| 0x03C | RIS | Raw Interrupt Status - which interrupts are pending (regardless of mask) |
| 0x040 | MIS | Masked Interrupt Status - `RIS & IMSC` (actually triggering) |
| 0x044 | ICR | Interrupt Clear Register - write to clear interrupts |

### Interrupt Status Bits

| Bit | Name | Meaning |
|-----|------|---------|
| 4 | RXIS | Receive interrupt status - data available in RX FIFO |
| 5 | TXIS | Transmit interrupt status - space available in TX FIFO |
| 6 | RTIS | Receive timeout interrupt status |
| 10 | OEIS | Overrun error interrupt status |

For receiving input, we care about **RXIS** (bit 4).

### The UART's Interrupt Mask vs CPU's Interrupt Mask

There are two levels of interrupt masking, and it's important to understand the difference:

1. **UART's IMSC register**: The UART's own mask. This controls whether the UART *asserts its interrupt line*.

2. **CPU's PSTATE.I bit**: The CPU's interrupt mask. This controls whether the CPU *responds to* asserted interrupt lines.

When we check `MIS = RIS & IMSC` before injecting an interrupt, we're emulating the **UART's behavior**, not the CPU's. The IMSC register lets the guest tell the UART: "Don't bother me about receive events right now."

Why would the guest do this? Examples:
- Temporarily disable RX interrupts while processing a batch of characters
- Disable interrupts during critical sections where the UART driver holds locks
- Power management scenarios

**What about data loss?** Data doesn't get lost because:
- The data stays safely in our `_rx_buffer`
- The RXFE flag in the Flag Register still correctly reports "data available"
- The guest can still poll if it wants—we just don't *interrupt* it

The GIC and CPU handle their own masking. If we assert IRQ 33 but the CPU has interrupts disabled (PSTATE.I = 1), the interrupt stays pending until the CPU unmasks. That's the GIC's job, not ours. Our job is to correctly emulate when the *UART* asserts its line.

### IRQ Numbers and Device Identification

When an interrupt fires, how does the kernel know which device caused it? The answer is **we told it in the Device Tree**.

IRQ 33 isn't magically "the UART interrupt"—we assigned it. In our Device Tree (from Chapter 7):

```dts
uart@9000000 {
    compatible = "arm,pl011";
    reg = <0x0 0x09000000 0x0 0x1000>;
    interrupts = <GIC_SPI 1 IRQ_TYPE_LEVEL_HIGH>;  // SPI 1 = IRQ 33
};
```

This tells the kernel: "The device at 0x09000000 uses SPI 1 (interrupt 33). When you receive IRQ 33, call the pl011 driver's interrupt handler."

**Multiple serial consoles** would each get different IRQ numbers:

```dts
uart0@9000000 {
    interrupts = <GIC_SPI 1 ...>;  // IRQ 33
};
uart1@9001000 {
    interrupts = <GIC_SPI 2 ...>;  // IRQ 34
};
uart2@9002000 {
    interrupts = <GIC_SPI 3 ...>;  // IRQ 35
};
```

The kernel maintains a mapping: IRQ 33 → uart0's handler, IRQ 34 → uart1's handler, etc. When IRQ 34 fires, the kernel calls uart1's handler, which reads from 0x9001000—not from uart0.

This is the fundamental purpose of the Device Tree: it's the VMM declaring "here's how I wired up the virtual hardware."

### Level-Triggered Interrupts and Who Deasserts

Most device interrupts, including the UART's, are **level-triggered**. This means the device *holds* the interrupt line high as long as the interrupt condition exists. Understanding this is crucial for correct emulation.

**The interrupt lifecycle:**

```
1. Host: User types 'l'
         │
         ▼
2. VMM: Inject 'l' into UART rx_buffer
        Set RIS.RXIS = 1 (data available)
        Assert IRQ line (hold HIGH)
         │
         ▼
3. GIC: Sees IRQ 33 HIGH
        Signals CPU "you have a pending interrupt"
         │
         ▼
4. CPU: Jumps to interrupt vector
        Kernel's IRQ handler runs
         │
         ▼
5. Kernel: Calls pl011 driver's IRQ handler
           Handler reads UART_DR (gets 'l')
           Our rx_buffer is now empty
         │
         ▼
6. VMM: After DR read, buffer empty
        Clear RIS.RXIS
        Deassert IRQ line (goes LOW)
         │
         ▼
7. CPU: IRQ line is now LOW
        Interrupt handler returns normally
        Execution continues
```

**Why the VMM must deassert:**

The kernel doesn't "deassert" the interrupt directly—it **services the device** (reads the data), which causes the device to deassert. In real hardware:

- Device holds line HIGH while condition exists (buffer has data)
- Kernel reads data, buffer empties
- Device releases line (goes LOW)

The kernel does write to ICR to acknowledge the interrupt and clear RIS, but the *physical line state* reflects the device's internal condition. We emulate this by:

1. When guest reads DR and empties buffer → clear RXIS
2. When MIS becomes 0 → call `gic.inject_irq(33, level=False)`

### ARM KVM IRQ Encoding

When injecting interrupts with `KVM_IRQ_LINE` on ARM, the `irq` field uses a specific bit encoding—it's not just the interrupt number:

```
bits:  | 31 ... 24 | 23  ... 16 | 15    ...    0 |
field: | irq_type  | vcpu_index |     irq_id     |
```

| irq_type | Meaning |
|----------|---------|
| 0 | SPI via IRQ routing (requires `KVM_SET_GSI_ROUTING`) |
| 1 | SPI via GIC directly |
| 2 | PPI (Private Peripheral Interrupt) |

For SPIs (like our UART interrupt), we use `irq_type = 1` and put the full GIC interrupt ID in `irq_id`. For IRQ 33 (SPI 1):

```python
# Encoding for IRQ 33 (SPI 1)
KVM_ARM_IRQ_TYPE_SPI = 1
encoded_irq = (KVM_ARM_IRQ_TYPE_SPI << 24) | 33  # = 0x01000021
```

If you just pass `33` without the type encoding, KVM returns `ENXIO` (errno 6) because it doesn't know how to route the interrupt.

### Updating GIC for ARM IRQ Encoding

We need to update the GIC's `inject_irq` method to use the proper ARM encoding. Update `src/god/devices/gic.py`:

```python
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

    NOTE: KVM_IRQ_LINE on ARM expects a specific bit encoding, not just
    the interrupt number. This method handles the conversion.

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
        raise GICError(
            f"Failed to {action} IRQ {irq} (encoded 0x{encoded_irq:08x}): "
            f"errno {get_errno()}"
        )
```

**What if we didn't deassert?** The guest would be stuck in an infinite interrupt loop:
1. Enter interrupt handler
2. Read all data, return from handler
3. Immediately re-enter handler (line still HIGH!)
4. No data to read, return
5. Immediately re-enter handler...

The guest would never make progress. Correct level-triggered semantics are essential.

### Interrupt Flow Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Interrupt Flow                                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Host stdin                                                         │
│       │                                                              │
│       │ User types 'x'                                               │
│       ▼                                                              │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ VMM: uart.inject_input(b'x')                                 │  │
│   │      _rx_buffer.append('x')                                  │  │
│   │      _ris |= INT_RX                                          │  │
│   │      if (_ris & _imsc):        ◄─── UART's own mask          │  │
│   │          gic.inject_irq(33, level=True)                      │  │
│   └──────────────────────────────────────────────────────────────┘  │
│       │                                                              │
│       ▼                                                              │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ GIC: IRQ 33 line is HIGH                                     │  │
│   │      Signal pending interrupt to CPU                         │  │
│   └──────────────────────────────────────────────────────────────┘  │
│       │                                                              │
│       ▼                                                              │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Guest CPU: Takes interrupt                                   │  │
│   │            Jumps to IRQ vector                               │  │
│   │            Kernel calls pl011_int() handler                  │  │
│   └──────────────────────────────────────────────────────────────┘  │
│       │                                                              │
│       ▼                                                              │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Guest reads UART_DR                                          │  │
│   │ VMM: Returns 'x', removes from _rx_buffer                    │  │
│   │      Buffer now empty → _ris &= ~INT_RX                      │  │
│   │      MIS now 0 → gic.inject_irq(33, level=False)             │  │
│   └──────────────────────────────────────────────────────────────┘  │
│       │                                                              │
│       ▼                                                              │
│   Guest returns from interrupt handler                               │
│   Continues normal execution                                         │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## The Run Loop and Input Handling

### The WFI Problem on ARM

You might wonder: why not use `select()` to wait on both stdin and the vCPU file descriptor simultaneously?

The vCPU fd doesn't work that way. You can't `select()` on it to wait for "guest wants to exit." The vCPU fd is used with `ioctl(KVM_RUN)`, which is a **blocking call** that returns when the guest exits.

But here's the real problem on ARM: **WFI doesn't cause a VM exit**.

When the Linux guest is idle (shell waiting for input), the kernel's idle loop executes the `WFI` (Wait For Interrupt) instruction. On x86, the equivalent `HLT` instruction causes a VM exit. But on ARM with KVM, WFI is handled **inside the kernel**—KVM keeps the vCPU blocked until an interrupt arrives. The `ioctl(KVM_RUN)` call simply doesn't return.

This means a naive polling approach won't work:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Naive Run Loop (BROKEN!)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   1. select(stdin, timeout=0)  ◄─── Check for input             │
│      │                                                           │
│      ├─► No input available                                      │
│      │                                                           │
│   2. vcpu.run()  ◄─── Guest executes WFI... blocks forever!     │
│      │                                                           │
│      X  Never returns until an interrupt                         │
│         But we can't inject an interrupt while blocked!          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

The guest is waiting for a UART interrupt to signal input, but we can't inject the interrupt because we're stuck in `KVM_RUN`. Deadlock!

### The Solution: Interrupting KVM_RUN

Real VMMs like [Firecracker](https://github.com/firecracker-microvm/firecracker) solve this using the `immediate_exit` flag in the `kvm_run` structure. This is a mechanism that tells KVM: "Return from KVM_RUN immediately with EINTR."

The `kvm_run` structure (shared memory between kernel and userspace) has this layout:

```c
struct kvm_run {
    __u8 request_interrupt_window;  // offset 0
    __u8 immediate_exit;            // offset 1  ◄─── This!
    __u8 padding1[6];               // offset 2-7
    __u32 exit_reason;              // offset 8
    // ... more fields
};
```

### How immediate_exit Works

The `immediate_exit` flag has a simple contract:

1. **If `immediate_exit = 1` when you call `KVM_RUN`**: The ioctl returns immediately with `-1` and `errno = EINTR`. The guest doesn't run at all.

2. **If the vCPU is blocked in WFI and a signal arrives**: The signal handler runs (interrupting the blocked syscall), and if it sets `immediate_exit = 1`, the kernel sees this and returns with `EINTR`.

**How do we know if it was a real exit or an interruption?**

The `ioctl()` return value tells us:
- **Return value >= 0**: A real VM exit occurred. Check `kvm_run->exit_reason` for why (MMIO, system event, etc.)
- **Return value < 0 with errno = EINTR**: We were interrupted. The guest may or may not have run—we don't know and don't care. Just check stdin and try again.

### How Signal Delivery Interrupts KVM_RUN

You might wonder: "If we modify kvm_run memory while the kernel is blocked in an ioctl, how does the kernel notice?" The answer lies in how Unix signals work with shared memory.

The `kvm_run` structure is **shared memory** between userspace and the kernel. When we create a vCPU, we `mmap()` the vCPU file descriptor to get a pointer to this structure. Both our code and the kernel can read/write it directly.

Here's what happens when SIGALRM fires while we're blocked in KVM_RUN:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USERSPACE                                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   kvm_run structure (shared memory, mmap'd)                          │
│   ┌─────────────────────────────────────┐                            │
│   │ immediate_exit: 0                   │ ◄─── We can write here     │
│   │ exit_reason: ...                    │                            │
│   └─────────────────────────────────────┘                            │
│                                                                      │
│   Our VMM code:                                                      │
│     ioctl(vcpu_fd, KVM_RUN)  ───────────────┐                        │
│         (blocked, waiting)                  │                        │
│                                             │                        │
├─────────────────────────────────────────────┼────────────────────────┤
│                         KERNEL              │                        │
├─────────────────────────────────────────────┼────────────────────────┤
│                                             ▼                        │
│   KVM's ioctl handler runs a loop:                                   │
│                                                                      │
│   kvm_arch_vcpu_ioctl_run() {                                        │
│       while (1) {                                                    │
│           if (signal_pending() || kvm_run->immediate_exit) {         │
│               return -EINTR;  ◄─── CHECK POINT                       │
│           }                                                          │
│           enter_guest();      // Actually run guest code             │
│           handle_exit();      // Guest exited, handle it             │
│       }                                                              │
│   }                                                                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**The sequence when SIGALRM fires:**

1. **Timer expires**: The kernel's timer subsystem notices our 100ms timer expired and marks SIGALRM as pending for our process.

2. **Kernel checks for signals**: At certain points (like when the vCPU blocks in WFI), the kernel checks: "Does this process have pending signals?"

3. **Signal delivery**: The kernel *temporarily returns to userspace* to run our signal handler:
   ```python
   def interactive_signal_handler(signum, frame):
       vcpu.set_immediate_exit(True)  # Writes 1 to shared memory!
   ```

4. **Return to kernel**: After our handler completes, control returns to the kernel.

5. **Kernel checks immediate_exit**: The kernel re-reads `kvm_run->immediate_exit` from the shared memory. It sees `1` and decides to return `-EINTR` instead of continuing the guest.

The key insight is that **signal delivery happens in the middle of the ioctl**. The kernel doesn't blindly wait forever—it periodically checks for signals. When it delivers a signal to userspace, our handler modifies the shared `kvm_run` structure, and the kernel sees the change when it resumes.

This is exactly how production VMMs like Firecracker handle the same problem. The shared memory design of `kvm_run` exists specifically to enable this kind of communication between the VMM and the kernel's KVM code.

### Putting It Together: Pseudo-Code

Here's pseudo-code showing exactly what happens:

```python
# Pseudo-code: What happens inside the kernel and our VMM

# === SETUP (once at start) ===
def setup_interactive_mode():
    # Set up a timer that fires every 100ms
    setitimer(ITIMER_REAL, interval=0.1, repeat=0.1)

    # When timer fires, this handler runs
    def on_timer_signal():
        kvm_run.immediate_exit = 1  # Tell KVM to return

    signal(SIGALRM, on_timer_signal)

# === THE RUN LOOP ===
while True:
    # Step 1: Check for stdin input (non-blocking)
    if select(stdin, timeout=0) says "data available":
        data = read(stdin)
        uart.inject_input(data)  # This triggers an interrupt

    # Step 2: Clear the flag before entering guest
    # (It might be set from a previous timer interrupt)
    kvm_run.immediate_exit = 0

    # Step 3: Run the guest
    # This is where the magic happens...
    result = ioctl(vcpu_fd, KVM_RUN)

    # What happened inside the kernel:
    #
    # CASE A: Guest runs and exits normally
    #   1. Kernel checks immediate_exit... it's 0, so continue
    #   2. Guest code runs
    #   3. Guest does MMIO access (or shutdown, or other exit)
    #   4. KVM_RUN returns with result=0
    #   5. kvm_run.exit_reason = KVM_EXIT_MMIO (or whatever)
    #
    # CASE B: Guest runs, then timer fires while in WFI
    #   1. Kernel checks immediate_exit... it's 0, so continue
    #   2. Guest code runs
    #   3. Guest executes WFI (waiting for interrupt)
    #   4. vCPU blocks in kernel, waiting...
    #   5. 100ms passes, SIGALRM fires!
    #   6. Signal handler sets immediate_exit = 1
    #   7. Kernel sees signal, checks immediate_exit, sees 1
    #   8. KVM_RUN returns with result=-1, errno=EINTR
    #
    # CASE C: Timer fires BEFORE we even enter guest
    #   1. Signal arrives just as we call ioctl()
    #   2. Handler sets immediate_exit = 1
    #   3. Kernel checks immediate_exit... it's 1!
    #   4. KVM_RUN returns immediately with result=-1, errno=EINTR
    #   5. Guest never ran at all

    # Step 4: Check what happened
    if result < 0 and errno == EINTR:
        # Cases B or C: We were interrupted
        # Loop back to check stdin - maybe user typed something!
        continue

    if result < 0:
        # Some other error
        raise Error("KVM_RUN failed")

    # Case A: Real VM exit - handle it
    if kvm_run.exit_reason == KVM_EXIT_MMIO:
        handle_mmio()
    elif kvm_run.exit_reason == KVM_EXIT_SYSTEM_EVENT:
        break  # Guest wants to shut down
    # ... etc
```

### The Key Insight

The beauty of this approach is that **we don't care whether the guest actually ran or not** when we get `EINTR`. We just:

1. Check stdin for any input that arrived
2. Inject it if there is any (which queues an interrupt)
3. Clear `immediate_exit`
4. Try running the guest again

If we injected input, the guest will wake from WFI (because there's now a pending interrupt) and process it. If we didn't inject anything, the guest will go back to WFI and we'll get interrupted again in 100ms.

The 100ms interval means:
- **Worst case latency**: 100ms from keypress to character appearing
- **Typical latency**: Much less (the guest often exits for other reasons)
- **CPU overhead**: Minimal (we wake up 10 times/second when idle)

### Visual Timeline

```
Time ──────────────────────────────────────────────────────────────────►

VMM:     [check stdin] [run]        [check stdin] [run]     [check stdin]
              │          │                │          │            │
              │          │                │          │            │
Kernel:       │    ┌─────┴─────┐          │    ┌─────┴─────┐      │
              │    │ Guest runs│          │    │ Guest runs│      │
              │    │           │          │    │   (WFI)   │      │
              │    │   MMIO    │          │    │     ▲     │      │
              │    │   exit    │          │    │     │     │      │
              │    └─────┬─────┘          │    │  SIGALRM  │      │
              │          │                │    │  (EINTR)  │      │
              │          │                │    └─────┬─────┘      │
              ▼          ▼                ▼          ▼            ▼
         ─────●──────────●────────────────●──────────●────────────●─────
              │          │                │          │            │
              │          │                │          │            │
         No input    Handle MMIO     User types    EINTR,     Inject 'a',
                                        'a'      loop back    run guest
                                                              (wakes from
                                                               interrupt)
```

This approach, inspired by [Firecracker's implementation](https://github.com/firecracker-microvm/firecracker/pull/304), ensures we periodically get control back to check stdin, even when the guest is idle in WFI.

## Implementation: Terminal Management

First, let's create a module to handle terminal mode switching. Create `src/god/terminal.py`:

```python
"""
Terminal mode management for raw/cooked mode switching.

When our VMM runs, we need the terminal in raw mode so:
- Characters are sent immediately (no line buffering)
- Special keys work (Ctrl+C, arrow keys, etc.)
- We can restore the terminal properly on exit

This module provides a context manager for safe terminal handling.
"""

import atexit
import sys
import termios
import tty
from typing import TextIO


class TerminalMode:
    """
    Manages terminal mode switching between raw and cooked modes.

    Raw mode sends characters immediately without buffering.
    Cooked mode (default) buffers until Enter and handles special keys.

    Usage:
        with TerminalMode(sys.stdin) as term:
            # Terminal is in raw mode here
            while True:
                if term.has_input():
                    char = term.read_char()
                    # process char...

    The terminal is automatically restored to its original mode on exit,
    even if an exception occurs.
    """

    def __init__(self, stream: TextIO = sys.stdin):
        """
        Create a terminal mode manager.

        Args:
            stream: The terminal stream to manage (default: stdin)
        """
        self._stream = stream
        self._fd = stream.fileno()
        self._original_attrs: list | None = None
        self._in_raw_mode = False

    def enter_raw_mode(self) -> None:
        """
        Switch the terminal to raw mode.

        In raw mode:
        - Input is available immediately (no buffering)
        - No echo of typed characters
        - Ctrl+C doesn't generate SIGINT
        - Special keys send escape sequences

        The original mode is saved and will be restored by exit_raw_mode()
        or automatically when the context manager exits.
        """
        if self._in_raw_mode:
            return

        # Save current terminal attributes
        self._original_attrs = termios.tcgetattr(self._fd)

        # Register cleanup handler in case of unexpected exit
        atexit.register(self._cleanup)

        # Switch to raw mode
        # tty.setraw() sets the terminal to raw mode:
        # - ICANON off: don't wait for Enter
        # - ECHO off: don't echo characters
        # - ISIG off: don't generate signals for Ctrl+C etc.
        # - Plus various other flags for raw I/O
        tty.setraw(self._fd)

        self._in_raw_mode = True

    def exit_raw_mode(self) -> None:
        """
        Restore the terminal to its original mode.

        This should be called before exiting, but the context manager
        and atexit handler will call it automatically if needed.
        """
        if not self._in_raw_mode:
            return

        if self._original_attrs is not None:
            # Restore original terminal attributes
            # TCSADRAIN waits for output to drain before applying
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original_attrs)

        # Unregister cleanup handler
        try:
            atexit.unregister(self._cleanup)
        except Exception:
            pass

        self._in_raw_mode = False

    def _cleanup(self) -> None:
        """Cleanup handler for atexit - restores terminal mode."""
        self.exit_raw_mode()

    @property
    def fd(self) -> int:
        """Get the file descriptor for use with select()."""
        return self._fd

    @property
    def in_raw_mode(self) -> bool:
        """Check if terminal is currently in raw mode."""
        return self._in_raw_mode

    def read_char(self) -> bytes:
        """
        Read a single character from the terminal.

        Returns:
            A single byte as a bytes object.

        Note: This blocks if no input is available. Use select() to
        check for input first.
        """
        import os
        return os.read(self._fd, 1)

    def __enter__(self) -> "TerminalMode":
        """Enter context manager - switch to raw mode."""
        self.enter_raw_mode()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager - restore terminal mode."""
        self.exit_raw_mode()
```

## Implementation: UART Input Support

Now let's update the UART to support receive interrupts. We need to:

1. Set the RXIS bit when data arrives
2. Fire an interrupt if the guest has enabled RX interrupts (IMSC)
3. Clear the interrupt when the guest reads all data
4. Deassert the IRQ line when MIS becomes zero

Update `src/god/devices/uart.py`:

```python
"""
PL011 UART emulation.

The PL011 is ARM's standard UART (Universal Asynchronous Receiver/Transmitter),
part of the PrimeCell peripheral family. It's what Linux uses when you specify
"console=ttyAMA0" on the kernel command line.

We emulate:
- Writes to the Data Register (DR) print characters to stdout
- Reads from the Flag Register (FR) report transmitter/receiver status
- Receive interrupts when input is available
"""

import sys
from typing import TYPE_CHECKING, TextIO

from god.vm.layout import UART
from .device import Device

if TYPE_CHECKING:
    from god.devices.gic import GIC


class PL011UART(Device):
    """
    PL011 UART (serial port) emulator with interrupt support.

    This provides serial console functionality:
    - Writes to DR output characters to the host terminal
    - Reads from FR return status (transmit ready, receive status)
    - Receive interrupts notify the guest when input is available

    Usage:
        uart = PL011UART()
        registry.register(uart)

        # Later, inject input:
        uart.inject_input(b"ls\\n")
    """

    # ==========================================================================
    # Register Offsets
    # ==========================================================================
    # These are offsets from the base address (0x09000000)

    DR = 0x000      # Data Register - read/write serial data
    RSR = 0x004     # Receive Status Register / Error Clear Register
    FR = 0x018      # Flag Register - status flags
    ILPR = 0x020    # IrDA Low-Power Counter Register (not used)
    IBRD = 0x024    # Integer Baud Rate Divisor
    FBRD = 0x028    # Fractional Baud Rate Divisor
    LCR_H = 0x02C   # Line Control Register (data format: bits, parity, etc.)
    CR = 0x030      # Control Register (enable/disable UART)
    IFLS = 0x034    # Interrupt FIFO Level Select
    IMSC = 0x038    # Interrupt Mask Set/Clear
    RIS = 0x03C     # Raw Interrupt Status
    MIS = 0x040     # Masked Interrupt Status
    ICR = 0x044     # Interrupt Clear Register
    DMACR = 0x048   # DMA Control Register

    # ==========================================================================
    # Flag Register (FR) Bits
    # ==========================================================================
    # These tell the guest about the UART's current status

    FR_TXFE = 1 << 7  # Transmit FIFO Empty (1 = all data sent)
    FR_RXFF = 1 << 6  # Receive FIFO Full (1 = can't receive more)
    FR_TXFF = 1 << 5  # Transmit FIFO Full (1 = can't send more)
    FR_RXFE = 1 << 4  # Receive FIFO Empty (1 = no data to read)
    FR_BUSY = 1 << 3  # UART Busy transmitting

    # ==========================================================================
    # Control Register (CR) Bits
    # ==========================================================================

    CR_UARTEN = 1 << 0  # UART Enable
    CR_TXE = 1 << 8     # Transmit Enable
    CR_RXE = 1 << 9     # Receive Enable

    # ==========================================================================
    # Interrupt Bits (for IMSC, RIS, MIS, ICR)
    # ==========================================================================

    INT_RX = 1 << 4     # Receive interrupt (RXIS)
    INT_TX = 1 << 5     # Transmit interrupt (TXIS)
    INT_RT = 1 << 6     # Receive timeout interrupt (RTIS)
    INT_OE = 1 << 10    # Overrun error interrupt (OEIS)

    def __init__(
        self,
        output: TextIO = sys.stdout,
        base_address: int | None = None,
        size: int | None = None,
        irq: int = 33,
    ):
        """
        Create a PL011 UART.

        Args:
            output: Where to write output characters. Defaults to stdout.
                    You can pass a StringIO for testing.
            base_address: MMIO base address. Defaults to layout.UART.base.
            size: MMIO region size. Defaults to layout.UART.size.
            irq: Interrupt number for the UART (default: 33 = SPI 1).
        """
        self._output = output
        self._base_address = base_address if base_address is not None else UART.base
        self._size = size if size is not None else UART.size
        self._irq = irq

        # Internal register state
        self._cr = 0          # Control Register
        self._lcr_h = 0       # Line Control Register
        self._ibrd = 0        # Integer Baud Rate Divisor
        self._fbrd = 0        # Fractional Baud Rate Divisor
        self._imsc = 0        # Interrupt Mask
        self._ris = 0         # Raw Interrupt Status

        # Receive buffer
        self._rx_buffer: list[int] = []

        # GIC reference for interrupt injection (set by VMRunner)
        self._gic: "GIC | None" = None

        # Track if IRQ is currently asserted (for level-triggered semantics)
        self._irq_asserted = False

    @property
    def name(self) -> str:
        return "PL011 UART"

    @property
    def base_address(self) -> int:
        return self._base_address

    @property
    def size(self) -> int:
        return self._size

    @property
    def irq(self) -> int:
        """Get the IRQ number for this UART."""
        return self._irq

    def set_gic(self, gic: "GIC") -> None:
        """
        Set the GIC reference for interrupt injection.

        This is called by VMRunner when setting up devices.

        Args:
            gic: The GIC instance to use for injecting interrupts.
        """
        self._gic = gic

    def read(self, offset: int, size: int) -> int:
        """Handle a read from the UART."""

        if offset == self.DR:
            # Data Register read - return received character (if any)
            if self._rx_buffer:
                char = self._rx_buffer.pop(0)
                # Update interrupt state after read
                self._update_rx_interrupt()
                return char
            return 0

        elif offset == self.FR:
            # Flag Register - tell guest about our status
            flags = 0

            # Transmit FIFO is always empty - we send instantly
            flags |= self.FR_TXFE

            # Receive FIFO is empty unless we have buffered input
            if not self._rx_buffer:
                flags |= self.FR_RXFE

            return flags

        elif offset == self.RSR:
            # Receive Status Register - no errors to report
            return 0

        elif offset == self.CR:
            return self._cr

        elif offset == self.LCR_H:
            return self._lcr_h

        elif offset == self.IBRD:
            return self._ibrd

        elif offset == self.FBRD:
            return self._fbrd

        elif offset == self.IMSC:
            return self._imsc

        elif offset == self.RIS:
            return self._ris

        elif offset == self.MIS:
            # Masked Interrupt Status = RIS & IMSC
            return self._ris & self._imsc

        else:
            # Unknown register - return 0
            return 0

    def write(self, offset: int, size: int, value: int):
        """Handle a write to the UART."""

        if offset == self.DR:
            # Data Register write - output the character!
            char = value & 0xFF
            self._output.write(chr(char))
            self._output.flush()

        elif offset == self.RSR:
            # Writing to RSR clears error flags
            pass

        elif offset == self.CR:
            self._cr = value

        elif offset == self.LCR_H:
            self._lcr_h = value

        elif offset == self.IBRD:
            self._ibrd = value

        elif offset == self.FBRD:
            self._fbrd = value

        elif offset == self.IMSC:
            self._imsc = value
            # Mask change might affect interrupt state
            self._update_irq_line()

        elif offset == self.ICR:
            # Interrupt Clear Register - clear specified interrupts
            self._ris &= ~value
            # Update IRQ line after clearing
            self._update_irq_line()

    def reset(self):
        """Reset the UART to initial state."""
        self._cr = 0
        self._lcr_h = 0
        self._ibrd = 0
        self._fbrd = 0
        self._imsc = 0
        self._ris = 0
        self._rx_buffer.clear()
        self._irq_asserted = False

    def inject_input(self, data: bytes) -> None:
        """
        Inject input data into the receive buffer.

        This simulates receiving data on the serial port. The guest
        can then read these bytes from the Data Register.

        If the guest has enabled receive interrupts (via IMSC), an IRQ
        will be asserted to notify it of the available data.

        Args:
            data: Bytes to make available to the guest.
        """
        for byte in data:
            self._rx_buffer.append(byte)

        # Set RX interrupt status (data available)
        self._ris |= self.INT_RX

        # Update IRQ line (will assert if guest has RX interrupts enabled)
        self._update_irq_line()

    def _update_rx_interrupt(self) -> None:
        """
        Update RX interrupt status based on buffer state.

        Called after reading from DR to update interrupt status.
        """
        if self._rx_buffer:
            # Still have data - keep interrupt set
            self._ris |= self.INT_RX
        else:
            # Buffer empty - clear RX interrupt
            self._ris &= ~self.INT_RX

        self._update_irq_line()

    def _update_irq_line(self) -> None:
        """
        Update the physical IRQ line based on interrupt state.

        This implements level-triggered interrupt semantics:
        - Assert (hold HIGH) when MIS has any bits set
        - Deassert (release LOW) when MIS is zero

        The IRQ stays asserted as long as the interrupt condition exists.
        The guest must service the interrupt (read data) to clear it.
        """
        if self._gic is None:
            return

        mis = self._ris & self._imsc

        if mis and not self._irq_asserted:
            # Condition exists and line not yet asserted - assert it
            self._gic.inject_irq(self._irq, level=True)
            self._irq_asserted = True
        elif not mis and self._irq_asserted:
            # Condition cleared - deassert the line
            self._gic.inject_irq(self._irq, level=False)
            self._irq_asserted = False
```

## Implementation: vCPU immediate_exit Support

Before updating the runner, we need to add a method to set the `immediate_exit` flag. Add this method to `src/god/vcpu/vcpu.py`:

```python
# In src/god/vcpu/vcpu.py, add this method to the VCPU class:

def set_immediate_exit(self, value: bool) -> None:
    """
    Set the immediate_exit flag in kvm_run.

    When immediate_exit is set to 1, the next KVM_RUN will return
    immediately with -EINTR instead of entering guest mode. This is
    used to interrupt a vCPU that might be blocked in WFI.

    This technique is used by production VMMs like Firecracker to
    handle the case where we need to check for stdin input while
    the guest might be blocked waiting for an interrupt.

    Args:
        value: True to set immediate_exit, False to clear it.
    """
    # immediate_exit is at offset 1 in kvm_run structure:
    #   offset 0: request_interrupt_window (u8)
    #   offset 1: immediate_exit (u8)
    immediate_exit_ptr = ffi.cast("uint8_t *", self._kvm_run + 1)
    immediate_exit_ptr[0] = 1 if value else 0
```

## Implementation: Updated Run Loop

Now let's update the runner to use `setitimer()` and `immediate_exit` for proper stdin handling. Here's the complete `src/god/vcpu/runner.py`:

```python
"""
VM execution runner.

This module provides the run loop that executes guest code and handles VM exits.
The run loop is the heart of the VMM - it's a simple concept:

1. Tell the vCPU to run (KVM_RUN)
2. Guest executes until something happens
3. Check why it stopped (exit_reason)
4. Handle the exit (emulate device, report error, etc.)
5. Go back to step 1

For interactive mode, we use the immediate_exit mechanism (inspired by
Firecracker) to periodically interrupt KVM_RUN and check stdin for input.
This is necessary on ARM because WFI doesn't cause a VM exit—the vCPU
blocks in the kernel waiting for an interrupt.
"""

import os
import select
import signal
import sys

from god.kvm.constants import (
    KVM_EXIT_HLT,
    KVM_EXIT_MMIO,
    KVM_EXIT_SYSTEM_EVENT,
    KVM_EXIT_INTERNAL_ERROR,
    KVM_EXIT_FAIL_ENTRY,
)
from god.kvm.system import KVMSystem
from god.vm.vm import VirtualMachine
from god.devices import DeviceRegistry, MMIOAccess, GIC, PL011UART
from god.terminal import TerminalMode
from .vcpu import VCPU
from . import registers


class RunnerError(Exception):
    """Exception raised when runner encounters an error."""
    pass


class VMRunner:
    """
    Runs a virtual machine.

    This class manages the run loop and coordinates between the VM,
    vCPU, and device handlers. It also sets up the GIC (interrupt
    controller) and handles terminal input for interactive console.

    The initialization sequence is:
    1. VMRunner() - creates the GIC (required before vCPUs)
    2. create_vcpu() - creates vCPUs (can call multiple times)
    3. run() - finalizes GIC and starts execution

    Usage:
        runner = VMRunner(vm, kvm)
        vcpu = runner.create_vcpu()
        vcpu.set_pc(entry_point)
        runner.load_binary("/path/to/binary", entry_point)
        stats = runner.run(interactive=True)
    """

    def __init__(
        self,
        vm: VirtualMachine,
        kvm: KVMSystem,
        devices: DeviceRegistry | None = None,
        create_gic: bool = True,
    ):
        """
        Create a runner for a VM.

        Args:
            vm: The VirtualMachine to run.
            kvm: The KVMSystem instance.
            devices: Device registry for MMIO handling.
            create_gic: If True (default), create the GIC automatically.
        """
        self._vm = vm
        self._kvm = kvm
        self._devices = devices if devices is not None else DeviceRegistry()
        self._vcpus: list[VCPU] = []
        self._gic: GIC | None = None
        self._uart: PL011UART | None = None

        # Create the GIC (interrupt controller)
        if create_gic:
            self._gic = GIC(self._vm.fd)
            self._gic.create()

        # Find UART in device registry and link it to GIC
        self._setup_uart_gic_link()

    def _setup_uart_gic_link(self) -> None:
        """Find the UART device and give it a reference to the GIC."""
        if self._gic is None:
            return

        for device in self._devices._devices:
            if isinstance(device, PL011UART):
                device.set_gic(self._gic)
                self._uart = device
                break

    @property
    def devices(self) -> DeviceRegistry:
        """Get the device registry."""
        return self._devices

    @property
    def gic(self) -> GIC | None:
        """Get the GIC (interrupt controller)."""
        return self._gic

    @property
    def uart(self) -> PL011UART | None:
        """Get the UART device (if registered)."""
        return self._uart

    @property
    def vcpus(self) -> list[VCPU]:
        """Get all created vCPUs."""
        return self._vcpus

    def create_vcpu(self) -> VCPU:
        """
        Create and return a vCPU.

        Returns:
            The created VCPU.
        """
        vcpu_id = len(self._vcpus)
        vcpu = VCPU(self._vm.fd, self._kvm, vcpu_id=vcpu_id)
        self._vcpus.append(vcpu)
        return vcpu

    def load_binary(self, path: str, entry_point: int) -> int:
        """
        Load a binary file into guest memory.

        Args:
            path: Path to the binary file.
            entry_point: Guest address where the binary should be loaded.

        Returns:
            Number of bytes loaded.
        """
        size = self._vm.memory.load_file(entry_point, path)
        print(f"Loaded {size} bytes at 0x{entry_point:08x}")
        return size

    def _handle_mmio(self, vcpu: VCPU) -> bool:
        """
        Handle an MMIO exit by dispatching to the device registry.

        Args:
            vcpu: The vCPU that triggered the MMIO exit.

        Returns:
            True if the access was handled by a device, False otherwise.
        """
        phys_addr, data_bytes, length, is_write = vcpu.get_mmio_info()

        if is_write:
            data = int.from_bytes(data_bytes, "little")
        else:
            data = 0

        access = MMIOAccess(
            address=phys_addr,
            size=length,
            is_write=is_write,
            data=data,
        )

        result = self._devices.handle_mmio(access)

        if not is_write:
            result_bytes = result.data.to_bytes(length, "little")
            vcpu.set_mmio_data(result_bytes)

        return result.handled

    def run(
        self,
        max_exits: int = 100000,
        quiet: bool = False,
        interactive: bool = False,
    ) -> dict:
        """
        Run the VM until it halts or hits max_exits.

        Args:
            max_exits: Maximum number of VM exits before giving up.
            quiet: If True, suppress debug output.
            interactive: If True, enable stdin input for the UART console.
                        This puts the terminal in raw mode and uses
                        setitimer() to periodically interrupt KVM_RUN.

        Returns:
            Dict with execution statistics.
        """
        if not self._vcpus:
            raise RunnerError("No vCPUs created - call create_vcpu() first")

        # Finalize GIC before running
        if self._gic is not None and not self._gic.finalized:
            self._gic.finalize()

        stats = {
            "exits": 0,
            "hlt": False,
            "exit_reason": None,
            "exit_counts": {},
        }

        vcpu = self._vcpus[0]

        # Save original signal handler so we can restore it later
        original_handler = signal.getsignal(signal.SIGALRM)

        if interactive and self._uart is not None:
            # =================================================================
            # INTERACTIVE MODE
            # =================================================================
            # We need to periodically interrupt KVM_RUN so we can check stdin.
            # Strategy (inspired by Firecracker):
            #   1. Set up a recurring SIGALRM timer (100ms)
            #   2. Signal handler sets vcpu.immediate_exit = True
            #   3. This causes KVM_RUN to return with EINTR
            #   4. We check stdin, inject any input, and continue
            # =================================================================

            def interactive_signal_handler(signum, frame):
                """Called every 100ms by SIGALRM - interrupts KVM_RUN."""
                vcpu.set_immediate_exit(True)

            signal.signal(signal.SIGALRM, interactive_signal_handler)
            signal.setitimer(signal.ITIMER_REAL, 0.1, 0.1)  # 100ms recurring

            try:
                with TerminalMode(sys.stdin) as term:
                    stats = self._run_loop(vcpu, max_exits, quiet, term)
            finally:
                # Always restore signal handling, even on exception
                signal.setitimer(signal.ITIMER_REAL, 0, 0)  # Stop timer
                signal.signal(signal.SIGALRM, original_handler)
        else:
            # =================================================================
            # NON-INTERACTIVE MODE
            # =================================================================
            # No stdin handling needed. Just set up a timeout to catch hangs.
            # =================================================================

            if not quiet:
                def timeout_handler(signum, frame):
                    print("\n[TIMEOUT - vCPU appears stuck, dumping registers]")
                    vcpu.dump_registers()
                    sys.exit(1)

                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(5)  # 5 second timeout

            try:
                stats = self._run_loop(vcpu, max_exits, quiet, None)
            finally:
                if not quiet:
                    signal.alarm(0)  # Cancel timeout
                    signal.signal(signal.SIGALRM, original_handler)

        return stats

    def _run_loop(
        self,
        vcpu: VCPU,
        max_exits: int,
        quiet: bool,
        term: TerminalMode | None,
    ) -> dict:
        """
        The main run loop.

        This is where guest code actually executes. The loop:
        1. Checks for stdin input (if interactive)
        2. Runs the vCPU until it exits
        3. Handles the exit reason
        4. Repeats

        Args:
            vcpu: The vCPU to run.
            max_exits: Maximum exits before stopping (safety limit).
            quiet: Suppress debug output.
            term: Terminal manager for interactive input, or None.

        Returns:
            Execution statistics dict.
        """
        stats = {
            "exits": 0,
            "hlt": False,
            "exit_reason": None,
            "exit_counts": {},
        }

        # Get stdin file descriptor for select() if interactive
        stdin_fd = term.fd if term else -1

        for i in range(max_exits):
            # =================================================================
            # STEP 1: Check for stdin input (non-blocking)
            # =================================================================
            # In interactive mode, we check if the user typed anything.
            # This runs after every vCPU exit, including EINTR from our timer.
            if term is not None and self._uart is not None:
                readable, _, _ = select.select([stdin_fd], [], [], 0)
                if stdin_fd in readable:
                    data = os.read(stdin_fd, 256)
                    if data:
                        # Inject input into UART - this sets the RX interrupt
                        # pending, so the guest will wake from WFI
                        self._uart.inject_input(data)

                # Clear immediate_exit before running the guest.
                # It may have been set by our SIGALRM handler.
                vcpu.set_immediate_exit(False)

            # =================================================================
            # STEP 2: Run the vCPU
            # =================================================================
            # This blocks until:
            #   - Guest does something that causes an exit (MMIO, shutdown)
            #   - Our SIGALRM fires and sets immediate_exit (returns EINTR)
            if not quiet and i < 5:
                print(f"  [vCPU run #{i}]")

            exit_reason = vcpu.run()

            if not quiet and i < 5:
                exit_name = vcpu.get_exit_reason_name(exit_reason)
                print(f"  [vCPU exit: {exit_name}]")

            # =================================================================
            # STEP 3: Check if this was a real exit or just our timer
            # =================================================================
            # exit_reason == -1 means EINTR (we were interrupted by signal)
            # This is expected in interactive mode - just loop back to check
            # stdin and try again.
            if exit_reason == -1:
                continue  # Back to step 1

            # =================================================================
            # STEP 4: Handle real VM exits
            # =================================================================
            stats["exits"] += 1
            exit_name = vcpu.get_exit_reason_name(exit_reason)
            stats["exit_counts"][exit_name] = (
                stats["exit_counts"].get(exit_name, 0) + 1
            )
            stats["exit_reason"] = exit_name

            if exit_reason == KVM_EXIT_HLT:
                # Guest executed HLT instruction.
                # Note: On ARM, WFI typically doesn't cause KVM_EXIT_HLT -
                # KVM handles it internally. But if we do get here, treat it
                # as the guest wanting to halt.
                stats["hlt"] = True
                break

            elif exit_reason == KVM_EXIT_MMIO:
                # Guest accessed a memory address that isn't RAM.
                # Dispatch to our device registry to handle it.
                if not quiet and stats["exits"] <= 10:
                    phys_addr, _, length, is_write = vcpu.get_mmio_info()
                    op = "W" if is_write else "R"
                    print(f"  MMIO[{stats['exits']}]: {op} 0x{phys_addr:08x} ({length}B)")
                self._handle_mmio(vcpu)

            elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
                # Guest requested shutdown/reset via PSCI.
                # This is how "poweroff -f" works.
                if not quiet:
                    print("\n[Guest requested shutdown/reset]")
                break

            elif exit_reason == KVM_EXIT_INTERNAL_ERROR:
                print("\n[KVM internal error]")
                vcpu.dump_registers()
                raise RunnerError("KVM internal error")

            elif exit_reason == KVM_EXIT_FAIL_ENTRY:
                print("\n[Failed to enter guest mode]")
                vcpu.dump_registers()
                raise RunnerError("Entry to guest mode failed")

            else:
                if not quiet:
                    print(f"\n[Unhandled exit: {exit_name}]")
                    vcpu.dump_registers()
                break

        return stats
```

## Updating the Init Script

Now we need to update the initramfs to start a shell instead of powering off.

### Why `exec /bin/sh`?

The init script uses `exec /bin/sh` rather than just `/bin/sh`. Here's why:

**Without exec:**
```sh
#!/bin/sh
/bin/sh   # Starts shell as child process
# When shell exits, we return here and script ends
# PID 1 (init) exits → kernel panic!
```

**With exec:**
```sh
#!/bin/sh
exec /bin/sh   # Replaces init with shell
# Shell IS now PID 1
# When shell exits → kernel handles it (poweroff/panic)
```

Using `exec` replaces the init process with the shell, so the shell becomes PID 1. This is fine for our minimal system.

### Is This the "Right" Approach?

For a production system, no. A proper init process:

1. **Reaps orphaned processes**: When a process's parent dies, it becomes a child of PID 1. Init must call `wait()` to clean up these zombies.

2. **Supervises services**: Restarts services if they crash.

3. **Handles shutdown**: Sends SIGTERM to children, waits, then SIGKILL.

4. **Manages the system**: Mounting, networking, logging, etc.

Real systems use:
- **systemd**: Full-featured service manager (most Linux distros)
- **OpenRC**: Lighter alternative (Alpine, Gentoo)
- **runit/s6**: Minimalist process supervision
- **BusyBox init**: Simple init for embedded systems

For our tutorial, `exec /bin/sh` is perfect because:
- Simplicity—nothing extra to explain
- When you type `poweroff -f`, the VM cleanly shuts down
- We're not running services that need supervision
- It demonstrates the concept without the complexity

If you wanted proper BusyBox init, you'd create `/etc/inittab`:
```
::respawn:/sbin/getty -L ttyAMA0 115200 vt100
::ctrlaltdel:/sbin/reboot
::shutdown:/bin/umount -a -r
```

And change the init script to just `exec /sbin/init`. But that's beyond our scope.

Update `src/god/build/initramfs.py`:

```python
"""
Initramfs creation.

This module creates CPIO archives for use as initramfs.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path


class InitramfsBuilder:
    """
    Creates initramfs archives.

    Usage:
        builder = InitramfsBuilder(work_dir="./build")
        builder.create_structure()
        builder.install_busybox(busybox_path)
        builder.create_init()
        cpio_path = builder.pack()
    """

    INIT_SCRIPT = """\
#!/bin/sh

# Mount essential filesystems
mount -t devtmpfs devtmpfs /dev
mount -t proc proc /proc
mount -t sysfs sysfs /sys

# Display banner
echo "=========================================="
echo "  Welcome to our VMM!"
echo "  Linux $(uname -r) on $(uname -m)"
echo "=========================================="
echo

# Start an interactive shell
# Using 'exec' replaces the init process with the shell,
# so the shell becomes PID 1
exec /bin/sh
"""

    def __init__(self, work_dir: str | Path = "./build"):
        """
        Create an initramfs builder.

        Args:
            work_dir: Directory for build artifacts
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.rootfs_dir = self.work_dir / "initramfs"
        self.cpio_path = self.work_dir / "initramfs.cpio"

    def create_structure(self) -> None:
        """Create the directory structure for initramfs."""
        print("Creating initramfs structure...")

        # Remove old if exists
        if self.rootfs_dir.exists():
            shutil.rmtree(self.rootfs_dir)

        # Create directories
        dirs = ["bin", "sbin", "dev", "proc", "sys", "etc", "tmp", "root"]
        for d in dirs:
            (self.rootfs_dir / d).mkdir(parents=True)

        # Create essential device nodes
        try:
            dev = self.rootfs_dir / "dev"
            os.mknod(dev / "console", stat.S_IFCHR | 0o600, os.makedev(5, 1))
            os.mknod(dev / "null", stat.S_IFCHR | 0o666, os.makedev(1, 3))
            print("Created device nodes")
        except PermissionError:
            print("Note: Could not create device nodes (need root)")
            print("      Kernel devtmpfs will handle this at boot")

        print("Structure created")

    def install_busybox(self, busybox_path: Path) -> None:
        """
        Install BusyBox and create symlinks.

        Args:
            busybox_path: Path to busybox binary
        """
        print("Installing BusyBox...")

        # Copy busybox binary
        dest = self.rootfs_dir / "bin" / "busybox"
        shutil.copy2(busybox_path, dest)
        dest.chmod(0o755)

        # Create symlinks for common commands
        commands = [
            # /bin commands
            (
                "bin",
                [
                    "sh",
                    "ash",
                    "ls",
                    "cat",
                    "echo",
                    "mkdir",
                    "rm",
                    "cp",
                    "mv",
                    "ln",
                    "chmod",
                    "chown",
                    "pwd",
                    "sleep",
                    "true",
                    "false",
                    "test",
                    "[",
                    "[[",
                    "printf",
                    "kill",
                    "ps",
                    "grep",
                    "sed",
                    "awk",
                    "cut",
                    "head",
                    "tail",
                    "sort",
                    "uniq",
                    "wc",
                    "tr",
                    "vi",
                    "clear",
                    "reset",
                    "stty",
                    "tty",
                    "date",
                    "uname",
                    "hostname",
                    "dmesg",
                    "env",
                    "id",
                    "whoami",
                ],
            ),
            # /sbin commands
            (
                "sbin",
                [
                    "init",
                    "mount",
                    "umount",
                    "poweroff",
                    "reboot",
                    "halt",
                    "mdev",
                    "ifconfig",
                    "route",
                    "ip",
                ],
            ),
        ]

        for dir_name, cmds in commands:
            for cmd in cmds:
                link = self.rootfs_dir / dir_name / cmd
                if not link.exists():
                    if dir_name == "sbin":
                        link.symlink_to("../bin/busybox")
                    else:
                        link.symlink_to("busybox")

        print("BusyBox installed")

    def create_init(self, script: str | None = None) -> None:
        """
        Create the init script.

        Args:
            script: Custom init script content, or use default
        """
        init_path = self.rootfs_dir / "init"
        init_path.write_text(script or self.INIT_SCRIPT)
        init_path.chmod(0o755)
        print("Created /init script")

    def pack(self, compress: bool = False) -> Path:
        """
        Create the CPIO archive.

        Args:
            compress: If True, gzip the archive

        Returns:
            Path to the created archive
        """
        print("Creating CPIO archive...")

        cpio_cmd = ["cpio", "-o", "-H", "newc"]

        result = subprocess.run(
            ["find", "."],
            cwd=self.rootfs_dir,
            capture_output=True,
            text=True,
            check=True,
        )

        with open(self.cpio_path, "wb") as f:
            subprocess.run(
                cpio_cmd,
                cwd=self.rootfs_dir,
                input=result.stdout,
                stdout=f,
                text=True,
                check=True,
            )

        if compress:
            subprocess.run(
                ["gzip", "-f", str(self.cpio_path)],
                check=True,
            )
            self.cpio_path = self.cpio_path.with_suffix(".cpio.gz")

        size = self.cpio_path.stat().st_size
        print(f"Created {self.cpio_path} ({size} bytes)")

        return self.cpio_path
```

## Updating the CLI

Finally, update the CLI to support interactive mode. In `src/god/cli.py`, modify the `boot_linux` command to add the `--interactive` flag and pass it to the runner:

```python
@app.command("boot")
def boot_linux(
    kernel: str = typer.Argument(..., help="Path to kernel Image"),
    initrd: str = typer.Option(
        None, "--initrd", "-i", help="Path to initramfs (cpio or cpio.gz)"
    ),
    cmdline: str = typer.Option(
        "console=ttyAMA0 earlycon=pl011,0x09000000",
        "--cmdline",
        "-c",
        help="Kernel command line",
    ),
    ram_mb: int = typer.Option(1024, "--ram", "-r", help="RAM size in megabytes"),
    dtb: str = typer.Option(
        None,
        "--dtb",
        "-d",
        help="Path to custom DTB file (optional, generates one if not provided)",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Show debug output (MMIO accesses, exit stats)",
    ),
    interactive: bool = typer.Option(
        True,
        "--interactive/--no-interactive",
        help="Enable interactive console (stdin input to guest)",
    ),
):
    """
    Boot a Linux kernel.

    Loads the kernel and optional initramfs into the VM and starts execution.
    A Device Tree is generated automatically unless a custom one is provided.

    By default, interactive mode is enabled, allowing you to type commands
    in the guest shell. Use --no-interactive for non-interactive boot.

    Examples:
        god boot Image --initrd initramfs.cpio
        god boot Image -i rootfs.cpio.gz -c "console=ttyAMA0 debug"
        god boot Image --dtb custom.dtb --ram 2048 --no-interactive
    """
    # ... (existing setup code) ...

    # Run with interactive mode
    stats = runner.run(max_exits=10_000_000, quiet=not debug, interactive=interactive)

    # ... (rest of function) ...
```

## Testing

### Step 1: Rebuild the Initramfs

First, rebuild the initramfs with the new init script:

```bash
god build initramfs --compress
```

This creates `build/initramfs.cpio.gz` with the shell-based init script. The `--compress` flag makes it smaller, which is important since it's loaded into RAM.

### Step 2: Boot Linux

Now boot with interactive mode:

```bash
god boot build/linux/arch/arm64/boot/Image --initrd build/initramfs.cpio.gz
```

You should see:

```
==========================================
  Welcome to our VMM!
  Linux 6.12.0 on aarch64
==========================================

/ #
```

### Step 3: Test Commands

Try running some commands:

```
/ # ls
bin   dev   etc   init  proc  root  sbin  sys   tmp
/ # echo "Hello from the shell!"
Hello from the shell!
/ # cat /proc/cpuinfo
processor       : 0
...
/ # uname -a
Linux (none) 6.12.0 #1 SMP PREEMPT ... aarch64 GNU/Linux
/ # ps
PID   USER     TIME  COMMAND
    1 0         0:00 /bin/sh
    2 0         0:00 ps
/ #
```

### Step 4: Exit

To exit the shell and shut down the VM:

```
/ # poweroff -f
```

## Debugging Tips

### Input Not Working?

1. **Check terminal mode**: Add debug prints in `TerminalMode` to verify raw mode is activated
2. **Check select()**: Print when stdin has data available
3. **Check UART inject**: Print when `inject_input()` is called
4. **Check interrupts**: Print when IRQ is asserted/deasserted

### Shell Exits Immediately?

If the shell starts but immediately exits:
1. Check if the init script has `exec /bin/sh` (not just `/bin/sh`)
2. Verify stdin is connected properly
3. Check kernel messages with `dmesg` for UART errors

### No Output After Input?

The guest might be waiting for interrupts:
1. Verify IMSC has RX interrupts enabled (guest kernel does this)
2. Check that GIC `inject_irq()` is being called
3. Verify IRQ 33 is correct for the UART

## Summary

In this chapter, we:

1. **Learned about terminal modes**: Raw mode sends characters immediately, which is what we need for an interactive console.

2. **Understood polling vs interrupts**: Polling wastes CPU cycles; interrupts are efficient and let the CPU sleep when idle.

3. **Mastered level-triggered interrupt semantics**: The device holds the IRQ line high while the condition exists; the VMM must deassert when the condition clears.

4. **Understood the interrupt hierarchy**: The UART's IMSC is separate from the CPU's interrupt masking—both must allow the interrupt for it to fire.

5. **Learned the ARM KVM IRQ encoding**: `KVM_IRQ_LINE` on ARM uses a bit-encoded format with irq_type, vcpu_index, and irq_id fields.

6. **Solved the WFI problem**: On ARM, WFI doesn't cause a VM exit. We use the `immediate_exit` flag (inspired by [Firecracker](https://github.com/firecracker-microvm/firecracker)) with `setitimer()` to periodically interrupt `KVM_RUN` and check for stdin input.

7. **Implemented UART receive interrupts**: When input arrives, we set the RXIS bit and trigger IRQ 33 through the GIC.

8. **Created terminal management**: The `TerminalMode` class safely handles switching between raw and cooked modes.

9. **Made Linux interactive**: The shell now works, and you can run commands in your VM!

## What's Next

In Chapter 9, we'll implement virtio devices for high-performance I/O. Virtio provides a standardized interface for virtual devices that's much faster than emulating real hardware like the UART.

[Continue to Chapter 9: Virtio Devices →](09-virtio-devices.md)
