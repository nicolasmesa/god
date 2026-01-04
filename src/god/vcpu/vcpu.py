"""
Virtual CPU management.

This module provides the VCPU class that represents a virtual processor.
A vCPU is what the guest sees as a CPU - it has registers, can execute
instructions, and traps to the hypervisor for certain operations.
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

    The lifecycle is:
    1. Create the vCPU (KVM_CREATE_VCPU)
    2. mmap the shared kvm_run structure
    3. Initialize for ARM64 (KVM_ARM_VCPU_INIT)
    4. Set registers (PC, SP, PSTATE, etc.)
    5. Run in a loop (KVM_RUN), handling exits

    Usage:
        with VCPU(vm_fd, kvm, vcpu_id=0) as vcpu:
            vcpu.set_pc(entry_point)
            vcpu.set_sp(stack_top)
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

        Raises:
            VCPUError: If vCPU creation fails.
        """
        self._vm_fd = vm_fd
        self._kvm = kvm
        self._vcpu_id = vcpu_id
        self._fd = -1
        self._kvm_run = None
        self._kvm_run_size = 0
        self._closed = False

        # Step 1: Create the vCPU
        # This gives us a file descriptor for vCPU-specific operations
        # Note: vcpu_id must be cast to int for cffi's variadic ioctl
        self._fd = lib.ioctl(vm_fd, KVM_CREATE_VCPU, ffi.cast("int", vcpu_id))
        if self._fd < 0:
            raise VCPUError(f"Failed to create vCPU {vcpu_id}: errno {get_errno()}")

        # Step 2: mmap the kvm_run structure
        # This is shared memory between the kernel and userspace.
        # When we call KVM_RUN and the guest exits, the kernel writes
        # exit information here. We read it to know what happened.
        self._kvm_run_size = kvm.get_vcpu_mmap_size()

        # Note: We use MAP_SHARED, not MAP_PRIVATE!
        # This is different from guest RAM which uses MAP_PRIVATE.
        # kvm_run is shared with the kernel - we need to see the kernel's
        # writes to the exit_reason field.
        kvm_run_ptr = lib.mmap(
            ffi.NULL,
            self._kvm_run_size,
            PROT_READ | PROT_WRITE,
            MAP_SHARED,
            self._fd,
            0,
        )

        if kvm_run_ptr == ffi.cast("void *", -1):
            lib.close(self._fd)
            raise VCPUError(f"Failed to mmap kvm_run: errno {get_errno()}")

        self._kvm_run = kvm_run_ptr

        # Step 3: Initialize the vCPU for ARM64
        self._init_arm64()

    def _init_arm64(self):
        """
        Initialize the vCPU with ARM64-specific settings.

        On ARM64, we must call KVM_ARM_VCPU_INIT before running the vCPU.
        This tells KVM what CPU features to emulate.
        """
        # First, ask KVM what CPU type to emulate on this host
        # This returns a configuration suitable for this hardware
        # Note: KVM_ARM_PREFERRED_TARGET is called on the VM fd, not /dev/kvm
        init = ffi.new("struct kvm_vcpu_init *")

        result = lib.ioctl(self._vm_fd, KVM_ARM_PREFERRED_TARGET, init)
        if result < 0:
            raise VCPUError(
                f"Failed to get preferred target: errno {get_errno()}"
            )

        # Enable PSCI 0.2 support
        # This allows the guest to use PSCI calls (CPU_OFF, etc.)
        # Feature bits are in features[0] as a bitmask
        KVM_ARM_VCPU_PSCI_0_2 = 2  # Bit index for PSCI 0.2
        init.features[0] |= (1 << KVM_ARM_VCPU_PSCI_0_2)

        # Now initialize the vCPU with that configuration
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

        Raises:
            VCPUError: If reading the register fails.
        """
        # Allocate space for the value
        value = ffi.new("uint64_t *")

        # Build the kvm_one_reg request
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

        Raises:
            VCPUError: If setting the register fails.
        """
        # Store the value
        value_ptr = ffi.new("uint64_t *")
        value_ptr[0] = value

        # Build the kvm_one_reg request
        reg = ffi.new("struct kvm_one_reg *")
        reg.id = reg_id
        reg.addr = int(ffi.cast("uintptr_t", value_ptr))

        result = lib.ioctl(self._fd, KVM_SET_ONE_REG, reg)
        if result < 0:
            raise VCPUError(
                f"Failed to set register {registers.get_register_name(reg_id)}: "
                f"errno {get_errno()}"
            )

    # Convenience methods for common registers

    def get_pc(self) -> int:
        """Get the program counter (address of next instruction)."""
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

        The vCPU will execute guest code until something causes it to
        exit back to userspace (halt instruction, MMIO access, etc.).

        Returns:
            The exit reason (KVM_EXIT_* constant).

        Raises:
            VCPUError: If KVM_RUN fails unexpectedly.
        """
        result = lib.ioctl(self._fd, KVM_RUN, ffi.cast("int", 0))
        if result < 0:
            errno = get_errno()
            # EINTR (4) means we were interrupted by a signal.
            # This is normal - just return a special value.
            if errno == 4:
                return -1
            raise VCPUError(f"KVM_RUN failed: errno {errno}")

        # Get exit reason from kvm_run structure
        # The structure layout (from Linux headers):
        #   offset 0-7: input fields (request_interrupt_window, immediate_exit, padding)
        #   offset 8: exit_reason (uint32_t)
        exit_reason = ffi.cast("uint32_t *", self._kvm_run + 8)[0]
        return exit_reason

    def get_exit_reason_name(self, exit_reason: int) -> str:
        """Get a human-readable name for an exit reason."""
        return EXIT_REASON_NAMES.get(exit_reason, f"UNKNOWN({exit_reason})")

    def get_mmio_info(self) -> tuple[int, bytes, int, bool]:
        """
        Get MMIO exit information.

        Call this after run() returns KVM_EXIT_MMIO to find out what
        memory address the guest tried to access.

        The kvm_run structure layout:
            offset 0-7:   request_interrupt_window, immediate_exit, padding
            offset 8-11:  exit_reason
            offset 12-15: ready_for_interrupt_injection, if_flag, flags
            offset 16-23: cr8
            offset 24-31: apic_base
            offset 32+:   exit union

        For MMIO (at offset 32):
            struct {
                __u64 phys_addr;   // offset 32 (+0)
                __u8  data[8];     // offset 40 (+8)
                __u32 len;         // offset 48 (+16)
                __u8  is_write;    // offset 52 (+20)
            } mmio;

        Returns:
            Tuple of (physical_address, data, length, is_write)
        """
        # Exit union starts at offset 32 in kvm_run
        mmio_base = self._kvm_run + 32

        phys_addr = ffi.cast("uint64_t *", mmio_base)[0]
        data = bytes(ffi.cast("uint8_t *", mmio_base + 8)[i] for i in range(8))
        length = ffi.cast("uint32_t *", mmio_base + 16)[0]
        is_write = bool(ffi.cast("uint8_t *", mmio_base + 20)[0])

        return phys_addr, data[:length], length, is_write

    def set_mmio_data(self, data: bytes):
        """
        Set data for an MMIO read response.

        When the guest reads from a device, we need to provide the data
        before resuming. Call this to put the read data in kvm_run.

        Args:
            data: The data to return to the guest (max 8 bytes).
        """
        # data field is at offset 32 + 8 = 40
        mmio_data_ptr = ffi.cast("uint8_t *", self._kvm_run + 32 + 8)
        for i, byte in enumerate(data[:8]):
            mmio_data_ptr[i] = byte

    def dump_registers(self):
        """Print all general-purpose registers (for debugging)."""
        print("vCPU Registers:")
        print("-" * 50)

        # General-purpose registers in rows of 4
        for i in range(0, 31, 4):
            parts = []
            for j in range(4):
                if i + j < 31:
                    reg_id = registers.X_REGISTERS[i + j]
                    value = self.get_register(reg_id)
                    parts.append(f"x{i+j:2d}=0x{value:016x}")
            print("  " + "  ".join(parts))

        # Special registers
        print()
        print(f"  sp     = 0x{self.get_sp():016x}")
        print(f"  pc     = 0x{self.get_pc():016x}")
        print(f"  pstate = 0x{self.get_pstate():016x}")

        # System registers (for exception debugging)
        print()
        print("System Registers:")
        try:
            # VBAR_EL1 should be readable
            vbar = self.get_register(registers.VBAR_EL1)
            print(f"  VBAR_EL1  = 0x{vbar:016x}  (Exception Vector Base)")
        except Exception as e:
            print(f"  VBAR_EL1  = (read failed: {e})")

        try:
            esr = self.get_register(registers.ESR_EL1)
            far = self.get_register(registers.FAR_EL1)
            elr = self.get_register(registers.ELR_EL1)
            sctlr = self.get_register(registers.SCTLR_EL1)
            print(f"  ESR_EL1   = 0x{esr:016x}  (Exception Syndrome)")
            print(f"  FAR_EL1   = 0x{far:016x}  (Fault Address)")
            print(f"  ELR_EL1   = 0x{elr:016x}  (Exception Return)")
            print(f"  SCTLR_EL1 = 0x{sctlr:016x}  (System Control)")

            # Decode ESR_EL1
            ec = (esr >> 26) & 0x3F  # Exception Class
            ec_names = {
                0x00: "Unknown",
                0x01: "WFI/WFE",
                0x15: "SVC in AArch64",
                0x16: "HVC in AArch64",
                0x17: "SMC in AArch64",
                0x20: "Instruction Abort (lower EL)",
                0x21: "Instruction Abort (same EL)",
                0x22: "PC alignment",
                0x24: "Data Abort (lower EL)",
                0x25: "Data Abort (same EL)",
                0x26: "SP alignment",
            }
            ec_name = ec_names.get(ec, f"Unknown(0x{ec:02x})")
            print(f"  -> Exception Class: {ec_name}")
        except Exception as e:
            print(f"  (Could not read system registers: {e})")

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
        """Support for 'with' statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up when exiting 'with' block."""
        self.close()
        return False
