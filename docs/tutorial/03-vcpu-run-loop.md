# Chapter 3: Virtual CPUs and the Run Loop

In this chapter, we'll create a virtual CPU and run actual guest code. This is where the magic happens—we'll see code execute inside our virtual machine for the first time!

## What is a vCPU?

A **vCPU (Virtual CPU)** is an abstraction of a processor that the guest sees and uses. From the guest's perspective, it's a real CPU with registers, an instruction pointer, and the ability to execute code.

From our perspective as the VMM, a vCPU is:
- A KVM object with a file descriptor
- A set of registers we can read and write
- Something we tell to "run" and it executes guest code

### vCPU State

A vCPU has state that includes:

1. **General-purpose registers**: x0-x30 on ARM64
2. **Special registers**:
   - PC (Program Counter) - address of next instruction
   - SP (Stack Pointer)
   - PSTATE (Processor State) - condition flags, interrupt masks, etc.
3. **System registers**: Control registers like SCTLR_EL1 that configure the CPU
4. **Floating-point/SIMD registers**: For math and vector operations

When the guest is running, these hold the guest's values. When we handle a VM exit, we can read and modify them.

### The kvm_run Structure

When we create a vCPU, KVM gives us a special shared memory region called **kvm_run**. This structure is shared between the kernel and our userspace VMM.

When the guest exits (stops executing), kvm_run tells us:
- **exit_reason**: Why the guest stopped
- Exit-specific data: Details about the exit (like MMIO address and data)

When we want to resume the guest, we call `KVM_RUN` and the kernel reads kvm_run for any data we want to pass in.

## ARM64 Register Overview

Let's understand ARM64's register layout, since we'll need to set registers to run our guest code.

### General-Purpose Registers

ARM64 has 31 general-purpose registers, named x0 through x30:

```
┌─────┬───────────────────────────────────────────────────┐
│ x0  │  Argument 1 / Return value                        │
│ x1  │  Argument 2                                       │
│ x2  │  Argument 3                                       │
│ x3  │  Argument 4                                       │
│ x4  │  Argument 5                                       │
│ x5  │  Argument 6                                       │
│ x6  │  Argument 7                                       │
│ x7  │  Argument 8                                       │
│ x8  │  Indirect result location / syscall number        │
├─────┼───────────────────────────────────────────────────┤
│x9-15│  Temporary / caller-saved                         │
├─────┼───────────────────────────────────────────────────┤
│x16  │  IP0 - Intra-procedure scratch register           │
│x17  │  IP1 - Intra-procedure scratch register           │
│x18  │  Platform register (reserved on some OSes)        │
├─────┼───────────────────────────────────────────────────┤
│x19-28 Callee-saved (preserved across function calls)    │
├─────┼───────────────────────────────────────────────────┤
│x29  │  FP - Frame pointer                               │
│x30  │  LR - Link register (return address)              │
└─────┴───────────────────────────────────────────────────┘
```

Note: Each register can also be accessed as a 32-bit register (w0-w30).

### Special Registers

**SP (Stack Pointer)**: Points to the top of the stack. Actually there are multiple stack pointers:
- SP_EL0: Stack pointer for EL0 (user mode)
- SP_EL1: Stack pointer for EL1 (kernel mode)
- etc.

**PC (Program Counter)**: Address of the next instruction to execute. On ARM64, you can't write to PC directly in code—branches change it.

**PSTATE (Processor State)**: Contains:
- N, Z, C, V: Condition flags from arithmetic operations
- I, F, A: Interrupt masks (IRQ, FIQ, Async abort)
- EL: Current exception level
- SP: Which stack pointer to use

### System Registers

ARM64 has many system registers that control the CPU. A few important ones:

**SCTLR_EL1** (System Control Register): Controls:
- MMU enable/disable
- Cache enable/disable
- Endianness

**TTBR0_EL1 / TTBR1_EL1**: Translation Table Base Registers (page table pointers)

**VBAR_EL1**: Vector Base Address Register (where exception vectors are)

For our initial testing, we won't mess with most of these—KVM initializes them to reasonable defaults.

## Creating a vCPU

### The KVM_CREATE_VCPU ioctl

Creating a vCPU is simple:

```python
vcpu_fd = ioctl(vm_fd, KVM_CREATE_VCPU, vcpu_id)
```

The `vcpu_id` is just a number (0 for the first CPU, 1 for the second, etc.).

### Mapping kvm_run

After creating the vCPU, we need to mmap a shared memory region for the kvm_run structure:

```python
# Get the required size from KVM
mmap_size = ioctl(kvm_fd, KVM_GET_VCPU_MMAP_SIZE)

# mmap the region from the vCPU file descriptor
kvm_run = mmap(
    addr=None,
    length=mmap_size,
    prot=PROT_READ | PROT_WRITE,
    flags=MAP_SHARED,  # Important: SHARED, not PRIVATE
    fd=vcpu_fd,
    offset=0,
)
```

