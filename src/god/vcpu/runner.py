"""
VM execution runner.

This module provides the run loop that executes guest code and handles VM exits.
The run loop is the heart of the VMM - it's a simple concept:

1. Tell the vCPU to run (KVM_RUN)
2. Guest executes until something happens
3. Check why it stopped (exit_reason)
4. Handle the exit (emulate device, report error, etc.)
5. Go back to step 1

Most guest execution is "exit-driven" - the VMM spends most of its time
waiting for the guest to do something that requires attention.
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
from god.devices import DeviceRegistry, MMIOAccess, GIC
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
    controller) which is required for the guest to receive interrupts.

    The initialization sequence is:
    1. VMRunner() - creates the GIC (required before vCPUs)
    2. create_vcpu() - creates vCPUs (can call multiple times)
    3. run() - finalizes GIC and starts execution

    The GIC must be created before vCPUs, but finalized after all vCPUs
    exist. VMRunner handles this automatically.

    Usage:
        runner = VMRunner(vm, kvm)  # GIC created here
        vcpu0 = runner.create_vcpu()
        vcpu1 = runner.create_vcpu()  # Multi-vCPU supported

        # Set up initial state
        vcpu0.set_pc(entry_point)
        vcpu0.set_sp(stack_top)

        # Load code
        runner.load_binary("/path/to/binary", entry_point)

        # Run (GIC finalized automatically, then execution starts)
        stats = runner.run()

        # Inject interrupts
        runner.gic.inject_irq(33, level=True)
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

        This sets up the VM infrastructure including the GIC (interrupt
        controller). The GIC must exist before vCPUs can be created.

        Args:
            vm: The VirtualMachine to run.
            kvm: The KVMSystem instance.
            devices: Device registry for MMIO handling. If not provided,
                     a new empty registry is created.
            create_gic: If True (default), create the GIC automatically.
                        Set to False only if you want to manage the GIC
                        yourself (rare).
        """
        self._vm = vm
        self._kvm = kvm
        self._devices = devices if devices is not None else DeviceRegistry()
        self._vcpus: list[VCPU] = []
        self._gic: GIC | None = None

        # Create the GIC (interrupt controller)
        # This must happen before any vCPUs are created.
        if create_gic:
            self._gic = GIC(self._vm.fd)
            self._gic.create()

    @property
    def devices(self) -> DeviceRegistry:
        """Get the device registry."""
        return self._devices

    @property
    def gic(self) -> GIC | None:
        """Get the GIC (interrupt controller)."""
        return self._gic

    @property
    def vcpus(self) -> list[VCPU]:
        """Get all created vCPUs."""
        return self._vcpus

    def create_vcpu(self) -> VCPU:
        """
        Create and return a vCPU.

        You can create multiple vCPUs by calling this method multiple times.
        The GIC will be finalized when run() is called, after all vCPUs exist.

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
        # Get MMIO access details from the vCPU
        phys_addr, data_bytes, length, is_write = vcpu.get_mmio_info()

        # Convert bytes to int for the device
        if is_write:
            data = int.from_bytes(data_bytes, "little")
        else:
            data = 0

        # Package the access for the device registry
        access = MMIOAccess(
            address=phys_addr,
            size=length,
            is_write=is_write,
            data=data,
        )

        # Dispatch to the appropriate device
        result = self._devices.handle_mmio(access)

        # For reads, we need to return data to the guest
        if not is_write:
            result_bytes = result.data.to_bytes(length, "little")
            vcpu.set_mmio_data(result_bytes)

        return result.handled

    def run(self, max_exits: int = 100000, quiet: bool = False) -> dict:
        """
        Run the VM until it halts or hits max_exits.

        The GIC is automatically finalized before the first vCPU runs.
        This ensures all vCPUs are created before GIC finalization.

        The run loop:
        1. Call vcpu.run() - guest executes until something happens
        2. Check exit_reason to see what happened
        3. Handle the exit appropriately
        4. Repeat

        Args:
            max_exits: Maximum number of VM exits before giving up.
                       This prevents infinite loops during development.
                       Default is 100000 (enough for a full boot).
            quiet: If True, suppress debug output for normal operations
                   like MMIO. Errors are always printed.

        Returns:
            Dict with execution statistics:
            - exits: Total number of exits
            - hlt: Whether the guest halted normally
            - exit_reason: Name of the final exit reason
            - exit_counts: Dict mapping exit names to counts

        Raises:
            RunnerError: If no vCPU was created, or on fatal errors.
        """
        if not self._vcpus:
            raise RunnerError("No vCPUs created - call create_vcpu() first")

        # Finalize GIC before running
        # This must happen after all vCPUs are created so the GIC
        # can set up per-CPU redistributors for each one.
        if self._gic is not None and not self._gic.finalized:
            self._gic.finalize()

        stats = {
            "exits": 0,
            "hlt": False,
            "exit_reason": None,
            "exit_counts": {},
        }

        # For now, we only run the first vCPU
        # Multi-vCPU execution requires threading (future work)
        vcpu = self._vcpus[0]

        for _ in range(max_exits):
            # Run the vCPU - this blocks until the guest exits
            exit_reason = vcpu.run()

            # Handle signal interruption (EINTR)
            # This can happen if we receive a signal while in KVM_RUN
            if exit_reason == -1:
                continue

            # Track statistics
            stats["exits"] += 1
            exit_name = vcpu.get_exit_reason_name(exit_reason)
            stats["exit_counts"][exit_name] = (
                stats["exit_counts"].get(exit_name, 0) + 1
            )
            stats["exit_reason"] = exit_name

            # Handle the exit based on its type
            if exit_reason == KVM_EXIT_HLT:
                # Guest executed HLT (or WFI on ARM) - it's done
                stats["hlt"] = True
                break

            elif exit_reason == KVM_EXIT_MMIO:
                # Guest tried to access memory that isn't RAM
                # Dispatch to the device registry to handle it
                self._handle_mmio(vcpu)

            elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
                # Guest requested shutdown or reset
                # On ARM, this usually comes through PSCI (Power State
                # Coordination Interface)
                if not quiet:
                    print("\n[Guest requested shutdown/reset]")
                break

            elif exit_reason == KVM_EXIT_INTERNAL_ERROR:
                # Something went wrong inside KVM
                print("\n[KVM internal error]")
                vcpu.dump_registers()
                raise RunnerError("KVM internal error")

            elif exit_reason == KVM_EXIT_FAIL_ENTRY:
                # The CPU failed to enter guest mode
                # Usually means we set up the vCPU state incorrectly
                print("\n[Failed to enter guest mode]")
                vcpu.dump_registers()
                raise RunnerError("Entry to guest mode failed")

            else:
                # Unknown exit - print info and stop
                if not quiet:
                    print(f"\n[Unhandled exit: {exit_name}]")
                    vcpu.dump_registers()
                break

        return stats
