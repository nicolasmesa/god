# Chapter 4: Emulating a Serial Console (PL011 UART)

In this chapter, we'll implement a serial console so our guest can print text. This is a crucial milestone—we'll finally see "Hello, World!" from our virtual machine!

## Why Serial Ports?

### A Brief History

Before graphical displays, computers communicated through **serial ports**—physical connections that sent data one bit at a time. Terminals (keyboard + screen) connected to mainframes via serial cables. The protocol was simple: send a byte, it appears on the screen.

Even today, serial ports are used for:
- Server management (out-of-band access when network is down)
- Embedded systems debugging
- Virtual machine consoles
- Kernel early boot output

### Why We Need It

Our guest currently has no way to communicate with the outside world. It can execute code and halt, but it can't tell us what it's doing. A serial console lets the guest:

1. Print debug messages
2. Output boot progress
3. Later: accept keyboard input

The Linux kernel uses serial ports for early boot messages (before graphics drivers load). The kernel command line option `console=ttyAMA0` tells Linux to use the PL011 UART.

### What Does "ttyAMA0" Mean?

The device name `ttyAMA0` breaks down as:

| Part | Meaning |
|------|---------|
| `tty` | "Teletype" - historical Unix name for terminal devices |
| `AMA` | "ARM AMBA" - ARM's Advanced Microcontroller Bus Architecture |
| `0` | First device of this type |

**AMBA** is ARM's standard bus for connecting peripherals. The PL011 UART is part of ARM's "PrimeCell" IP library, and all PrimeCell peripherals connect via the AMBA bus. So when Linux loads its `amba-pl011` driver, devices appear as `/dev/ttyAMA*`.

You might also see other tty device names:

