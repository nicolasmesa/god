# Chapter 1: Talking to KVM - The Foundation

In this chapter, we'll establish communication with KVM and verify that our system is set up correctly. By the end, you'll understand how hardware virtualization works and have a working `god kvm info` command.

## How Hardware Virtualization Works

Before we write any code, let's understand what problem KVM solves and how it solves it.

### The Problem: Running Guest Code Safely

Imagine you want to run Windows inside Linux. The Windows code expects to have full control of the CPU—it wants to access hardware directly, manage memory however it likes, and handle interrupts. But we can't just let random code do that on our real CPU—it could crash our host system or access other users' data.

There are several ways to solve this:

**Option 1: Full Emulation (Software Interpretation)**

Interpret every guest instruction in software. When the guest executes `mov x0, #42`, our emulator:
1. Reads the instruction bytes
2. Decodes what operation it represents
3. Simulates the effect (set virtual register x0 to 42)

This is extremely slow—maybe 100x slower than native execution. Every single instruction requires dozens of host instructions to emulate.

**Option 2: Dynamic Binary Translation**

Translate blocks of guest code into host code on-the-fly. Instead of interpreting each instruction, translate a whole sequence into host-native code and run that.

This is faster than interpretation (maybe 5-20x slowdown), but still has overhead from translation and the need to handle privileged instructions specially.

