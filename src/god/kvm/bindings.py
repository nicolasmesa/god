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

    // Memory region structure for KVM_SET_USER_MEMORY_REGION
    // This tells KVM how to map guest physical addresses to host memory.
    struct kvm_userspace_memory_region {
        uint32_t slot;            // Slot ID (0, 1, 2, ...) - identifies this region
        uint32_t flags;           // Flags like KVM_MEM_READONLY
        uint64_t guest_phys_addr; // Guest Physical Address (GPA) - where guest sees it
        uint64_t memory_size;     // Size in bytes (must be page-aligned)
        uint64_t userspace_addr;  // Host Virtual Address (HVA) - where it really is
    };

    // vCPU initialization structure for ARM64
    // Used with KVM_ARM_PREFERRED_TARGET and KVM_ARM_VCPU_INIT
    struct kvm_vcpu_init {
        uint32_t target;       // CPU target type (e.g., generic ARM, Cortex-A53)
        uint32_t features[7];  // Feature flags (e.g., enable PSCI, SVE, etc.)
    };

    // Single register access structure
    // Used with KVM_GET_ONE_REG and KVM_SET_ONE_REG
    struct kvm_one_reg {
        uint64_t id;    // Register identifier (encodes type, size, and which register)
        uint64_t addr;  // Pointer to value (cast from userspace address)
    };

    // =========================================================================
    // In-kernel device structures (GIC, etc.)
    // =========================================================================

    // Structure for creating an in-kernel device (KVM_CREATE_DEVICE)
    // Used for devices that KVM emulates in kernel space for performance,
    // like the interrupt controller (GIC).
    struct kvm_create_device {
        uint32_t type;   // Input: device type (e.g., KVM_DEV_TYPE_ARM_VGIC_V3)
        uint32_t fd;     // Output: file descriptor for the created device
        uint32_t flags;  // Input: creation flags (e.g., KVM_CREATE_DEVICE_TEST)
    };

    // Structure for getting/setting device attributes (KVM_SET_DEVICE_ATTR)
    // This is a key-value interface for configuring in-kernel devices.
    // Think of it as: device.set(group, attr, value_at_addr)
    struct kvm_device_attr {
        uint32_t flags;  // Flags (currently unused, set to 0)
        uint32_t group;  // Attribute group (namespace), e.g., "addresses" or "control"
        uint64_t attr;   // Specific attribute within the group
        uint64_t addr;   // Pointer to the value (for set) or buffer (for get)
    };

    // Structure for injecting interrupts (KVM_IRQ_LINE)
    // Used to assert or deassert interrupt lines to the guest.
    struct kvm_irq_level {
        // The irq field encodes both the interrupt number and type.
        // For ARM GIC SPIs: just the interrupt number (32+)
        uint32_t irq;
        uint32_t level;  // 1 = assert (raise), 0 = deassert (lower)
    };
""")

# Compile the C interface
# This creates a dynamic library we can call from Python
lib = ffi.dlopen(None)  # None means use the C library


def get_errno() -> int:
    """
    Get the current errno value.

    errno is a global variable in C that contains the error code from
    the last system call that failed. We need to check it after ioctl
    calls to know what went wrong.
    """
    return lib.__errno_location()[0]
