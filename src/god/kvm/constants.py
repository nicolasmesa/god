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
_IOC_NONE = 0  # No data transfer
_IOC_WRITE = 1  # Writing data to the driver
_IOC_READ = 2  # Reading data from the driver

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
        (dir << _IOC_DIRSHIFT)
        | (type << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
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
KVM_CREATE_DEVICE = _IOWR(KVMIO, 0xE0, 12)  # 12 = sizeof(struct kvm_create_device)

# Inject an interrupt into the guest via the interrupt controller.
# Argument is a pointer to struct kvm_irq_level.
# For level-triggered interrupts, you must deassert when the condition clears.
KVM_IRQ_LINE = _IOW(KVMIO, 0x61, 8)  # 8 = sizeof(struct kvm_irq_level)


# ============================================================================
# Device ioctls (on device file descriptor from KVM_CREATE_DEVICE)
# ============================================================================

# Set an attribute on an in-kernel device.
# Used to configure devices like the GIC (set addresses, initialize, etc.)
# Argument is a pointer to struct kvm_device_attr.
KVM_SET_DEVICE_ATTR = _IOW(KVMIO, 0xE1, 24)  # 24 = sizeof(struct kvm_device_attr)

# Get an attribute from an in-kernel device.
KVM_GET_DEVICE_ATTR = _IOW(KVMIO, 0xE2, 24)

# Check if a device attribute exists (without getting/setting it).
KVM_HAS_DEVICE_ATTR = _IOW(KVMIO, 0xE3, 24)


# ============================================================================
# vCPU ioctls (on vCPU file descriptor)
# ============================================================================

# Run the vCPU until it exits.
# The exit reason and details are in the mmap'd kvm_run structure.
KVM_RUN = _IO(KVMIO, 0x80)

# Get the value of a single register.
# Argument is a pointer to struct kvm_one_reg.
KVM_GET_ONE_REG = _IOW(KVMIO, 0xAB, 16)  # 16 = sizeof(struct kvm_one_reg)

# Set the value of a single register.
# Argument is a pointer to struct kvm_one_reg.
KVM_SET_ONE_REG = _IOW(KVMIO, 0xAC, 16)

# Initialize the vCPU with a specific configuration.
# Required on ARM before first run.
# struct kvm_vcpu_init has target (4 bytes) + features[7] (28 bytes) = 32 bytes
KVM_ARM_VCPU_INIT = _IOW(KVMIO, 0xAE, 32)

# Get the preferred target CPU type for this host.
# Returns the configuration to use with KVM_ARM_VCPU_INIT.
KVM_ARM_PREFERRED_TARGET = _IOR(KVMIO, 0xAF, 32)


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
O_RDWR = 2  # Open for reading and writing
O_CLOEXEC = 0o2000000  # Close file descriptor on exec


# ============================================================================
# Memory protection flags (for mmap)
# ============================================================================
PROT_READ = 1  # Pages can be read
PROT_WRITE = 2  # Pages can be written


# ============================================================================
# Memory mapping flags (for mmap)
# ============================================================================
MAP_SHARED = 1  # Share changes with other mappings
MAP_PRIVATE = 2  # Changes are private to this mapping
MAP_ANONYMOUS = 0x20  # Don't back with a file (just allocate memory)


# ============================================================================
# KVM Exit Reasons
# ============================================================================
# When KVM_RUN returns, kvm_run.exit_reason tells us why the guest stopped.

KVM_EXIT_UNKNOWN = 0  # Unknown exit reason
KVM_EXIT_EXCEPTION = 1  # Guest caused an exception
KVM_EXIT_IO = 2  # Guest accessed I/O port (x86, not used on ARM)
KVM_EXIT_HYPERCALL = 3  # Guest made a hypercall
KVM_EXIT_DEBUG = 4  # Debug event
KVM_EXIT_HLT = 5  # Guest executed HLT instruction
KVM_EXIT_MMIO = 6  # Guest accessed memory-mapped I/O
KVM_EXIT_IRQ_WINDOW_OPEN = 7  # Interrupt window is open
KVM_EXIT_SHUTDOWN = 8  # Guest shut down
KVM_EXIT_FAIL_ENTRY = 9  # Entry to guest failed
KVM_EXIT_INTR = 10  # Interrupted by signal
KVM_EXIT_SET_TPR = 11  # TPR access (x86)
KVM_EXIT_TPR_ACCESS = 12  # TPR access (x86)
KVM_EXIT_INTERNAL_ERROR = 17  # Internal KVM error
KVM_EXIT_SYSTEM_EVENT = 24  # System event (reset, shutdown, etc.)
KVM_EXIT_ARM_NISV = 28  # ARM: Not Implemented Special Value


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


# ============================================================================
# In-kernel Device Types (for KVM_CREATE_DEVICE)
# ============================================================================
# These identify which type of device to create.

# ARM GICv2 - older interrupt controller, memory-mapped CPU interface
KVM_DEV_TYPE_ARM_VGIC_V2 = 5

# ARM GICv3 - modern interrupt controller, system register CPU interface
# This is what we use for our VMM.
KVM_DEV_TYPE_ARM_VGIC_V3 = 7


# ============================================================================
# GIC Device Attribute Groups (for KVM_SET_DEVICE_ATTR)
# ============================================================================
# These are the "namespaces" for GIC configuration.

# Address configuration group - set where GIC components appear in memory
KVM_DEV_ARM_VGIC_GRP_ADDR = 0

# Distributor configuration group
KVM_DEV_ARM_VGIC_GRP_DIST_REGS = 1

# CPU interface configuration group (GICv2 only)
KVM_DEV_ARM_VGIC_GRP_CPU_REGS = 2

# Control group - initialization and other control operations
KVM_DEV_ARM_VGIC_GRP_CTRL = 4

# Redistributor configuration group (GICv3)
KVM_DEV_ARM_VGIC_GRP_REDIST_REGS = 5


# ============================================================================
# GIC Address Types (for KVM_DEV_ARM_VGIC_GRP_ADDR)
# ============================================================================
# These specify which GIC component's address we're setting.

# GICv2 addresses
KVM_VGIC_V2_ADDR_TYPE_DIST = 0  # Distributor base address
KVM_VGIC_V2_ADDR_TYPE_CPU = 1   # CPU interface base address

# GICv3 addresses
KVM_VGIC_V3_ADDR_TYPE_DIST = 2    # Distributor base address
KVM_VGIC_V3_ADDR_TYPE_REDIST = 3  # Redistributor base address


# ============================================================================
# GIC Control Attributes (for KVM_DEV_ARM_VGIC_GRP_CTRL)
# ============================================================================

# Initialize the GIC after configuration is complete.
# Must be called after setting addresses, before creating vCPUs.
KVM_DEV_ARM_VGIC_CTRL_INIT = 0


# ============================================================================
# Device Creation Flags (for KVM_CREATE_DEVICE)
# ============================================================================

# Test if device creation would succeed without actually creating it.
# Useful for checking hardware/kernel support.
KVM_CREATE_DEVICE_TEST = 1