We use `MAP_SHARED` because both the kernel and our VMM need to see each other's writes to this memory. When KVM runs the guest and it exits, the kernel writes the `exit_reason` to this structure. With `MAP_PRIVATE`, we'd get our own copy of the memory and never see the kernel's updates.

This is different from guest RAM which uses `MAP_PRIVATE`. For guest RAM, the kernel maps our memory into the guest's address space, but we don't need to see real-time updates from the kernel—we just need to provide the backing memory. The kernel reads/writes through the guest's page tables, not through our mapping.

### ARM64 vCPU Initialization

On ARM64, we need to initialize the vCPU with a specific target configuration before we can use it. This is a two-step process:

```python
# First, allocate a kvm_vcpu_init structure
init = ffi.new("struct kvm_vcpu_init *")

# Ask KVM what CPU target to use for this host
# This call is made on the VM fd (not /dev/kvm or the vCPU fd)
result = ioctl(vm_fd, KVM_ARM_PREFERRED_TARGET, init)

# KVM has now filled in init.target with the appropriate value
# On most modern ARM64 systems, this will be KVM_ARM_TARGET_GENERIC_V8

# Enable PSCI 0.2 support - this is critical!
# PSCI (Power State Coordination Interface) allows the guest to
# request system shutdown/reset via HVC calls
KVM_ARM_VCPU_PSCI_0_2 = 2  # Feature bit index
init.features[0] |= (1 << KVM_ARM_VCPU_PSCI_0_2)

# Now initialize the vCPU with this configuration
result = ioctl(vcpu_fd, KVM_ARM_VCPU_INIT, init)
```

The `kvm_vcpu_init` structure has two fields:
- `target`: The CPU type (e.g., `KVM_ARM_TARGET_GENERIC_V8`). KVM fills this in based on your host hardware.
- `features[7]`: A bitmask of features to enable. We must enable `KVM_ARM_VCPU_PSCI_0_2` (bit 2) to allow the guest to shut down properly.

Without PSCI enabled, the guest has no clean way to exit—it would have to rely on MMIO or other hacks.

## The kvm_run Structure

Let's look at the kvm_run structure that we share with the kernel. Understanding its layout is important because we'll read from it directly using pointer arithmetic:

```c
struct kvm_run {
    /* in (offset 0-7) */
    __u8 request_interrupt_window;  // offset 0
    __u8 immediate_exit;            // offset 1
    __u8 padding1[6];               // offset 2-7

    /* out (offset 8-15) */
    __u32 exit_reason;              // offset 8 (we read this!)
    __u8 ready_for_interrupt_injection;
    __u8 if_flag;
    __u16 flags;

    /* in/out (offset 16-31) */
    __u64 cr8;                      // offset 16
    __u64 apic_base;                // offset 24

    /* Exit-specific data (offset 32+) */
    union {
        /* KVM_EXIT_MMIO (at offset 32) */
        struct {
            __u64 phys_addr;   // +0  (offset 32)
            __u8  data[8];     // +8  (offset 40)
            __u32 len;         // +16 (offset 48)
            __u8  is_write;    // +20 (offset 52)
        } mmio;

        /* KVM_EXIT_HYPERCALL */
        struct {
            __u64 nr;
            __u64 args[6];
            __u64 ret;
            __u32 longmode;
            __u32 pad;
        } hypercall;

        /* ... other exit types ... */
    };
};
```

The key fields are:
- `exit_reason` at offset 8: Why the guest stopped (KVM_EXIT_HLT, KVM_EXIT_MMIO, etc.)
- The exit union at offset 32: Exit-specific data depending on the exit reason

We don't define this structure in our cffi bindings because the exact layout varies by architecture. Instead, we access fields directly using pointer offsets.

## The Run Loop - Heart of the VMM

The **run loop** is the core of any VMM. It's a simple concept:

1. Tell the vCPU to run (`KVM_RUN`)
2. Guest executes until something happens
3. Check why it stopped (`exit_reason`)
4. Handle the exit
5. Go back to step 1

```python
while True:
    # Tell KVM to run the guest
    ioctl(vcpu_fd, KVM_RUN)

    # Guest ran until something happened - check why
    exit_reason = kvm_run.exit_reason

    if exit_reason == KVM_EXIT_HLT:
        # Guest executed HLT - it's done
        print("Guest halted")
        break

    elif exit_reason == KVM_EXIT_MMIO:
        # Guest accessed a device address
        handle_mmio(kvm_run.mmio)

    elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
        # Guest requested shutdown/reset
        print("Guest requested shutdown")
        break

    elif exit_reason == KVM_EXIT_INTERNAL_ERROR:
        # Something went wrong
        print("KVM internal error!")
        break

    else:
        print(f"Unhandled exit reason: {exit_reason}")
        break
```

### Exit Reasons We Care About

**KVM_EXIT_MMIO (6)**: The guest tried to read from or write to a memory address that isn't RAM—it's a device address. We need to emulate the device access.

**KVM_EXIT_SYSTEM_EVENT (24)**: The guest requested a system event like shutdown or reset through PSCI. **This is how we'll halt our guest on ARM64.**

