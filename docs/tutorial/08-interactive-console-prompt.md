# Chapter 8: Interactive Console - Writing Prompt

Use this prompt to guide an agent in writing Chapter 8 of the VMM tutorial.

## Context

This is Chapter 8 of a tutorial series building a VMM (Virtual Machine Monitor) from scratch for ARM64 on Linux using KVM. The reader has completed:

- Chapter 1: KVM Foundation (opening /dev/kvm, ioctls)
- Chapter 2: VM Creation and Memory (memory slots, guest physical addresses)
- Chapter 3: vCPU and Run Loop (KVM_RUN, handling exits)
- Chapter 4: Serial Console (PL011 UART - **output only**)
- Chapter 5: Interrupt Controller (GICv3)
- Chapter 6: Timer (ARM architected timer)
- Chapter 7: Booting Linux (kernel, DTB, initramfs - boots but can't interact)

The current state: Linux boots, prints a welcome message, and immediately calls `poweroff` because **the UART only supports output**. The shell can't work because it can't read keyboard input.

## What This Chapter Must Cover

### Goal
Add input support to the PL011 UART so the guest can receive keyboard input from the host. This enables an interactive shell.

### Technical Requirements

1. **UART Receive Support**: The PL011 UART already has basic scaffolding (`inject_input()` method, `_rx_buffer`), but it's not connected to anything. We need to:
   - Read host stdin
   - Feed characters into the UART's receive buffer
   - Handle the RXFE (Receive FIFO Empty) flag correctly in the Flag Register

2. **Interrupt-Driven Input** (required approach): When input arrives, trigger UART RX interrupt (IRQ 33) via the GIC so the guest doesn't have to poll. This requires:
   - Setting the RIS (Raw Interrupt Status) register's RXIS bit when data arrives
   - Checking IMSC (Interrupt Mask) to see if RX interrupts are enabled
   - Calling `gic.inject_irq(33, level=True)` when `(RIS & IMSC) != 0`
   - Clearing the interrupt when guest writes to ICR

3. **Discuss but reject polling-based approach**: Explain why polling is bad:
   - Guest busy-waits checking Flag Register in a loop
   - Wastes CPU cycles (100% CPU usage even when idle)
   - Prevents power-saving (WFI never reached)
   - Real hardware uses interrupts for good reason

4. **Terminal Handling**: The host terminal needs to be in raw mode so:
   - Characters are sent immediately (no line buffering)
   - Special keys work (Ctrl+C, arrow keys, etc.)
   - We can restore terminal state on exit

5. **Non-blocking stdin**: The run loop currently blocks on `vcpu.run()`. We need to also monitor stdin for input. Options:
   - Use `select()` or `poll()` to wait on both vCPU fd and stdin
   - Use a separate thread for stdin (simpler but has synchronization concerns)
   - Use non-blocking stdin with periodic checks (simpler, slight latency)

   Recommend: `select()` approach - clean, efficient, no threading complexity.

6. **Update init script**: Change from `poweroff -f` to `exec /bin/sh` now that input works.

### New Acronyms/Terms to Define

| Term | Definition |
|------|------------|
| **RXFE** | Receive FIFO Empty - Flag Register bit indicating no data available to read |
| **RXIS** | Receive Interrupt Status - bit in RIS indicating receive interrupt pending |
| **Raw mode** | Terminal mode where input is unbuffered and unprocessed |
| **Cooked mode** | Default terminal mode with line editing and buffering |
| **select()** | Unix system call that waits for activity on multiple file descriptors |
| **SIGINT** | Signal sent when user presses Ctrl+C |

### Files to Modify/Create

1. **`src/god/devices/uart.py`**:
   - Add interrupt triggering when RX data arrives
   - Implement proper RXIS bit handling
   - Add method to check if interrupt should fire

2. **`src/god/vcpu/runner.py`**:
   - Modify run loop to use `select()` on stdin + vCPU
   - Read stdin and inject into UART
   - Set up raw terminal mode

3. **`src/god/terminal.py`** (new file):
   - Terminal mode management (raw/restore)
   - Context manager for safe cleanup

4. **`src/god/build/initramfs.py`**:
   - Update `INIT_SCRIPT` to use `exec /bin/sh` instead of `poweroff -f`

5. **`src/god/cli.py`**:
   - Minor updates if needed for terminal handling

### Code Style Requirements (CRITICAL)

1. **Complete implementations only**: Every code block must be the full, working implementation. No "add a method like this" or "you could do something like". Show the exact code.

2. **Explain all acronyms**: First use of any acronym must be spelled out with a clear definition.

3. **No silent failures**: Don't over-use try/except. Let errors propagate with clear messages. Only catch exceptions when there's a specific recovery action.

4. **Match existing style**: Look at chapters 4-7 for format:
   - Clear section headers
   - Tables for register bits, flags, etc.
   - ASCII diagrams for data flow
   - Code blocks with full implementations
   - Explanatory text between code sections

5. **Test everything**: Show how to verify each piece works before moving on.

### Chapter Structure

```
# Chapter 8: Interactive Console

## Introduction
- What we're building and why
- Current limitation (output only)
- What an interactive console enables

## How Terminal Input Works
- Terminal modes (raw vs cooked)
- Why we need raw mode
- stdin as a file descriptor

## Polling vs Interrupts
- Explain polling approach
- Why it's wasteful
- Interrupt-driven approach
- Why interrupts are better

## PL011 UART Receive Interrupts
- UART registers involved (RIS, MIS, IMSC, ICR)
- RXIS bit
- Interrupt flow diagram
- Integration with GIC

## Implementation: Terminal Management
- Full terminal.py implementation
- Raw mode setup
- Cleanup on exit

## Implementation: UART Input Support
- Updated uart.py with interrupt support
- inject_input() triggering interrupts
- Full code listing

## Implementation: Updated Run Loop
- Using select() for stdin + vCPU
- Reading and injecting input
- Full runner.py changes

## Updating the Init Script
- Change to exec /bin/sh
- Why this works now

## Testing
- Boot and interact with shell
- Verify commands work
- Test Ctrl+C handling

## Summary
- What we built
- Key concepts learned

## What's Next
- Preview of Chapter 9 (Virtio)
```

### Expected Final Output

After this chapter, running `god boot` should produce:

```
==========================================
  Welcome to our VMM!
  Linux 6.12.0 on aarch64
==========================================

/ # ls
bin   dev   etc   init  proc  root  sbin  sys   tmp
/ # echo "Hello from the shell!"
Hello from the shell!
/ # cat /proc/cpuinfo
processor       : 0
...
/ # uname -a
Linux (none) 6.12.0 #1 SMP ... aarch64 GNU/Linux
/ # exit
```

### Important Notes

1. The UART already has `inject_input()` and `_rx_buffer` - don't rewrite from scratch, extend what exists.

2. The GIC is already set up and working. Use `runner.gic.inject_irq(33, level=True)` to inject UART interrupt.

3. UART IRQ is 33 (SPI 1) - this is defined in `vm/layout.py` as `UART_IRQ`.

4. Keep error handling minimal - this is a tutorial, not production code. Clear error messages over silent recovery.

5. The terminal must be restored even if the program crashes. Use `atexit` or context managers.

6. Test incrementally: first verify input reaches the UART, then verify interrupts fire, then verify shell works.