| Name | Hardware |
|------|----------|
| `ttyS0` | PC-style serial port (8250/16550 UART) |
| `ttyUSB0` | USB-to-serial adapter |
| `ttyAMA0` | ARM PL011 UART (what we're implementing) |

## How MMIO Works

### Memory-Mapped I/O

On x86 systems, there are two ways for the CPU to talk to devices:
1. **Port I/O**: Special IN/OUT instructions access a separate I/O address space
2. **Memory-Mapped I/O (MMIO)**: Devices appear as memory addresses

ARM systems use **only MMIO**. There are no I/O ports. To talk to a device, you read from or write to its memory address.

```c
// Writing to a serial port on ARM
volatile uint32_t *uart_data = (uint32_t *)0x09000000;
*uart_data = 'H';  // Send character 'H'
```

### How KVM Traps MMIO

Remember our memory layout? We only registered RAM (starting at 0x40000000) with KVM. The UART address (0x09000000) is not backed by RAM.

When the guest accesses an address not in any memory region:
1. The CPU detects this is not valid RAM
2. The CPU triggers a VM exit
3. KVM reports `KVM_EXIT_MMIO` with details about the access
4. We handle the access (emulate the device)
5. We resume the guest

```
Guest executes: str w0, [x1]  where x1=0x09000000

    │
    ▼
CPU: "Address 0x09000000 is not in RAM"
    │
    ▼
VM Exit to KVM
    │
    ▼
KVM: "exit_reason = KVM_EXIT_MMIO"
     "mmio.phys_addr = 0x09000000"
     "mmio.data = [0x48, ...]"  (if write)
     "mmio.is_write = true"
     "mmio.len = 4"
    │
    ▼
Our VMM: handle the UART write
    │
    ▼
Resume guest with KVM_RUN
```

## The PL011 UART

### What is PL011?

**PL011** is ARM's standard UART design. "PL" stands for **PrimeCell**, ARM's brand for peripheral IP. The 011 is just a model number.

PL011 is well-documented, widely supported, and Linux has a built-in driver for it. It's the obvious choice for our serial console.

### PL011 Register Map

The PL011 has many registers, but we only need to implement a few for basic functionality:

```
Offset   Name    Size   Description
------   ----    ----   -----------
0x000    DR      32     Data Register - read/write serial data
0x018    FR      32     Flag Register - status flags
0x024    IBRD    32     Integer Baud Rate Divisor
0x028    FBRD    32     Fractional Baud Rate Divisor
0x02C    LCR_H   32     Line Control Register (data format)
0x030    CR      32     Control Register (enable/disable)
0x034    IFLS    32     Interrupt FIFO Level Select
0x038    IMSC    32     Interrupt Mask Set/Clear
0x03C    RIS     32     Raw Interrupt Status
0x040    MIS     32     Masked Interrupt Status
0x044    ICR     32     Interrupt Clear Register
```

### The Important Registers

**DR (Data Register) - Offset 0x000**

This is where data is sent and received.

- **Write**: The byte you write is transmitted out the serial port
- **Read**: Returns a received byte (if any)

For writes, the bottom 8 bits are the character to send. Other bits contain error flags (which we'll ignore for now).

**FR (Flag Register) - Offset 0x018**

This tells the guest about the UART's status:

```
Bit   Name   Meaning
---   ----   -------
0     CTS    Clear To Send (hardware flow control)
1     DSR    Data Set Ready
2     DCD    Data Carrier Detect
3     BUSY   UART is busy transmitting
4     RXFE   Receive FIFO Empty (1 = no data to read)
5     TXFF   Transmit FIFO Full (1 = can't accept more data)
6     RXFF   Receive FIFO Full
7     TXFE   Transmit FIFO Empty (1 = all data sent)
8     RI     Ring Indicator
```

For output, the guest checks bit 5 (TXFF). If it's 0, the transmit FIFO has room and the guest can write.

**CR (Control Register) - Offset 0x030**

Controls whether the UART is enabled:

```
Bit   Name   Meaning
---   ----   -------
0     UARTEN UART Enable (1 = enabled)
8     TXE    Transmit Enable
9     RXE    Receive Enable
```

### Minimum Viable UART

To get output working, we need to handle:

1. **Writes to DR (0x000)**: Print the character to our terminal
2. **Reads from FR (0x018)**: Return status (transmitter ready, receiver empty)

Everything else can be stubbed or ignored initially.

## Implementation: Device Framework

Before implementing PL011, let's create a framework for device emulation.

### Base Device Class

Create `src/god/devices/device.py`:

```python
"""
Base class for emulated devices.

All devices that handle MMIO accesses should inherit from this class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class MMIOAccess:
    """
    Describes an MMIO access.

    Attributes:
        address: The physical address accessed
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
        data: For reads, the data to return (as int)
        handled: Whether the access was handled
    """
    data: int = 0
    handled: bool = True


class Device(ABC):
    """
    Base class for all emulated devices.

    Subclasses must implement:
    - name: Human-readable device name
    - base_address: Where the device is in guest memory
    - size: Size of the device's MMIO region
    - read(): Handle read accesses
    - write(): Handle write accesses
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Device name for debugging."""
        pass

    @property
    @abstractmethod
    def base_address(self) -> int:
        """Base address in guest physical memory."""
        pass

    @property
    @abstractmethod
    def size(self) -> int:
        """Size of the device's MMIO region."""
        pass

    def contains(self, address: int) -> bool:
        """Check if an address falls within this device's region."""
        return self.base_address <= address < self.base_address + self.size

    def offset(self, address: int) -> int:
        """Get the offset within the device for an address."""
        return address - self.base_address

    @abstractmethod
    def read(self, offset: int, size: int) -> int:
        """
        Handle a read from the device.

        Args:
            offset: Offset within the device (0 = base_address)
            size: Read size in bytes

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
            size: Write size in bytes
            value: The value being written
        """
        pass

    def reset(self):
        """Reset the device to initial state. Override if needed."""
        pass
```

### Device Registry

Create `src/god/devices/registry.py`:

```python
"""
Device registry for MMIO dispatch.

The registry maps address ranges to devices and dispatches MMIO accesses
to the appropriate handler.
"""

from .device import Device, MMIOAccess, MMIOResult


class DeviceRegistry:
    """
    Manages devices and dispatches MMIO accesses.

    Usage:
        registry = DeviceRegistry()
        registry.register(uart)
        registry.register(virtio)

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
        """
        # Check for overlaps
        for existing in self._devices:
            if self._overlaps(device, existing):
                raise ValueError(
                    f"Device {device.name} (0x{device.base_address:x}) "
                    f"overlaps with {existing.name} (0x{existing.base_address:x})"
                )

        self._devices.append(device)
        print(f"Registered device: {device.name} at 0x{device.base_address:08x}")

    def _overlaps(self, a: Device, b: Device) -> bool:
        """Check if two devices' address ranges overlap."""
        a_end = a.base_address + a.size
        b_end = b.base_address + b.size
        return not (a_end <= b.base_address or b_end <= a.base_address)

    def find_device(self, address: int) -> Device | None:
        """Find the device that handles an address."""
        for device in self._devices:
            if device.contains(address):
                return device
        return None

    def handle_mmio(self, access: MMIOAccess) -> MMIOResult:
        """
        Handle an MMIO access.

        Args:
            access: The MMIO access details.

        Returns:
            The result of handling the access.
        """
        device = self.find_device(access.address)

        if device is None:
            print(
                f"Warning: Unhandled MMIO {'write' if access.is_write else 'read'} "
                f"at 0x{access.address:08x}"
            )
            return MMIOResult(data=0, handled=False)

        offset = device.offset(access.address)

        if access.is_write:
            device.write(offset, access.size, access.data)
            return MMIOResult(handled=True)
        else:
            value = device.read(offset, access.size)
            return MMIOResult(data=value, handled=True)

    def reset_all(self):
        """Reset all devices."""
        for device in self._devices:
            device.reset()
```

## Implementation: PL011 UART

Now let's implement the PL011 UART. Create `src/god/devices/uart.py`:

```python
"""
PL011 UART emulation.

The PL011 is ARM's standard serial port. We emulate it for console I/O.
Linux uses this for "console=ttyAMA0".
"""

import sys
from typing import TextIO

from god.vm.layout import UART
from .device import Device


class PL011UART(Device):
    """
    PL011 UART (serial port) emulator.

    This provides basic serial console functionality:
    - Writes to DR output characters to the host terminal
    - Reads from FR return status (transmit ready, receive empty)

    The real PL011 has FIFOs, interrupts, DMA, and baud rate configuration.
    We skip most of that complexity - our "transmission" is instant (we just
    print to stdout), so we always report the transmit FIFO as empty.

    Usage:
        uart = PL011UART()
        registry.register(uart)
    """

    # Register offsets
    DR = 0x000      # Data Register
    RSR = 0x004     # Receive Status Register / Error Clear Register
    FR = 0x018      # Flag Register
    ILPR = 0x020    # IrDA Low-Power Counter Register (not used)
    IBRD = 0x024    # Integer Baud Rate Divisor
    FBRD = 0x028    # Fractional Baud Rate Divisor
    LCR_H = 0x02C   # Line Control Register
    CR = 0x030      # Control Register
    IFLS = 0x034    # Interrupt FIFO Level Select
    IMSC = 0x038    # Interrupt Mask Set/Clear
    RIS = 0x03C     # Raw Interrupt Status
    MIS = 0x040     # Masked Interrupt Status
    ICR = 0x044     # Interrupt Clear Register
    DMACR = 0x048   # DMA Control Register

    # Flag Register bits
    FR_TXFE = 1 << 7  # Transmit FIFO Empty
    FR_RXFF = 1 << 6  # Receive FIFO Full
    FR_TXFF = 1 << 5  # Transmit FIFO Full
    FR_RXFE = 1 << 4  # Receive FIFO Empty
    FR_BUSY = 1 << 3  # UART Busy

    # Control Register bits
    CR_UARTEN = 1 << 0  # UART Enable
    CR_TXE = 1 << 8     # Transmit Enable
    CR_RXE = 1 << 9     # Receive Enable

    def __init__(
        self,
        output: TextIO = sys.stdout,
        base_address: int | None = None,
        size: int | None = None,
    ):
        """
        Create a PL011 UART.

        Args:
            output: Where to write output (default: stdout).
                    You can pass a StringIO for testing.
            base_address: MMIO base address. Defaults to layout.UART.base.
            size: MMIO region size. Defaults to layout.UART.size.
        """
        self._output = output
        self._base_address = base_address if base_address is not None else UART.base
        self._size = size if size is not None else UART.size

        # Internal state
        self._cr = 0          # Control Register (UART disabled initially)
        self._lcr_h = 0       # Line Control Register
        self._ibrd = 0        # Integer Baud Rate
        self._fbrd = 0        # Fractional Baud Rate
        self._imsc = 0        # Interrupt Mask
        self._ris = 0         # Raw Interrupt Status

        # Receive buffer (for future input support)
        self._rx_buffer: list[int] = []

    @property
    def name(self) -> str:
        return "PL011 UART"

    @property
    def base_address(self) -> int:
        return self._base_address

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, size: int) -> int:
        """Handle a read from the UART."""

        if offset == self.DR:
            # Data Register read - return received character
            if self._rx_buffer:
                return self._rx_buffer.pop(0)
            return 0

        elif offset == self.FR:
            # Flag Register - return current status
            flags = 0
            flags |= self.FR_TXFE  # Transmit FIFO is always empty (instant send)
            if not self._rx_buffer:
                flags |= self.FR_RXFE  # Receive FIFO is empty
            return flags

        elif offset == self.RSR:
            # Receive Status Register - no errors
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
            # Data Register write - output character
            char = value & 0xFF  # Bottom 8 bits are the character
            self._output.write(chr(char))
            self._output.flush()

        elif offset == self.RSR:
            # Writing clears error flags
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

        elif offset == self.ICR:
            # Interrupt Clear Register - clear specified interrupts
            self._ris &= ~value

        # Other registers are either read-only or not important for basic operation

    def reset(self):
        """Reset the UART to initial state."""
        self._cr = 0
        self._lcr_h = 0
        self._ibrd = 0
        self._fbrd = 0
        self._imsc = 0
        self._ris = 0
        self._rx_buffer.clear()

    def inject_input(self, data: bytes):
        """
        Inject input data into the receive buffer.

        This simulates receiving data on the serial port.

        Args:
            data: Bytes to inject.
        """
        for byte in data:
            self._rx_buffer.append(byte)
```

Create `src/god/devices/__init__.py`:

```python
"""
Device emulation package.
"""

from .device import Device, MMIOAccess, MMIOResult
from .registry import DeviceRegistry
from .uart import PL011UART

__all__ = [
    "Device",
    "MMIOAccess",
    "MMIOResult",
    "DeviceRegistry",
    "PL011UART",
]
```

## Updating the Run Loop

Now we need to integrate the device registry into our run loop. Update `src/god/vcpu/runner.py`:

```python
"""
VM execution runner.

This module provides the run loop that executes guest code and handles VM exits.
"""

from god.kvm.constants import (
    KVM_EXIT_HLT,
    KVM_EXIT_MMIO,
    KVM_EXIT_SYSTEM_EVENT,
    KVM_EXIT_INTERNAL_ERROR,
    KVM_EXIT_FAIL_ENTRY,
)
from god.kvm.system import KVMSystem
from god.vm.vm import VirtualMachine
from god.devices import DeviceRegistry, MMIOAccess
from .vcpu import VCPU
from . import registers


class RunnerError(Exception):
    """Exception raised when runner encounters an error."""
    pass


class VMRunner:
    """
    Runs a virtual machine.

    This class manages the run loop and coordinates between the VM,
    vCPU, and device handlers.
    """

    def __init__(
        self,
        vm: VirtualMachine,
        kvm: KVMSystem,
        devices: DeviceRegistry | None = None,
    ):
        """
        Create a runner for a VM.

        Args:
            vm: The VirtualMachine to run.
            kvm: The KVMSystem instance.
            devices: Device registry for MMIO handling. If not provided,
                     a new empty registry is created.
        """
        self._vm = vm
        self._kvm = kvm
        self._devices = devices if devices is not None else DeviceRegistry()
        self._vcpu: VCPU | None = None

    @property
    def devices(self) -> DeviceRegistry:
        """Get the device registry."""
        return self._devices

    def create_vcpu(self) -> VCPU:
        """Create and return a vCPU."""
        if self._vcpu is not None:
            raise RunnerError("vCPU already created")

        self._vcpu = VCPU(self._vm.fd, self._kvm, vcpu_id=0)
        return self._vcpu

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
        Handle an MMIO exit.

        Returns:
            True if handled, False if unhandled.
        """
        phys_addr, data_bytes, length, is_write = vcpu.get_mmio_info()

        # Convert bytes to int for writes
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
            # For reads, we need to return the data to the guest
            result_bytes = result.data.to_bytes(length, "little")
            vcpu.set_mmio_data(result_bytes)

        return result.handled

    def run(self, max_exits: int = 100000, quiet: bool = False) -> dict:
        """
        Run the VM until it halts or hits max_exits.

        Args:
            max_exits: Maximum number of VM exits before giving up.
            quiet: If True, don't print MMIO debug messages.

        Returns:
            Dict with execution statistics.
        """
        if self._vcpu is None:
            raise RunnerError("vCPU not created - call create_vcpu() first")

        stats = {
            "exits": 0,
            "hlt": False,
            "exit_reason": None,
            "exit_counts": {},
        }

        vcpu = self._vcpu

        for _ in range(max_exits):
            # Run the vCPU
            exit_reason = vcpu.run()

            # Handle signal interruption
            if exit_reason == -1:
                continue

            stats["exits"] += 1
            exit_name = vcpu.get_exit_reason_name(exit_reason)
            stats["exit_counts"][exit_name] = stats["exit_counts"].get(exit_name, 0) + 1
            stats["exit_reason"] = exit_name

            if exit_reason == KVM_EXIT_HLT:
                stats["hlt"] = True
                break

            elif exit_reason == KVM_EXIT_MMIO:
                self._handle_mmio(vcpu)

            elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
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

## Hello World from the Guest

Now let's create a guest program that prints "Hello, World!" Create `tests/guest_code/hello.S`:

```asm
/*
 * hello.S - Hello World for our VMM
 *
 * This program prints "Hello, World!" to the PL011 UART and halts.
 */

    .global _start

    /* UART addresses */
    .equ UART_BASE, 0x09000000
    .equ UART_DR,   0x000       /* Data Register */
    .equ UART_FR,   0x018       /* Flag Register */
    .equ UART_FR_TXFF, (1 << 5) /* Transmit FIFO Full flag */

_start:
    /* Load UART base address into x1 */
    mov     x1, #0x0000
    movk    x1, #0x0900, lsl #16    /* x1 = 0x09000000 */

    /* Load message address into x2 */
    adr     x2, message

print_loop:
    /* Load next character */
    ldrb    w0, [x2], #1            /* Load byte, increment pointer */

    /* Check for null terminator */
    cbz     w0, done                 /* If zero, we're done */

wait_tx_ready:
    /* Check if transmit FIFO has room */
    ldr     w3, [x1, #UART_FR]      /* Read Flag Register */
    tst     w3, #UART_FR_TXFF       /* Test TXFF bit */
    b.ne    wait_tx_ready           /* If set (full), wait */

    /* Send the character */
    str     w0, [x1, #UART_DR]      /* Write to Data Register */

    /* Next character */
    b       print_loop

done:
    /*
     * Shutdown using PSCI (Power State Coordination Interface).
     *
     * On ARM64 KVM, neither WFI nor HLT cause a proper VM exit:
     * - WFI (Wait For Interrupt) just sleeps forever waiting for an
     *   interrupt that never comes (we haven't set up the GIC yet)
     * - HLT is a debug instruction that causes an exception, not a VM exit
     *
     * Instead, ARM64 guests use PSCI to request shutdown. PSCI is a firmware
     * interface - we make a "hypervisor call" (HVC) with the PSCI function
     * ID in x0.
     *
     * PSCI_SYSTEM_OFF = 0x84000008 (using 32-bit calling convention)
     *
     * When KVM sees this HVC, it returns KVM_EXIT_SYSTEM_EVENT to our VMM.
     */
    mov     x0, #0x0008             /* Lower 16 bits of PSCI_SYSTEM_OFF */
    movk    x0, #0x8400, lsl #16    /* x0 = 0x84000008 */
    hvc     #0                       /* Hypervisor call - triggers VM exit */

    /* Should never reach here */
    b       .

    .align 4
message:
    .asciz "Hello, World!\n"
```

### Why Not WFI or HLT?

This is an important ARM64/KVM detail that catches many developers:

| Instruction | What Happens on KVM |
|-------------|---------------------|
| `WFI` (Wait For Interrupt) | Guest sleeps forever - **no VM exit!** KVM just waits for an interrupt that never comes. |
| `HLT` (Debug Halt) | Causes a debug exception, not a VM exit. Used for debugging, not normal shutdown. |
| `PSCI SYSTEM_OFF` | Proper shutdown. KVM recognizes the HVC call and returns `KVM_EXIT_SYSTEM_EVENT`. |

This is why our `simple.S` from Chapter 3 also uses PSCI - it's the standard way for ARM64 guests to request shutdown.

## Updating the CLI

Update `src/god/cli.py` to use the UART:

```python
@app.command("run")
def run_binary(
    binary: str = typer.Argument(..., help="Path to the binary to run"),
    entry: str = typer.Option(
        "0x40080000",
        "--entry",
        "-e",
        help="Entry point address (hex)",
    ),
    ram_mb: int = typer.Option(
        64,
        "--ram",
        "-r",
        help="RAM size in megabytes",
    ),
    with_uart: bool = typer.Option(
        True,
        "--uart/--no-uart",
        help="Enable UART emulation",
    ),
):
    """
    Run a binary in the VM.

    Loads the binary at the entry point and runs until it halts.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vcpu.runner import VMRunner, RunnerError
    from god.vcpu import registers
    from god.devices import DeviceRegistry, PL011UART

    # Parse entry point
    entry_point = int(entry, 16) if entry.startswith("0x") else int(entry)

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                # Set up devices
                devices = DeviceRegistry()

                if with_uart:
                    uart = PL011UART()
                    devices.register(uart)

                runner = VMRunner(vm, kvm, devices)
                vcpu = runner.create_vcpu()

                # Set initial register state
                vcpu.set_pc(entry_point)
                stack_top = vm.ram_base + vm.ram_size
                vcpu.set_sp(stack_top)

                pstate = (
                    registers.PSTATE_MODE_EL1H |
                    registers.PSTATE_A |
                    registers.PSTATE_I |
                    registers.PSTATE_F
                )
                vcpu.set_pstate(pstate)

                print(f"PC = 0x{entry_point:016x}")
                print(f"SP = 0x{stack_top:016x}")
                print()

                # Load the binary
                runner.load_binary(binary, entry_point)

                # Run!
                print("=" * 60)
                print("Guest output:")
                print("-" * 60)

                stats = runner.run(quiet=True)

                print("-" * 60)
                print()
                print(f"Guest {'halted' if stats['hlt'] else 'stopped'} "
                      f"after {stats['exits']} exits")

    except (KVMError, VMError, RunnerError) as e:
        print(f"\nError: {e}")
        raise typer.Exit(code=1)
```

## Testing

Build and run the hello world program:

```bash
cd ~/workplace/veleiro-god

# Assemble the hello.S we already have (with PSCI shutdown)
aarch64-linux-gnu-as -o tests/guest_code/hello.o tests/guest_code/hello.S
aarch64-linux-gnu-ld -nostdlib -static -Ttext=0x40080000 \
    -o tests/guest_code/hello tests/guest_code/hello.o
aarch64-linux-gnu-objcopy -O binary tests/guest_code/hello tests/guest_code/hello.bin

# Run it!
sudo uv run god run tests/guest_code/hello.bin
```

Expected output:

```
Creating VM with 64 MB RAM...
Registered device: PL011 UART at 0x09000000
PC = 0x0000000040080000
SP = 0x0000000044000000

Loaded 79 bytes at 0x40080000
============================================================
Guest output:
------------------------------------------------------------
Hello, World!
------------------------------------------------------------

Guest stopped after 29 exits
```

Note: It says "stopped" not "halted" because PSCI SYSTEM_OFF causes `KVM_EXIT_SYSTEM_EVENT`, not `KVM_EXIT_HLT`. Both mean the guest terminated successfully.

**We did it!** Our virtual machine just printed "Hello, World!"

## Deep Dive: UART Internals

### Baud Rate (Does It Matter?)

On real hardware, the **baud rate** determines how fast bits are transmitted. Common rates are 9600, 115200, etc. The IBRD and FBRD registers set the baud rate divisor.

In our emulator, **baud rate doesn't matter**. We're not transmitting real electrical signals—we're just capturing writes and printing them. We accept any baud rate configuration but ignore it.

### FIFO Buffers

Real UARTs have **FIFOs (First-In-First-Out buffers)** to smooth out differences between CPU speed and transmission speed. The CPU can write multiple bytes quickly, and the UART transmits them gradually.

Our emulator has instant transmission—writes appear immediately. We always report the transmit FIFO as empty (FR_TXFE set), meaning "ready for more data."

### Flow Control

Hardware flow control uses signals like CTS (Clear To Send) and RTS (Request To Send) to prevent data loss. We ignore these for simplicity.

## Adding Input Support (Preview)

Our UART can output but not yet receive input. To add input:

1. Read from the host's stdin (in a non-blocking way)
2. Inject characters into the UART's receive buffer
3. Set the RXFE flag appropriately
4. Optionally raise an interrupt

We'll fully implement this when we add interrupt support in Chapter 5.

## Gotchas

### Byte Order

MMIO accesses come as raw bytes. When converting to integers:
- ARM64 is little-endian by default
- A 32-bit write of 0x48 comes as `[0x48, 0x00, 0x00, 0x00]`

We use `int.from_bytes(data, "little")` to handle this correctly.

### Blocking on TXFF

Some code loops waiting for TXFF to clear:

```c
while (UART_FR & TXFF) ; // Wait until not full
UART_DR = char;
```

If we always report TXFF as clear (FIFO never full), this works fine. If we ever reported TXFF as set, the guest would spin forever waiting.

### Missing Register Accesses

If the guest accesses a register we don't handle, we return 0 for reads and ignore writes. This works for most cases, but some code might misbehave if it gets unexpected values.

## Performance Note

> **Performance**: Every single character causes a VM exit. Printing "Hello, World!" causes 14 exits (one per character, plus extras for flag register checks).
>
> **Why this is slow**: VM exits are expensive—thousands of CPU cycles each. A production VMM would use `virtio-console` which batches characters, reducing exits dramatically.
>
> **Why we did it this way**: PL011 is simpler to implement and teaches MMIO emulation fundamentals. We'll add virtio-console in Chapter 7.

## What's Next?

In this chapter, we:

1. Learned how MMIO device emulation works
2. Created a device framework with base classes and registry
3. Implemented the PL011 UART
4. Saw "Hello, World!" from our guest!

In the next chapter, we'll set up the interrupt controller so our guest can handle asynchronous events. This is essential for keyboard input, timers, and other devices that need to notify the CPU.

[Continue to Chapter 5: The Interrupt Controller →](05-interrupt-controller.md)