**KVM_EXIT_INTERNAL_ERROR (17)**: KVM encountered an internal error. This usually means we set something up wrong.

**KVM_EXIT_FAIL_ENTRY (9)**: The CPU failed to enter guest mode. Usually indicates incorrect vCPU state.

### Why Not WFI or HLT?

You might expect to use the `WFI` (Wait For Interrupt) or `HLT` instruction to halt the guest. However:

- **WFI** on ARM64 KVM does **not** cause a VM exit. It just puts the vCPU to sleep waiting for an interrupt that never comes (since we haven't set up an interrupt controller). Your VMM would hang forever.

- **HLT** on ARM64 is a debug halt instruction that triggers a Debug exception, not a VM exit.

Instead, ARM64 guests use **PSCI (Power State Coordination Interface)** to request shutdown:

```asm
mov     x0, #0x0008             /* PSCI_SYSTEM_OFF lower bits */
movk    x0, #0x8400, lsl #16    /* x0 = 0x84000008 */
hvc     #0                       /* Call hypervisor */
```

The `HVC` (Hypervisor Call) instruction traps to KVM, which recognizes the PSCI SYSTEM_OFF function ID and returns `KVM_EXIT_SYSTEM_EVENT` to our VMM.

## Setting Registers

To run guest code, we need to set at least:
- **PC**: The address where execution should start
- **PSTATE**: Processor state (exception level, interrupt masks, etc.)

On ARM64, we set registers using `KVM_GET_ONE_REG` and `KVM_SET_ONE_REG`:

```python
struct kvm_one_reg {
    __u64 id;    // Register identifier
    __u64 addr;  // Pointer to value
};
```

The register ID encodes which register we're accessing. ARM64 register IDs look like:

```
KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE | offset
```

Where `offset` is the offset within the `kvm_regs` structure.

## Implementation: vCPU Class

Let's implement the vCPU. First, update our bindings in `src/god/kvm/bindings.py`:

```python
# Add to the ffi.cdef():
ffi.cdef("""
    // ... previous definitions ...

    // vCPU initialization structure for ARM64
    struct kvm_vcpu_init {
        uint32_t target;
        uint32_t features[7];
    };

    // Single register access
    struct kvm_one_reg {
        uint64_t id;
        uint64_t addr;
    };

    // The kvm_run structure (simplified - we'll access specific fields)
    // The full structure is large, we define what we need
""")
```

Now create `src/god/vcpu/registers.py`:

```python
"""
ARM64 register definitions for KVM.

This module defines register IDs used with KVM_GET_ONE_REG and KVM_SET_ONE_REG.
The IDs encode information about the register type, size, and location.
"""

# Register type flags
KVM_REG_ARM64 = 0x6000000000000000  # ARM64 register space
KVM_REG_ARM_CORE = 0x0010 << 16     # Core registers (x0-x30, PC, SP, PSTATE)

# Size flags
KVM_REG_SIZE_U32 = 0x0020000000000000  # 32-bit register
KVM_REG_SIZE_U64 = 0x0030000000000000  # 64-bit register

# Base for core registers
_CORE_REG_BASE = KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE


def _core_reg(offset: int) -> int:
    """
    Create a register ID for a core register.

    The offset is in terms of __u64 array indices in the kernel's
    struct kvm_regs.
    """
    return _CORE_REG_BASE | (offset * 2)  # *2 because indices are 32-bit in struct


# General-purpose registers (x0-x30)
# These are stored in regs[0..30] in struct kvm_regs
X0 = _core_reg(0)
X1 = _core_reg(1)
X2 = _core_reg(2)
X3 = _core_reg(3)
X4 = _core_reg(4)
X5 = _core_reg(5)
X6 = _core_reg(6)
X7 = _core_reg(7)
X8 = _core_reg(8)
X9 = _core_reg(9)
X10 = _core_reg(10)
X11 = _core_reg(11)
X12 = _core_reg(12)
X13 = _core_reg(13)
X14 = _core_reg(14)
X15 = _core_reg(15)
X16 = _core_reg(16)
X17 = _core_reg(17)
X18 = _core_reg(18)
X19 = _core_reg(19)
X20 = _core_reg(20)
X21 = _core_reg(21)
X22 = _core_reg(22)
X23 = _core_reg(23)
X24 = _core_reg(24)
X25 = _core_reg(25)
X26 = _core_reg(26)
X27 = _core_reg(27)
X28 = _core_reg(28)
X29 = _core_reg(29)  # Frame pointer
X30 = _core_reg(30)  # Link register

# List of all X registers for iteration
X_REGISTERS = [
    X0, X1, X2, X3, X4, X5, X6, X7, X8, X9,
    X10, X11, X12, X13, X14, X15, X16, X17, X18, X19,
    X20, X21, X22, X23, X24, X25, X26, X27, X28, X29, X30,
]

# Stack pointer
SP = _core_reg(31)

# Program counter
PC = _core_reg(32)

# Processor state (PSTATE)
# This includes exception level, interrupt masks, condition flags
PSTATE = _core_reg(33)

# PSTATE bits
PSTATE_MODE_EL1H = 0x05   # EL1 with SP_EL1 (kernel mode)
PSTATE_MODE_EL0T = 0x00   # EL0 with SP_EL0 (user mode)
PSTATE_F = 1 << 6         # FIQ mask
PSTATE_I = 1 << 7         # IRQ mask
PSTATE_A = 1 << 8         # Async abort mask
PSTATE_D = 1 << 9         # Debug mask


# Register names for debugging
REGISTER_NAMES = {
    **{_core_reg(i): f"x{i}" for i in range(31)},
    SP: "sp",
    PC: "pc",
    PSTATE: "pstate",
}


def get_register_name(reg_id: int) -> str:
    """Get a human-readable name for a register ID."""
    return REGISTER_NAMES.get(reg_id, f"unknown(0x{reg_id:x})")
```

Now create `src/god/vcpu/vcpu.py`:

```python
"""
Virtual CPU management.

This module provides the vCPU class that represents a virtual processor.
"""

from god.kvm.bindings import ffi, lib, get_errno
from god.kvm.constants import (
    KVM_CREATE_VCPU,
    KVM_RUN,
    KVM_GET_ONE_REG,
    KVM_SET_ONE_REG,
    KVM_ARM_VCPU_INIT,
    KVM_ARM_PREFERRED_TARGET,
    PROT_READ,
    PROT_WRITE,
    MAP_SHARED,
    KVM_EXIT_HLT,
    KVM_EXIT_MMIO,
    KVM_EXIT_SYSTEM_EVENT,
    KVM_EXIT_INTERNAL_ERROR,
    KVM_EXIT_FAIL_ENTRY,
    EXIT_REASON_NAMES,
)
from god.kvm.system import KVMSystem
from . import registers


class VCPUError(Exception):
    """Exception raised when vCPU operations fail."""
    pass


class VCPU:
    """
    Represents a virtual CPU.

    A vCPU is a virtualized processor that can execute guest code.
    We can set its registers, run it, and handle exits.

    Usage:
        vcpu = VCPU(vm, kvm, vcpu_id=0)
        vcpu.set_pc(entry_point)
        while True:
            exit_reason = vcpu.run()
            if exit_reason == KVM_EXIT_HLT:
                break
    """

    def __init__(self, vm_fd: int, kvm: KVMSystem, vcpu_id: int = 0):
        """
        Create a new vCPU.

        Args:
            vm_fd: The VM file descriptor.
            kvm: The KVMSystem instance (for getting mmap size).
            vcpu_id: The vCPU ID (0 for first CPU, 1 for second, etc.).
        """
        self._vm_fd = vm_fd
        self._kvm = kvm
        self._vcpu_id = vcpu_id
        self._fd = -1
        self._kvm_run = None
        self._kvm_run_size = 0
        self._closed = False

        # Create the vCPU
        # Note: vcpu_id must be cast to int for cffi's variadic ioctl
        self._fd = lib.ioctl(vm_fd, KVM_CREATE_VCPU, ffi.cast("int", vcpu_id))
        if self._fd < 0:
            raise VCPUError(f"Failed to create vCPU {vcpu_id}: errno {get_errno()}")

        # Get the mmap size for kvm_run
        self._kvm_run_size = kvm.get_vcpu_mmap_size()

        # mmap the kvm_run structure
        # This is shared memory between kernel and userspace
        kvm_run_ptr = lib.mmap(
            ffi.NULL,
            self._kvm_run_size,
            PROT_READ | PROT_WRITE,
            MAP_SHARED,  # SHARED because it's shared with kernel
            self._fd,
            0,
        )

        if kvm_run_ptr == ffi.cast("void *", -1):
            lib.close(self._fd)
            raise VCPUError(f"Failed to mmap kvm_run: errno {get_errno()}")

        self._kvm_run = kvm_run_ptr

        # Initialize the vCPU for ARM64
        self._init_arm64()

    def _init_arm64(self):
        """Initialize the vCPU with ARM64-specific settings."""
        # Get the preferred target for this host
        # Note: This ioctl is called on the VM fd, not /dev/kvm
        init = ffi.new("struct kvm_vcpu_init *")

        result = lib.ioctl(self._vm_fd, KVM_ARM_PREFERRED_TARGET, init)
        if result < 0:
            raise VCPUError(
                f"Failed to get preferred target: errno {get_errno()}"
            )

        # Enable PSCI 0.2 support
        # This allows the guest to request shutdown via HVC calls
        KVM_ARM_VCPU_PSCI_0_2 = 2
        init.features[0] |= (1 << KVM_ARM_VCPU_PSCI_0_2)

        # Initialize the vCPU with that target
        result = lib.ioctl(self._fd, KVM_ARM_VCPU_INIT, init)
        if result < 0:
            raise VCPUError(f"Failed to initialize vCPU: errno {get_errno()}")

    @property
    def fd(self) -> int:
        """Get the vCPU file descriptor."""
        if self._fd < 0:
            raise VCPUError("vCPU is closed")
        return self._fd

    def get_register(self, reg_id: int) -> int:
        """
        Get the value of a register.

        Args:
            reg_id: The register ID (from registers module).

        Returns:
            The register value as an integer.
        """
        # Allocate space for the value
        value = ffi.new("uint64_t *")

        # Build the request
        reg = ffi.new("struct kvm_one_reg *")
        reg.id = reg_id
        reg.addr = int(ffi.cast("uintptr_t", value))

        result = lib.ioctl(self._fd, KVM_GET_ONE_REG, reg)
        if result < 0:
            raise VCPUError(
                f"Failed to get register {registers.get_register_name(reg_id)}: "
                f"errno {get_errno()}"
            )

        return value[0]

    def set_register(self, reg_id: int, value: int):
        """
        Set the value of a register.

        Args:
            reg_id: The register ID (from registers module).
            value: The value to set.
        """
        # Store the value
        value_ptr = ffi.new("uint64_t *")
        value_ptr[0] = value

        # Build the request
        reg = ffi.new("struct kvm_one_reg *")
        reg.id = reg_id
        reg.addr = int(ffi.cast("uintptr_t", value_ptr))

        result = lib.ioctl(self._fd, KVM_SET_ONE_REG, reg)
        if result < 0:
            raise VCPUError(
                f"Failed to set register {registers.get_register_name(reg_id)}: "
                f"errno {get_errno()}"
            )

    def get_pc(self) -> int:
        """Get the program counter."""
        return self.get_register(registers.PC)

    def set_pc(self, value: int):
        """Set the program counter."""
        self.set_register(registers.PC, value)

    def get_sp(self) -> int:
        """Get the stack pointer."""
        return self.get_register(registers.SP)

    def set_sp(self, value: int):
        """Set the stack pointer."""
        self.set_register(registers.SP, value)

    def get_pstate(self) -> int:
        """Get the processor state register."""
        return self.get_register(registers.PSTATE)

    def set_pstate(self, value: int):
        """Set the processor state register."""
        self.set_register(registers.PSTATE, value)

    def run(self) -> int:
        """
        Run the vCPU until it exits.

        Returns:
            The exit reason (KVM_EXIT_*).
        """
        # Note: We must cast the 0 to int for cffi's variadic ioctl
        result = lib.ioctl(self._fd, KVM_RUN, ffi.cast("int", 0))
        if result < 0:
            errno = get_errno()
            # EINTR (4) is expected if we received a signal
            if errno == 4:
                # Signal interrupted - return a special value
                return -1
            raise VCPUError(f"KVM_RUN failed: errno {errno}")

        # Get exit reason from kvm_run structure
        # The exit_reason is at offset 8 in the structure (after 8 bytes of input fields)
        exit_reason = ffi.cast("uint32_t *", self._kvm_run + 8)[0]
        return exit_reason

    def get_exit_reason_name(self, exit_reason: int) -> str:
        """Get a human-readable name for an exit reason."""
        return EXIT_REASON_NAMES.get(exit_reason, f"UNKNOWN({exit_reason})")

    def get_mmio_info(self) -> tuple[int, bytes, int, bool]:
        """
        Get MMIO exit information.

        Call this after run() returns KVM_EXIT_MMIO.

        Returns:
            Tuple of (phys_addr, data, length, is_write)
        """
        # The exit union starts at offset 32 in kvm_run
        # MMIO struct layout:
        #   offset 32 (+0):  __u64 phys_addr
        #   offset 40 (+8):  __u8 data[8]
        #   offset 48 (+16): __u32 len
        #   offset 52 (+20): __u8 is_write
        mmio_base = self._kvm_run + 32

        phys_addr = ffi.cast("uint64_t *", mmio_base)[0]
        data = bytes(ffi.cast("uint8_t *", mmio_base + 8)[i] for i in range(8))
        length = ffi.cast("uint32_t *", mmio_base + 16)[0]
        is_write = bool(ffi.cast("uint8_t *", mmio_base + 20)[0])

        return phys_addr, data[:length], length, is_write

    def set_mmio_data(self, data: bytes):
        """
        Set data for an MMIO read response.

        Call this before run() after handling an MMIO read.
        """
        # data field is at offset 32 + 8 = 40
        mmio_data_ptr = ffi.cast("uint8_t *", self._kvm_run + 32 + 8)
        for i, byte in enumerate(data[:8]):
            mmio_data_ptr[i] = byte

    def dump_registers(self):
        """Print all general-purpose registers (for debugging)."""
        print("vCPU Registers:")
        print("-" * 40)

        # General-purpose registers
        for i in range(31):
            reg_id = registers.X_REGISTERS[i]
            value = self.get_register(reg_id)
            print(f"  x{i:2d} = 0x{value:016x}")

        # Special registers
        print()
        print(f"  sp  = 0x{self.get_sp():016x}")
        print(f"  pc  = 0x{self.get_pc():016x}")
        print(f"  pstate = 0x{self.get_pstate():016x}")

    def close(self):
        """Close the vCPU and free resources."""
        if self._closed:
            return

        self._closed = True

        if self._kvm_run is not None:
            lib.munmap(self._kvm_run, self._kvm_run_size)
            self._kvm_run = None

        if self._fd >= 0:
            lib.close(self._fd)
            self._fd = -1

    def __del__(self):
        """Clean up when garbage collected."""
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
```

Create `src/god/vcpu/__init__.py`:

```python
"""
Virtual CPU management package.
"""

from .vcpu import VCPU, VCPUError
from . import registers

__all__ = ["VCPU", "VCPUError", "registers"]
```

## Our First Guest Program

Now let's create a simple guest program to test our vCPU. This will be ARM64 assembly that:
1. Writes a value to a memory location
2. Requests shutdown via PSCI

### The Assembly Code

Create `tests/guest_code/simple.S`:

```asm
/*
 * simple.S - Our first guest program
 *
 * This program:
 * 1. Loads the value 0xDEADBEEF into x0
 * 2. Loads an address into x1
 * 3. Stores x0 at that address
 * 4. Requests shutdown via PSCI
 *
 * After this runs, we can verify that 0xDEADBEEF was written to memory.
 */

    .global _start

_start:
    /* Load 0xDEADBEEF into x0 */
    /* ARM64 can't load large immediates in one instruction, so we use
     * mov to load the lower 16 bits, then movk (move keep) to load
     * the upper bits without clearing what we already set */
    mov     x0, #0xBEEF             /* x0 = 0x000000000000BEEF */
    movk    x0, #0xDEAD, lsl #16    /* x0 = 0x00000000DEADBEEF */

    /* Load address 0x40001000 into x1 */
    /* This is in our RAM region (RAM starts at 0x40000000) */
    mov     x1, #0x1000
    movk    x1, #0x4000, lsl #16

    /* Store x0 at the address in x1 */
    str     x0, [x1]

    /*
     * Shutdown using PSCI
     *
     * PSCI (Power State Coordination Interface) is the standard way
     * for ARM guests to request power state changes from the hypervisor.
     *
     * PSCI_SYSTEM_OFF = 0x84000008 (32-bit calling convention)
     * We put this in x0 and execute HVC #0 (hypervisor call)
     */
    mov     x0, #0x0008             /* Lower bits of PSCI_SYSTEM_OFF */
    movk    x0, #0x8400, lsl #16    /* x0 = 0x84000008 */
    hvc     #0                       /* Call hypervisor - exits VM */

    /* Should never reach here */
    b       .
```

### Building the Guest Program

Inside the Lima VM, use the ARM64 build tools to assemble and link:

```bash
# Create the directory
mkdir -p tests/guest_code

# Assemble and link
# -nostdlib: Don't link standard library
# -static: Static binary
# -Ttext=0x40080000: Put code at RAM + 0x80000 (where kernel would go)
aarch64-linux-gnu-as -o tests/guest_code/simple.o tests/guest_code/simple.S
aarch64-linux-gnu-ld -nostdlib -static -Ttext=0x40080000 \
    -o tests/guest_code/simple tests/guest_code/simple.o

# Extract just the raw binary (no ELF headers)
aarch64-linux-gnu-objcopy -O binary tests/guest_code/simple tests/guest_code/simple.bin
```

Let's verify the disassembly looks right:

```bash
$ aarch64-linux-gnu-objdump -d tests/guest_code/simple

tests/guest_code/simple:     file format elf64-littleaarch64

Disassembly of section .text:

0000000040080000 <_start>:
    40080000:   d297dde0    mov     x0, #0xbeef
    40080004:   f2bbd5a0    movk    x0, #0xdead, lsl #16
    40080008:   d2820001    mov     x1, #0x1000
    4008000c:   f2a80001    movk    x1, #0x4000, lsl #16
    40080010:   f9000020    str     x0, [x1]
    40080014:   d2800100    mov     x0, #0x8
    40080018:   f2b08000    movk    x0, #0x8400, lsl #16
    4008001c:   d4000002    hvc     #0x0
    40080020:   14000000    b       40080020 <_start+0x20>
```

Perfect! 9 instructions × 4 bytes = 36 bytes.

## Implementation: The Run Loop

Now let's implement a simple runner that loads our test program and executes it. Create `src/god/vcpu/runner.py`:

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
from god.vm.vm import VirtualMachine
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

    Usage:
        runner = VMRunner(vm)
        runner.load_binary("/path/to/binary", entry_point)
        runner.run()
    """

    def __init__(self, vm: VirtualMachine, kvm):
        """
        Create a runner for a VM.

        Args:
            vm: The VirtualMachine to run.
            kvm: The KVMSystem instance.
        """
        self._vm = vm
        self._kvm = kvm
        self._vcpu: VCPU | None = None

    def create_vcpu(self) -> VCPU:
        """Create and return a vCPU."""
        if self._vcpu is not None:
            raise RunnerError("vCPU already created")

        self._vcpu = VCPU(self._vm.fd, self._kvm, vcpu_id=0)
        return self._vcpu

    def load_binary(self, path: str, entry_point: int):
        """
        Load a binary file into guest memory.

        Args:
            path: Path to the binary file.
            entry_point: Guest address where the binary should be loaded.
        """
        size = self._vm.memory.load_file(entry_point, path)
        print(f"Loaded {size} bytes at 0x{entry_point:08x}")

    def run(self, max_exits: int = 1000) -> dict:
        """
        Run the VM until it halts or hits max_exits.

        Args:
            max_exits: Maximum number of VM exits before giving up.

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
                # Guest halted - we're done
                stats["hlt"] = True
                break

            elif exit_reason == KVM_EXIT_MMIO:
                # Guest accessed device memory
                phys_addr, data, length, is_write = vcpu.get_mmio_info()

                if is_write:
                    print(
                        f"MMIO write: addr=0x{phys_addr:08x} "
                        f"data={data.hex()} len={length}"
                    )
                else:
                    print(
                        f"MMIO read: addr=0x{phys_addr:08x} len={length}"
                    )
                    # Return zeros for now
                    vcpu.set_mmio_data(bytes(length))

            elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
                print("Guest requested shutdown/reset")
                break

            elif exit_reason == KVM_EXIT_INTERNAL_ERROR:
                print("KVM internal error!")
                vcpu.dump_registers()
                raise RunnerError("KVM internal error")

            elif exit_reason == KVM_EXIT_FAIL_ENTRY:
                print("Failed to enter guest mode!")
                vcpu.dump_registers()
                raise RunnerError("Entry to guest mode failed")

            else:
                print(f"Unhandled exit: {exit_name}")
                vcpu.dump_registers()
                break

        return stats
```

## Adding a CLI Command

Let's add a command to run our test. Update `src/god/cli.py`:

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
):
    """
    Run a binary in the VM.

    Loads the binary at the entry point and runs until it halts.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vcpu.runner import VMRunner, RunnerError
    from god.vcpu import registers

    # Parse entry point
    entry_point = int(entry, 16) if entry.startswith("0x") else int(entry)

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                print(f"VM created: fd={vm.fd}")

                runner = VMRunner(vm, kvm)
                vcpu = runner.create_vcpu()
                print(f"vCPU created: fd={vcpu.fd}")

                # Set initial register state
                # PC = entry point
                vcpu.set_pc(entry_point)

                # SP = top of RAM (grows down)
                stack_top = vm.ram_base + vm.ram_size
                vcpu.set_sp(stack_top)

                # PSTATE = EL1h with all interrupts masked
                pstate = (
                    registers.PSTATE_MODE_EL1H |  # Exception Level 1, SP_EL1
                    registers.PSTATE_A |           # Mask async aborts
                    registers.PSTATE_I |           # Mask IRQs
                    registers.PSTATE_F             # Mask FIQs
                )
                vcpu.set_pstate(pstate)

                print()
                print("Initial register state:")
                print(f"  PC = 0x{entry_point:016x}")
                print(f"  SP = 0x{stack_top:016x}")
                print(f"  PSTATE = 0x{pstate:016x}")
                print()

                # Load the binary
                print(f"Loading {binary}...")
                runner.load_binary(binary, entry_point)

                # Run!
                print("Running...")
                print("-" * 60)

                stats = runner.run()

                print("-" * 60)
                print()
                print("Execution finished!")
                print(f"  Total exits: {stats['exits']}")
                print(f"  Exit reason: {stats['exit_reason']}")
                print(f"  Guest halted: {stats['hlt']}")

                if stats["exit_counts"]:
                    print("  Exit breakdown:")
                    for reason, count in stats["exit_counts"].items():
                        print(f"    {reason}: {count}")

                # Check if our test value was written
                test_addr = 0x40001000
                try:
                    data = vm.memory.read(test_addr, 8)
                    value = int.from_bytes(data, "little")
                    print()
                    print(f"Memory at 0x{test_addr:08x}: 0x{value:016x}")
                    if value == 0xDEADBEEF:
                        print("SUCCESS! Guest wrote expected value.")
                except Exception as e:
                    print(f"Could not read test address: {e}")

    except (KVMError, VMError, RunnerError) as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1)
```

## Testing

Now let's test everything!

Inside Lima:

```bash
cd ~/workplace/veleiro-god

# Build the test program
aarch64-linux-gnu-as -o tests/guest_code/simple.o tests/guest_code/simple.S
aarch64-linux-gnu-ld -nostdlib -static -Ttext=0x40080000 \
    -o tests/guest_code/simple tests/guest_code/simple.o
aarch64-linux-gnu-objcopy -O binary tests/guest_code/simple tests/guest_code/simple.bin

# Run it!
god run tests/guest_code/simple.bin
```

Expected output:

```
Creating VM with 64 MB RAM...
VM created: fd=4
vCPU created: fd=5

Initial register state:
  PC     = 0x0000000040080000
  SP     = 0x0000000044000000
  PSTATE = 0x00000000000001c5

Loading tests/guest_code/simple.bin...
Loaded 36 bytes at 0x40080000

Running...
------------------------------------------------------------
Guest requested shutdown/reset
------------------------------------------------------------

Execution finished!
  Total exits: 1
  Exit reason: SYSTEM_EVENT
  Guest halted: False
  Exit breakdown:
    SYSTEM_EVENT: 1

Memory at 0x40001000: 0x00000000deadbeef
SUCCESS! Guest wrote expected value.
```

We just ran code in a virtual machine we built from scratch!

## Deep Dive: ARM Exception Levels

Let's understand the exception levels more deeply, since they're fundamental to how ARM virtualization works.

### The Four Exception Levels

```
┌─────────────────────────────────────────────────────────────────┐
│ EL3: Secure Monitor                                             │
│   - Highest privilege level                                     │
│   - Controls transitions between secure and non-secure worlds   │
│   - Usually runs firmware (ARM Trusted Firmware)                │
├─────────────────────────────────────────────────────────────────┤
│ EL2: Hypervisor                                                 │
│   - Second highest privilege                                    │
│   - Controls virtualization                                     │
│   - KVM runs here (when enabled)                                │
│   - Can trap and emulate guest operations                       │
├─────────────────────────────────────────────────────────────────┤
│ EL1: Operating System Kernel                                    │
│   - Privileged mode for OS kernel                               │
│   - Can manage memory, handle interrupts                        │
│   - Guest OS runs here                                          │
├─────────────────────────────────────────────────────────────────┤
│ EL0: User Applications                                          │
│   - Lowest privilege                                            │
│   - User programs run here                                      │
│   - Cannot access privileged resources directly                 │
└─────────────────────────────────────────────────────────────────┘
```

### How Virtualization Uses Exception Levels

When KVM is enabled:
- The host Linux kernel runs at EL2 (using VHE - Virtualization Host Extensions)
- Guest code runs at EL1 and EL0, but KVM traps certain operations
- When a trap occurs, the CPU transitions to EL2 and KVM handles it

The PSTATE register contains the current exception level in its lowest 4 bits:
- 0b0000 (0): EL0 with SP_EL0
- 0b0100 (4): EL1 with SP_EL0
- 0b0101 (5): EL1 with SP_EL1 (what we use)

### Why We Use EL1h

When we set PSTATE to `PSTATE_MODE_EL1H (0x05)`, we're saying:
- Run at Exception Level 1 (kernel mode)
- Use the SP_EL1 stack pointer (dedicated kernel stack)

This is appropriate for bare-metal code or kernel code. User programs would run at EL0.

## Gotchas

### vCPU Initialization

On ARM64, you **must** call `KVM_ARM_VCPU_INIT` before running the vCPU. If you forget, you'll get errors or undefined behavior. Also:
- Enable `KVM_ARM_VCPU_PSCI_0_2` in the features, or the guest won't be able to shut down cleanly.
- `KVM_ARM_PREFERRED_TARGET` is called on the **VM fd**, not `/dev/kvm`.

### cffi Variadic Arguments

When using cffi to call variadic functions like `ioctl()`, you must cast integer arguments to cdata types:

```python
# Wrong - causes "needs to be a cdata object" error
lib.ioctl(fd, KVM_RUN, 0)

# Correct
lib.ioctl(fd, KVM_RUN, ffi.cast("int", 0))
```

### kvm_run Structure Offsets

The exit-specific data in `kvm_run` starts at offset **32**, not 16. The first 32 bytes contain:
- Bytes 0-7: Input fields
- Bytes 8-15: Exit reason and flags
- Bytes 16-31: cr8 and apic_base

If you get the offset wrong, you'll read garbage from the MMIO fields.

### Register Encoding

The register IDs have a specific encoding. If you get them wrong, you'll get EINVAL errors. Always use the constants from the registers module.

### PSTATE Must Be Valid

PSTATE has specific valid combinations. Invalid values cause entry failures. Always use the defined constants.

### Memory Must Be Mapped

If the PC points to an address that isn't in a memory region, the guest will fail immediately. Make sure your entry point is in mapped memory.

### WFI/HLT Don't Exit

On ARM64 KVM:
- `WFI` (Wait For Interrupt) does **not** cause a VM exit. The guest just sleeps forever waiting for an interrupt.
- `HLT` is a debug halt, not a VM exit.

Use PSCI SYSTEM_OFF via `hvc #0` to exit cleanly.

## What's Next?

In this chapter, we:

1. Learned how vCPUs work
2. Understood ARM64 registers
3. Implemented the vCPU class with register access
4. Created the run loop
5. Wrote and ran our first guest program
6. Saw actual code execute in our VM!

In the next chapter, we'll add a serial console so our guest can print text. We'll learn about MMIO device emulation and implement the PL011 UART.

[Continue to Chapter 4: Emulating a Serial Console →](04-serial-console.md)
