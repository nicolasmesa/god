"""
Boot loader for Linux kernels.

This module handles loading the kernel, initramfs, and DTB into
guest memory and setting up the vCPU state for boot.
"""

from dataclasses import dataclass
from pathlib import Path

from god.vcpu import registers
from god.vm.layout import RAM_BASE
from god.vm.memory import MemoryManager


@dataclass
class BootInfo:
    """
    Information about loaded boot components.

    This is returned by BootLoader.load() and contains all the
    addresses and sizes needed to boot the kernel.

    Attributes:
        kernel_addr: Guest physical address of kernel
        kernel_size: Size of kernel in bytes
        initrd_addr: Guest physical address of initramfs (0 if none)
        initrd_size: Size of initramfs in bytes (0 if none)
        dtb_addr: Guest physical address of DTB
        dtb_size: Size of DTB in bytes
    """

    kernel_addr: int
    kernel_size: int
    initrd_addr: int
    initrd_size: int
    dtb_addr: int
    dtb_size: int

    @property
    def initrd_end(self) -> int:
        """End address of initramfs (for Device Tree)."""
        return self.initrd_addr + self.initrd_size


class BootLoader:
    """
    Loads Linux boot components into guest memory.

    This class handles:
    - Loading the kernel at the correct offset
    - Loading initramfs after the kernel
    - Placing the DTB at a safe location
    - Setting up vCPU registers for boot

    Usage:
        loader = BootLoader(memory, ram_size)
        boot_info = loader.load(
            kernel_path="Image",
            initrd_path="initramfs.cpio",
            dtb_data=dtb_bytes,
        )
        loader.setup_vcpu(vcpu, boot_info)
    """

    def __init__(self, memory: MemoryManager, ram_size: int):
        """
        Create a boot loader.

        Args:
            memory: The guest memory manager
            ram_size: Size of guest RAM in bytes
        """
        self._memory = memory
        self._ram_size = ram_size
        self._ram_base = RAM_BASE

    def load(
        self,
        kernel_path: str | Path,
        initrd_path: str | Path | None = None,
        dtb_data: bytes | None = None,
    ) -> BootInfo:
        """
        Load boot components into guest memory.

        Args:
            kernel_path: Path to kernel Image file
            initrd_path: Path to initramfs (optional)
            dtb_data: DTB blob bytes (required)

        Returns:
            BootInfo with addresses of loaded components

        Raises:
            ValueError: If dtb_data is not provided
        """
        from .kernel import KernelImage

        if dtb_data is None:
            raise ValueError("DTB data is required")

        # Load kernel
        kernel = KernelImage.load(kernel_path)
        kernel_addr = self._ram_base + kernel.text_offset
        self._memory.write(kernel_addr, kernel.data)
        print(f"Loaded kernel at 0x{kernel_addr:08x} ({len(kernel.data)} bytes)")

        # Calculate where initramfs goes
        # Place it high in RAM (at 128MB offset) to avoid conflicts with
        # early kernel allocations which tend to be at low addresses
        kernel_end = kernel_addr + len(kernel.data)
        initrd_addr = self._ram_base + (128 * 1024 * 1024)  # 128 MB into RAM
        initrd_addr = (initrd_addr + 0xFFF) & ~0xFFF  # Align to 4KB

        # Load initramfs
        initrd_size = 0
        next_addr = initrd_addr
        if initrd_path is not None:
            initrd_path = Path(initrd_path)
            with open(initrd_path, "rb") as f:
                initrd_data = f.read()
            self._memory.write(initrd_addr, initrd_data)
            initrd_size = len(initrd_data)
            next_addr = initrd_addr + initrd_size

            # Debug: verify initramfs was loaded correctly
            readback = self._memory.read(initrd_addr, 16)
            magic_str = " ".join(f"{b:02x}" for b in readback[:8])
            print(f"Loaded initramfs at 0x{initrd_addr:08x} ({initrd_size} bytes)")
            print(f"  First 8 bytes: {magic_str}")
            if readback[:2] == b'\x1f\x8b':
                print("  Format: gzip compressed")
            elif readback[:6] == b'070701':
                print("  Format: cpio newc (uncompressed)")
            else:
                print(f"  Format: unknown (expected 1f 8b for gzip or 070701 for cpio)")

        # Place DTB right after initramfs (or kernel if no initramfs)
        # DTB must be:
        # - 8-byte aligned
        # - Within kernel's initial page table mapping (close to kernel)
        # - Not overlapping with kernel or initramfs
        dtb_addr = (next_addr + 0xFFF) & ~0xFFF  # Align to 4KB
        self._memory.write(dtb_addr, dtb_data)
        print(f"Loaded DTB at 0x{dtb_addr:08x} ({len(dtb_data)} bytes)")

        boot_info = BootInfo(
            kernel_addr=kernel_addr,
            kernel_size=len(kernel.data),
            initrd_addr=initrd_addr if initrd_size > 0 else 0,
            initrd_size=initrd_size,
            dtb_addr=dtb_addr,
            dtb_size=len(dtb_data),
        )

        # Final verification: check that memory at initrd_addr contains expected data
        if initrd_size > 0:
            verify_bytes = self._memory.read(boot_info.initrd_addr, 16)
            print(f"Verification: memory at 0x{boot_info.initrd_addr:08x} = "
                  f"{' '.join(f'{b:02x}' for b in verify_bytes[:8])}")

        return boot_info

    def setup_vcpu(self, vcpu, boot_info: BootInfo) -> None:
        """
        Configure vCPU registers for Linux boot.

        Sets up the ARM64 Linux boot protocol:
        - x0 = DTB address (physical)
        - x1, x2, x3 = 0 (reserved)
        - PC = kernel entry point (physical)
        - PSTATE = EL1h with interrupts masked

        Note: We do NOT set VBAR_EL1 or SP - the kernel manages its own
        exception vectors and stack. Setting SP to a physical address would
        cause problems after MMU is enabled (the kernel would try to use
        it for exception handling in virtual address space).

        Args:
            vcpu: The VCPU to configure
            boot_info: Boot information from load()
        """
        # x0 = DTB address (Linux boot protocol)
        vcpu.set_register(registers.X0, boot_info.dtb_addr)

        # x1, x2, x3 = 0 (reserved for future use)
        vcpu.set_register(registers.X1, 0)
        vcpu.set_register(registers.X2, 0)
        vcpu.set_register(registers.X3, 0)

        # PC = kernel entry point
        vcpu.set_pc(boot_info.kernel_addr)

        # PSTATE = EL1h with all interrupts masked
        # This is required by the ARM64 Linux boot protocol
        pstate = (
            registers.PSTATE_MODE_EL1H  # EL1, using SP_EL1
            | registers.PSTATE_D  # Mask Debug exceptions
            | registers.PSTATE_A  # Mask SError
            | registers.PSTATE_I  # Mask IRQ
            | registers.PSTATE_F  # Mask FIQ
        )
        vcpu.set_pstate(pstate)

        # Set up VBAR_EL1 and SP for early exception handling.
        # The kernel will update these later, but having valid values
        # helps if an exception occurs very early.
        vectors_phys = boot_info.kernel_addr + 0x10800
        vcpu.set_register(registers.VBAR_EL1, vectors_phys)

        stack_phys = boot_info.dtb_addr + boot_info.dtb_size
        stack_phys = (stack_phys + 0xFFF) & ~0xFFF  # Page align
        stack_phys += 0x10000  # Add 64KB for stack
        vcpu.set_sp(stack_phys)

        print(
            f"vCPU configured: PC=0x{boot_info.kernel_addr:08x}, "
            f"x0(DTB)=0x{boot_info.dtb_addr:08x}, "
            f"VBAR=0x{vectors_phys:08x}, SP=0x{stack_phys:08x}"
        )
