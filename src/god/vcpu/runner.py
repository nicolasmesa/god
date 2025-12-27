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
        runner = VMRunner(vm, kvm)
        vcpu = runner.create_vcpu()

        # Set up initial state
        vcpu.set_pc(entry_point)
        vcpu.set_sp(stack_top)
        vcpu.set_pstate(...)

        # Load code
        runner.load_binary("/path/to/binary", entry_point)

        # Run!
        stats = runner.run()
    """

    def __init__(self, vm: VirtualMachine, kvm: KVMSystem):
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
        """
        Create and return a vCPU.

        Currently we only support a single vCPU.

        Returns:
            The created VCPU.

        Raises:
            RunnerError: If a vCPU was already created.
        """
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

    def run(self, max_exits: int = 1000) -> dict:
        """
        Run the VM until it halts or hits max_exits.

        The run loop:
        1. Call vcpu.run() - guest executes until something happens
        2. Check exit_reason to see what happened
        3. Handle the exit appropriately
        4. Repeat

        Args:
            max_exits: Maximum number of VM exits before giving up.
                       This prevents infinite loops during development.

        Returns:
            Dict with execution statistics:
            - exits: Total number of exits
            - hlt: Whether the guest halted normally
            - exit_reason: Name of the final exit reason
            - exit_counts: Dict mapping exit names to counts

        Raises:
            RunnerError: If no vCPU was created, or on fatal errors.
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
                # This is how devices are accessed - we need to emulate them
                phys_addr, data, length, is_write = vcpu.get_mmio_info()

                if is_write:
                    print(
                        f"MMIO write: addr=0x{phys_addr:08x} "
                        f"data={data.hex()} len={length}"
                    )
                    # For now, just ignore writes
                else:
                    print(
                        f"MMIO read: addr=0x{phys_addr:08x} len={length}"
                    )
                    # Return zeros for now - we don't have any devices yet
                    vcpu.set_mmio_data(bytes(length))

            elif exit_reason == KVM_EXIT_SYSTEM_EVENT:
                # Guest requested shutdown or reset
                # On ARM, this usually comes through PSCI (Power State
                # Coordination Interface)
                print("Guest requested shutdown/reset")
                break

            elif exit_reason == KVM_EXIT_INTERNAL_ERROR:
                # Something went wrong inside KVM
                print("KVM internal error!")
                vcpu.dump_registers()
                raise RunnerError("KVM internal error")

            elif exit_reason == KVM_EXIT_FAIL_ENTRY:
                # The CPU failed to enter guest mode
                # Usually means we set up the vCPU state incorrectly
                print("Failed to enter guest mode!")
                vcpu.dump_registers()
                raise RunnerError("Entry to guest mode failed")

            else:
                # Unknown exit - print info and stop
                print(f"Unhandled exit: {exit_name}")
                vcpu.dump_registers()
                break

        return stats