**Option 3: Hardware Virtualization (What We'll Use)**

Modern CPUs have special "guest mode" support. The CPU can run guest code directly at full native speed, and automatically trap to the host when the guest does something that needs intervention.

This is nearly as fast as native execution for CPU-bound code—the only overhead is handling the traps (VM exits).

### Hardware Virtualization on ARM64

ARM64 processors have hardware virtualization support called **VHE** (Virtualization Host Extensions). Here's how it works:

ARM64 has four **Exception Levels** (privilege levels):

```
┌─────────────────────────────────────────┐
│  EL0  │  User applications              │  Least privileged
├───────┼─────────────────────────────────┤
│  EL1  │  Operating system kernel        │
├───────┼─────────────────────────────────┤
│  EL2  │  Hypervisor                     │
├───────┼─────────────────────────────────┤
│  EL3  │  Secure monitor / firmware      │  Most privileged
└───────┴─────────────────────────────────┘
```

When running a virtual machine:

- **Guest applications** run at EL0 (like normal)
- **Guest kernel** runs at EL1 (like normal)
- **KVM (the hypervisor)** runs at EL2
- The **host Linux kernel** also runs at EL2 (thanks to VHE, it can share EL2 with KVM)

When the guest tries to do something privileged (like access certain system registers or perform I/O), the CPU automatically traps from guest EL1 to hypervisor EL2. KVM then handles the situation and returns to the guest.

```
Normal execution:
┌─────────────┐
│ Guest runs  │ ───→ Guest executes instructions at native speed
│ (EL0/EL1)   │
└─────────────┘

When guest does something privileged:
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Guest runs  │ ──→ │  VM Exit    │ ──→ │ KVM handles │
│ (EL1)       │     │  (trap)     │     │ (EL2)       │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                    ┌─────────────┐     ┌──────┴──────┐
                    │ Guest runs  │ ←── │  VM Entry   │
                    │ (EL1)       │     │  (return)   │
                    └─────────────┘     └─────────────┘
```

### What the CPU Does vs. What We Do

**The CPU hardware handles:**
- Running guest code at native speed
- Trapping privileged operations
- Switching between guest and host context
- Memory virtualization (Stage-2 translation)
- Virtualizing timers and interrupts (partially)

**KVM (the Linux module) handles:**
- Setting up the CPU's virtualization features
- Managing guest memory mappings
- Handling most VM exits
- Emulating the interrupt controller
- Providing an API for userspace VMMs

**We (our Python VMM) handle:**
- Creating and destroying VMs
- Setting up guest memory
- Emulating devices (serial port, disk, etc.)
- Loading guest code (kernel, initramfs)
- Running the main VMM loop

## The KVM Interface

### The File /dev/kvm

In Unix, everything is a file. KVM exposes itself through a special device file: `/dev/kvm`. When we open this file, we get a **file descriptor** (just a number) that we can use to send commands to KVM.

```python
# Conceptually, this is what we're doing:
kvm_fd = open("/dev/kvm")

# Now kvm_fd is a number like 3 or 4 that represents our connection to KVM
```

### File Descriptors in Unix

If you haven't worked with file descriptors before, here's a quick primer:

When a process opens a file, the operating system:
1. Finds the file and prepares to do I/O on it
2. Returns a small integer (the file descriptor) that represents this open file
3. The process uses this integer for all future operations on the file

Standard file descriptors:
- 0 = stdin (standard input)
- 1 = stdout (standard output)
- 2 = stderr (standard error)
- 3+ = files you open

When you're done with a file, you close the file descriptor to release resources.

### The ioctl System Call

**ioctl** (pronounced "eye-ock-tull") stands for "I/O Control." It's a system call that sends commands to device drivers.

The basic form is:
```c
int ioctl(int fd, unsigned long request, ...);
```

Where:
- `fd` is the file descriptor (like the one we got from opening /dev/kvm)
- `request` is a command number (like KVM_GET_API_VERSION)
- `...` is optional data to pass to or receive from the driver

For example:
```python
# Ask KVM what API version it speaks
version = ioctl(kvm_fd, KVM_GET_API_VERSION)

# Create a new VM (returns a new file descriptor for the VM)
vm_fd = ioctl(kvm_fd, KVM_CREATE_VM, 0)

# Create a vCPU in that VM (returns a new file descriptor for the vCPU)
vcpu_fd = ioctl(vm_fd, KVM_CREATE_VCPU, 0)
```

### The Hierarchy of File Descriptors

When working with KVM, we'll have multiple file descriptors forming a hierarchy:

```
/dev/kvm (kvm_fd)
    │
    ├── VM 1 (vm_fd)
    │   ├── vCPU 0 (vcpu_fd)
    │   └── vCPU 1 (vcpu_fd)
    │
    └── VM 2 (vm_fd)
        └── vCPU 0 (vcpu_fd)
```

Different ioctls work on different file descriptor types:
- `kvm_fd`: System-wide operations (get version, check capabilities, create VM)
- `vm_fd`: VM-specific operations (set memory, create vCPU, create devices)
- `vcpu_fd`: vCPU-specific operations (set registers, run vCPU)

## Our First cffi Bindings

### What is cffi?

**cffi** (C Foreign Function Interface) lets Python code call C functions and work with C data structures.

Why do we need it? KVM's interface is defined in C header files. The ioctl request codes are C macros. The data structures are C structs. We need a way to:

1. Define these C constructs in Python
2. Call the ioctl system call with the right parameters
3. Read and write C structures from Python

cffi gives us all of this.

### Adding cffi to Our Project

First, let's add cffi to our dependencies:

```bash
# In the project directory
uv add cffi
```

This updates `pyproject.toml` to include cffi.

### Defining C Types for KVM

Let's start building our KVM bindings. Create `src/god/kvm/bindings.py`:

```python
"""
Low-level cffi bindings for the KVM API.

This module provides Python access to the KVM (Kernel-based Virtual Machine)
interface through cffi. KVM is a Linux kernel module that provides hardware
virtualization capabilities.

The bindings are structured around KVM's file descriptor hierarchy:
- /dev/kvm: System-level operations
- VM file descriptors: Per-VM operations
- vCPU file descriptors: Per-vCPU operations
"""

from cffi import FFI

# Create the FFI instance that we'll use throughout
ffi = FFI()

# Define the C types and constants we need.
# These come from Linux kernel headers:
# - /usr/include/linux/kvm.h
# - /usr/include/asm/kvm.h (architecture-specific)
ffi.cdef("""
    // Standard C types we'll use
    typedef unsigned long size_t;
    typedef long ssize_t;

    // ioctl request type
    typedef unsigned long ioctl_request_t;

    // File operations
    int open(const char *pathname, int flags);
    int close(int fd);
    int ioctl(int fd, ioctl_request_t request, ...);

    // Memory mapping
    void *mmap(void *addr, size_t length, int prot, int flags, int fd, long offset);
    int munmap(void *addr, size_t length);

    // Error handling
    int *__errno_location(void);
""")

# Compile the C interface
# This creates a dynamic library we can call from Python
lib = ffi.dlopen(None)  # None means use the C library

def get_errno():
    """
    Get the current errno value.

    errno is a global variable in C that contains the error code from
    the last system call that failed. We need to check it after ioctl
    calls to know what went wrong.
    """
    return lib.__errno_location()[0]
```

### The ioctl Request Codes

KVM defines many ioctl request codes. These are magic numbers that tell KVM what operation to perform. They're defined as C macros in the Linux headers, but we need to translate them to Python constants.

Create `src/god/kvm/constants.py`:

```python
"""
KVM constants and ioctl request codes.

These values come from the Linux kernel headers:
- /usr/include/linux/kvm.h
- /usr/include/asm-generic/ioctl.h

Each constant is documented with its purpose and typical usage.
"""

# ============================================================================
# ioctl Direction and Size Encoding
# ============================================================================
# Linux ioctl request codes encode the direction (read/write) and data size.
# The format is: direction (2 bits) | size (14 bits) | type (8 bits) | number (8 bits)

# Direction flags
_IOC_NONE = 0   # No data transfer
_IOC_WRITE = 1  # Writing data to the driver
_IOC_READ = 2   # Reading data from the driver

# Shifts for encoding
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30

def _IOC(dir: int, type: int, nr: int, size: int) -> int:
    """
    Encode an ioctl request number.

    This matches the _IOC macro from Linux headers.
    """
    return (
        (dir << _IOC_DIRSHIFT) |
        (type << _IOC_TYPESHIFT) |
        (nr << _IOC_NRSHIFT) |
        (size << _IOC_SIZESHIFT)
    )

def _IO(type: int, nr: int) -> int:
    """ioctl with no data transfer."""
    return _IOC(_IOC_NONE, type, nr, 0)

def _IOR(type: int, nr: int, size: int) -> int:
    """ioctl that reads data from the driver."""
    return _IOC(_IOC_READ, type, nr, size)

def _IOW(type: int, nr: int, size: int) -> int:
    """ioctl that writes data to the driver."""
    return _IOC(_IOC_WRITE, type, nr, size)

def _IOWR(type: int, nr: int, size: int) -> int:
    """ioctl that both reads and writes data."""
    return _IOC(_IOC_READ | _IOC_WRITE, type, nr, size)


# ============================================================================
# KVM ioctl Type
# ============================================================================
# All KVM ioctls use 0xAE as the type byte (think of it as KVM's "namespace")
KVMIO = 0xAE


# ============================================================================
# System ioctls (on /dev/kvm file descriptor)
# ============================================================================

# Get the KVM API version.
# This must return 12 - that's the stable API version.
# If it returns anything else, the API has changed incompatibly.
KVM_GET_API_VERSION = _IO(KVMIO, 0x00)

# Create a new virtual machine.
# Returns a file descriptor for the new VM.
# The argument is a machine type (0 for default).
KVM_CREATE_VM = _IO(KVMIO, 0x01)

# Check if an extension/capability is supported.
# The argument is the capability number (see KVM_CAP_* constants).
# Returns > 0 if supported (value may indicate feature details).
KVM_CHECK_EXTENSION = _IO(KVMIO, 0x03)

# Get the size of the vcpu mmap area (the kvm_run structure).
# We need to mmap this much memory for each vCPU.
KVM_GET_VCPU_MMAP_SIZE = _IO(KVMIO, 0x04)


# ============================================================================
# VM ioctls (on VM file descriptor)
# ============================================================================

# Set a memory region for the guest.
# Argument is a pointer to struct kvm_userspace_memory_region.
KVM_SET_USER_MEMORY_REGION = _IOW(KVMIO, 0x46, 32)  # 32 = sizeof(struct)

# Create a virtual CPU.
# Returns a file descriptor for the new vCPU.
# The argument is the vCPU ID (0 for first CPU, 1 for second, etc.).
KVM_CREATE_VCPU = _IO(KVMIO, 0x41)

# Create an in-kernel device (like the interrupt controller).
# Argument is a pointer to struct kvm_create_device.
KVM_CREATE_DEVICE = _IOWR(KVMIO, 0xe0, 12)  # 12 = sizeof(struct)


# ============================================================================
# vCPU ioctls (on vCPU file descriptor)
# ============================================================================

# Run the vCPU until it exits.
# The exit reason and details are in the mmap'd kvm_run structure.
KVM_RUN = _IO(KVMIO, 0x80)

# Get the value of a single register.
# Argument is a pointer to struct kvm_one_reg.
KVM_GET_ONE_REG = _IOW(KVMIO, 0xab, 16)  # 16 = sizeof(struct kvm_one_reg)

# Set the value of a single register.
# Argument is a pointer to struct kvm_one_reg.
KVM_SET_ONE_REG = _IOW(KVMIO, 0xac, 16)

# Initialize the vCPU with a specific configuration.
# Required on ARM before first run.
KVM_ARM_VCPU_INIT = _IOW(KVMIO, 0xae, 8)  # 8 = sizeof(struct kvm_vcpu_init)

# Get the preferred target CPU type for this host.
# Returns the configuration to use with KVM_ARM_VCPU_INIT.
KVM_ARM_PREFERRED_TARGET = _IOR(KVMIO, 0xaf, 8)


# ============================================================================
# KVM Capabilities (for KVM_CHECK_EXTENSION)
# ============================================================================
# These are the capability numbers we check to see what features KVM supports.

# Maximum number of vCPUs per VM
KVM_CAP_MAX_VCPUS = 66

# Maximum number of memory slots
KVM_CAP_NR_MEMSLOTS = 10

# ARM64 specific: Can create VMs with different configurations
KVM_CAP_ARM_VM_IPA_SIZE = 165

# Support for setting one register at a time (ARM uses this)
KVM_CAP_ONE_REG = 70


# ============================================================================
# File open flags
# ============================================================================
O_RDWR = 2      # Open for reading and writing
O_CLOEXEC = 0o2000000  # Close file descriptor on exec


# ============================================================================
# Memory protection flags (for mmap)
# ============================================================================
PROT_READ = 1   # Pages can be read
PROT_WRITE = 2  # Pages can be written


# ============================================================================
# Memory mapping flags (for mmap)
# ============================================================================
MAP_SHARED = 1      # Share changes with other mappings
MAP_PRIVATE = 2     # Changes are private to this mapping
MAP_ANONYMOUS = 0x20  # Don't back with a file (just allocate memory)


# ============================================================================
# KVM Exit Reasons
# ============================================================================
# When KVM_RUN returns, kvm_run.exit_reason tells us why the guest stopped.

KVM_EXIT_UNKNOWN = 0          # Unknown exit reason
KVM_EXIT_EXCEPTION = 1        # Guest caused an exception
KVM_EXIT_IO = 2               # Guest accessed I/O port (x86, not used on ARM)
KVM_EXIT_HYPERCALL = 3        # Guest made a hypercall
KVM_EXIT_DEBUG = 4            # Debug event
KVM_EXIT_HLT = 5              # Guest executed HLT instruction
KVM_EXIT_MMIO = 6             # Guest accessed memory-mapped I/O
KVM_EXIT_IRQ_WINDOW_OPEN = 7  # Interrupt window is open
KVM_EXIT_SHUTDOWN = 8         # Guest shut down
KVM_EXIT_FAIL_ENTRY = 9       # Entry to guest failed
KVM_EXIT_INTR = 10            # Interrupted by signal
KVM_EXIT_SET_TPR = 11         # TPR access (x86)
KVM_EXIT_TPR_ACCESS = 12      # TPR access (x86)
KVM_EXIT_INTERNAL_ERROR = 17  # Internal KVM error
KVM_EXIT_SYSTEM_EVENT = 24    # System event (reset, shutdown, etc.)
KVM_EXIT_ARM_NISV = 28        # ARM: Not Implemented Special Value


# Human-readable names for exit reasons (for debugging)
EXIT_REASON_NAMES = {
    KVM_EXIT_UNKNOWN: "UNKNOWN",
    KVM_EXIT_EXCEPTION: "EXCEPTION",
    KVM_EXIT_IO: "IO",
    KVM_EXIT_HYPERCALL: "HYPERCALL",
    KVM_EXIT_DEBUG: "DEBUG",
    KVM_EXIT_HLT: "HLT",
    KVM_EXIT_MMIO: "MMIO",
    KVM_EXIT_IRQ_WINDOW_OPEN: "IRQ_WINDOW_OPEN",
    KVM_EXIT_SHUTDOWN: "SHUTDOWN",
    KVM_EXIT_FAIL_ENTRY: "FAIL_ENTRY",
    KVM_EXIT_INTR: "INTR",
    KVM_EXIT_INTERNAL_ERROR: "INTERNAL_ERROR",
    KVM_EXIT_SYSTEM_EVENT: "SYSTEM_EVENT",
    KVM_EXIT_ARM_NISV: "ARM_NISV",
}
```

### Opening KVM

Now let's create a high-level wrapper that opens KVM and checks the version. Create `src/god/kvm/system.py`:

```python
"""
KVM system-level operations.

This module handles opening /dev/kvm and performing system-level queries
like checking the API version and supported capabilities.
"""

import os
from .bindings import ffi, lib, get_errno
from .constants import (
    KVM_GET_API_VERSION,
    KVM_CHECK_EXTENSION,
    KVM_GET_VCPU_MMAP_SIZE,
    O_RDWR,
    O_CLOEXEC,
)


class KVMError(Exception):
    """Exception raised when a KVM operation fails."""
    pass


class KVMSystem:
    """
    Represents the KVM system interface (/dev/kvm).

    This class handles opening /dev/kvm and provides methods for system-level
    operations like checking capabilities and creating VMs.

    Usage:
        kvm = KVMSystem()
        print(f"KVM API version: {kvm.api_version}")
        print(f"Max vCPUs: {kvm.check_extension(KVM_CAP_MAX_VCPUS)}")
    """

    # The expected KVM API version
    # This has been stable since 2007 - if it changes, something is very wrong
    EXPECTED_API_VERSION = 12

    def __init__(self, device_path: str = "/dev/kvm"):
        """
        Open the KVM device.

        Args:
            device_path: Path to the KVM device file. Almost always /dev/kvm.

        Raises:
            KVMError: If /dev/kvm cannot be opened or the API version is wrong.
        """
        self._device_path = device_path
        self._fd = -1

        # Open /dev/kvm
        # O_RDWR: We need both read and write access
        # O_CLOEXEC: Close this FD if we exec another program (security best practice)
        self._fd = lib.open(device_path.encode(), O_RDWR | O_CLOEXEC)

        if self._fd < 0:
            errno = get_errno()
            if errno == 2:  # ENOENT - file not found
                raise KVMError(
                    f"KVM device not found at {device_path}. "
                    "Is KVM available on this system? "
                    "On Linux, check if the kvm module is loaded: lsmod | grep kvm"
                )
            elif errno == 13:  # EACCES - permission denied
                raise KVMError(
                    f"Permission denied opening {device_path}. "
                    "Try adding yourself to the 'kvm' group: sudo usermod -aG kvm $USER"
                )
            else:
                raise KVMError(f"Failed to open {device_path}: errno {errno}")

        # Check API version
        self._api_version = lib.ioctl(self._fd, KVM_GET_API_VERSION)
        if self._api_version < 0:
            self.close()
            raise KVMError(f"Failed to get KVM API version: errno {get_errno()}")

        if self._api_version != self.EXPECTED_API_VERSION:
            self.close()
            raise KVMError(
                f"Unexpected KVM API version {self._api_version}, "
                f"expected {self.EXPECTED_API_VERSION}. "
                "This version of the VMM may not be compatible with your kernel."
            )

    def close(self):
        """Close the KVM device."""
        if self._fd >= 0:
            lib.close(self._fd)
            self._fd = -1

    def __enter__(self):
        """Support for 'with' statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the device when exiting 'with' block."""
        self.close()
        return False

    @property
    def fd(self) -> int:
        """
        Get the file descriptor for /dev/kvm.

        This is useful when you need to make ioctl calls directly.
        """
        if self._fd < 0:
            raise KVMError("KVM device is closed")
        return self._fd

    @property
    def api_version(self) -> int:
        """
        Get the KVM API version.

        This should always be 12 for modern kernels.
        """
        return self._api_version

    def check_extension(self, capability: int) -> int:
        """
        Check if a KVM extension/capability is supported.

        Args:
            capability: The capability number (KVM_CAP_*).

        Returns:
            The capability value. 0 means not supported, >0 means supported
            (the exact value may have meaning depending on the capability).
        """
        result = lib.ioctl(self._fd, KVM_CHECK_EXTENSION, ffi.cast("unsigned long", capability))
        if result < 0:
            # Some capabilities return -1 for "not supported" instead of 0
            return 0
        return result

    def get_vcpu_mmap_size(self) -> int:
        """
        Get the size of the memory area to mmap for each vCPU.

        When we create a vCPU, we need to mmap a region of memory that
        contains the kvm_run structure. This tells us how big that region is.

        Returns:
            Size in bytes.
        """
        size = lib.ioctl(self._fd, KVM_GET_VCPU_MMAP_SIZE)
        if size < 0:
            raise KVMError(f"Failed to get vCPU mmap size: errno {get_errno()}")
        return size
```

### Creating the Capability Checker

Now let's create a module that queries and displays all relevant capabilities. Create `src/god/kvm/capabilities.py`:

```python
"""
KVM capability checking.

This module queries KVM for its supported capabilities and provides
human-readable descriptions of each.
"""

from dataclasses import dataclass
from typing import Optional

from .system import KVMSystem


@dataclass
class Capability:
    """
    Describes a KVM capability.

    Attributes:
        name: The constant name (e.g., "KVM_CAP_MAX_VCPUS")
        number: The capability number used with KVM_CHECK_EXTENSION
        description: Human-readable description of what this capability does
        value: The value returned by KVM (None if not queried yet)
    """
    name: str
    number: int
    description: str
    value: Optional[int] = None


# All capabilities we care about, with descriptions
CAPABILITIES = [
    Capability(
        name="KVM_CAP_NR_MEMSLOTS",
        number=10,
        description="Maximum number of memory slots per VM",
    ),
    Capability(
        name="KVM_CAP_MAX_VCPUS",
        number=66,
        description="Maximum number of vCPUs per VM",
    ),
    Capability(
        name="KVM_CAP_MAX_VCPU_ID",
        number=128,
        description="Maximum vCPU ID allowed",
    ),
    Capability(
        name="KVM_CAP_ONE_REG",
        number=70,
        description="Supports getting/setting individual registers",
    ),
    Capability(
        name="KVM_CAP_ARM_VM_IPA_SIZE",
        number=165,
        description="Maximum Intermediate Physical Address (IPA) size in bits (ARM64)",
    ),
    Capability(
        name="KVM_CAP_ARM_PSCI_0_2",
        number=102,
        description="Supports PSCI 0.2 (Power State Coordination Interface) for CPU on/off",
    ),
    Capability(
        name="KVM_CAP_ARM_PMU_V3",
        number=126,
        description="Supports ARM Performance Monitor Unit v3",
    ),
    Capability(
        name="KVM_CAP_IRQCHIP",
        number=0,
        description="Supports in-kernel interrupt controller (GIC)",
    ),
    Capability(
        name="KVM_CAP_IOEVENTFD",
        number=36,
        description="Supports IOEVENTFD (efficient doorbell mechanism)",
    ),
    Capability(
        name="KVM_CAP_IRQFD",
        number=32,
        description="Supports IRQFD (efficient interrupt injection)",
    ),
    Capability(
        name="KVM_CAP_ARM_EL1_32BIT",
        number=105,
        description="Supports 32-bit guests at EL1 (AArch32 mode)",
    ),
]


def query_capabilities(kvm: KVMSystem) -> list[Capability]:
    """
    Query all known capabilities from KVM.

    Args:
        kvm: An open KVMSystem instance.

    Returns:
        List of Capability objects with values filled in.
    """
    results = []
    for cap in CAPABILITIES:
        value = kvm.check_extension(cap.number)
        results.append(Capability(
            name=cap.name,
            number=cap.number,
            description=cap.description,
            value=value,
        ))
    return results


def format_capabilities(capabilities: list[Capability]) -> str:
    """
    Format capabilities for display.

    Args:
        capabilities: List of queried capabilities.

    Returns:
        Formatted string suitable for printing.
    """
    lines = []

    # Find the longest name for alignment
    max_name_len = max(len(cap.name) for cap in capabilities)

    for cap in capabilities:
        # Format the value
        if cap.value is None:
            value_str = "not queried"
        elif cap.value == 0:
            value_str = "not supported"
        else:
            value_str = str(cap.value)

        # Build the line
        name_padded = cap.name.ljust(max_name_len)
        lines.append(f"  {name_padded}  = {value_str}")
        lines.append(f"    └─ {cap.description}")

    return "\n".join(lines)
```

### Adding the CLI Command

Now let's add the `god kvm info` command. Update `src/god/cli.py`:

```python
"""
Command-line interface for the god VMM.

This module defines all CLI commands using the Typer library.
"""

import typer

from god import __version__

app = typer.Typer(
    help="god - A Virtual Machine Monitor built from scratch",
    no_args_is_help=True,
)


def version_callback(value: bool):
    """Show version and exit."""
    if value:
        print(f"god version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
):
    """god - A Virtual Machine Monitor built from scratch."""
    pass


# Create a subcommand group for KVM-related commands
kvm_app = typer.Typer(help="KVM-related commands")
app.add_typer(kvm_app, name="kvm")


@kvm_app.command("info")
def kvm_info():
    """
    Display KVM system information.

    Shows the KVM API version, vCPU mmap size, and all supported capabilities.
    This is useful for verifying that KVM is working correctly and understanding
    what features are available on this system.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.kvm.capabilities import query_capabilities, format_capabilities

    try:
        with KVMSystem() as kvm:
            print("KVM System Information")
            print("=" * 60)
            print()
            print(f"Device:            /dev/kvm")
            print(f"API Version:       {kvm.api_version} (expected: 12)")
            print(f"vCPU mmap size:    {kvm.get_vcpu_mmap_size()} bytes")
            print()
            print("Capabilities:")
            print("-" * 60)
            capabilities = query_capabilities(kvm)
            print(format_capabilities(capabilities))
            print()
            print("KVM is ready!")

    except KVMError as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1)
```

### Creating the Package Structure

Make sure the kvm package is properly structured. Create `src/god/kvm/__init__.py`:

```python
"""
KVM interface layer.

This package provides Python bindings for the KVM (Kernel-based Virtual Machine)
API, allowing us to create and manage virtual machines from Python.

Main classes:
- KVMSystem: Represents /dev/kvm and provides system-level operations
"""

from .system import KVMSystem, KVMError

__all__ = ["KVMSystem", "KVMError"]
```

## Testing

### Running Inside Lima

Now let's test our implementation. First, make sure you're inside the Lima VM:

```bash
# From macOS
limactl shell default
```

Inside the Lima VM, navigate to the project directory and run:

```bash
# The project should be mounted at the same path thanks to Lima's file sharing
cd /path/to/workspace/veleiro-god

# Run the command
uv run god kvm info
```

### Expected Output

You should see something like:

```
KVM System Information
============================================================

Device:            /dev/kvm
API Version:       12 (expected: 12)
vCPU mmap size:    8192 bytes

Capabilities:
------------------------------------------------------------
  KVM_CAP_NR_MEMSLOTS       = 32
    └─ Maximum number of memory slots per VM
  KVM_CAP_MAX_VCPUS         = 256
    └─ Maximum number of vCPUs per VM
  KVM_CAP_MAX_VCPU_ID       = 255
    └─ Maximum vCPU ID allowed
  KVM_CAP_ONE_REG           = 1
    └─ Supports getting/setting individual registers
  KVM_CAP_ARM_VM_IPA_SIZE   = 48
    └─ Maximum Intermediate Physical Address (IPA) size in bits (ARM64)
  KVM_CAP_ARM_PSCI_0_2      = 1
    └─ Supports PSCI 0.2 (Power State Coordination Interface) for CPU on/off
  KVM_CAP_ARM_PMU_V3        = 1
    └─ Supports ARM Performance Monitor Unit v3
  KVM_CAP_IRQCHIP           = 1
    └─ Supports in-kernel interrupt controller (GIC)
  KVM_CAP_IOEVENTFD         = 1
    └─ Supports IOEVENTFD (efficient doorbell mechanism)
  KVM_CAP_IRQFD             = 1
    └─ Supports IRQFD (efficient interrupt injection)
  KVM_CAP_ARM_EL1_32BIT     = not supported
    └─ Supports 32-bit guests at EL1 (AArch32 mode)

KVM is ready!
```

### Troubleshooting Common Issues

**Problem: "KVM device not found at /dev/kvm"**

The KVM module might not be loaded, or you might not be in a VM with nested virtualization. In Lima, KVM should be available. Check:
```bash
ls -la /dev/kvm
lsmod | grep kvm
```

**Problem: "Permission denied"**

You don't have access to /dev/kvm. Add yourself to the kvm group:
```bash
sudo usermod -aG kvm $USER
# Then log out and back in, or start a new shell
```

**Problem: cffi import error**

Make sure cffi is installed:
```bash
uv add cffi
uv sync
```

## Deep Dive: KVM Internals (Optional)

If you're curious about how KVM works inside the Linux kernel, here's a brief tour.

### KVM Source Code Structure

KVM's source code lives in the Linux kernel tree:

```
linux/
├── virt/kvm/
│   ├── kvm_main.c      # Core KVM implementation
│   ├── eventfd.c       # Event file descriptor support
│   └── ...
├── arch/arm64/kvm/
│   ├── arm.c           # ARM64-specific KVM code
│   ├── mmu.c           # Memory management
│   ├── handle_exit.c   # VM exit handling
│   └── ...
└── include/linux/kvm_host.h  # Main KVM header
```

### The kvm_main.c Entry Point

When you call `ioctl(kvm_fd, KVM_GET_API_VERSION)`, here's roughly what happens:

1. The kernel routes the ioctl to the KVM file operations handler
2. `kvm_dev_ioctl()` in `kvm_main.c` receives the call
3. It switches on the request code and returns the API version

### ARM-Specific Code

ARM64 KVM code in `arch/arm64/kvm/` handles:
- Setting up Stage-2 page tables (for guest physical → host physical translation)
- Handling VM exits specific to ARM (like WFI - Wait For Interrupt)
- Managing the virtual GIC and timer
- Setting up CPU state for guest entry/exit

## What's Next?

In this chapter, we:

1. Learned how hardware virtualization works
2. Understood the KVM interface (file descriptors, ioctl)
3. Created cffi bindings for KVM
4. Implemented the `god kvm info` command
5. Verified KVM is working on our system

In the next chapter, we'll create an actual virtual machine and set up its memory. We'll learn about guest physical addresses, memory slots, and how to allocate memory for our guest.

[Continue to Chapter 2: Creating a VM and Setting Up Memory →](02-vm-creation-memory.md)
