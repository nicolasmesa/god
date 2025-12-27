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
