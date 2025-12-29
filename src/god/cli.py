"""
Command-line interface for the god VMM.

This module defines all CLI commands using the Typer library.
"""

from typing import Annotated

import typer

from god import __version__

app = typer.Typer(
    name="god",
    help="god - A Virtual Machine Monitor built from scratch",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        typer.echo(f"god {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", "-v", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """god - A Virtual Machine Monitor built from scratch."""
    pass


# Create a subcommand group for KVM-related commands
kvm_app = typer.Typer(help="KVM-related commands")
app.add_typer(kvm_app, name="kvm")


@kvm_app.command("info")
def kvm_info() -> None:
    """
    Display KVM system information.

    Shows the KVM API version, vCPU mmap size, and all supported capabilities.
    This is useful for verifying that KVM is working correctly and understanding
    what features are available on this system.
    """
    from god.kvm.capabilities import format_capabilities, query_capabilities
    from god.kvm.system import KVMError, KVMSystem

    try:
        with KVMSystem() as kvm:
            print("KVM System Information")
            print("=" * 60)
            print()
            print("Device:            /dev/kvm")
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
        raise typer.Exit(code=1) from e


@app.command("test-vm")
def test_vm(
    ram_mb: int = typer.Option(
        1024,
        "--ram",
        "-r",
        help="RAM size in megabytes",
    ),
):
    """
    Test VM creation and memory setup.

    Creates a VM, allocates memory, writes some data, reads it back,
    and verifies everything works.
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vm.memory import MemoryError

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")
    print()

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                print(f"VM created: {vm}")
                print()
                print("Memory slots:")
                for slot in vm.memory.slots:
                    print(f"  {slot}")
                print()

                # Write some data to memory
                test_address = vm.ram_base
                test_data = b"Hello from the VMM!"

                print(f"Writing test data to 0x{test_address:08x}...")
                vm.memory.write(test_address, test_data)

                # Read it back
                print(f"Reading back from 0x{test_address:08x}...")
                read_back = vm.memory.read(test_address, len(test_data))

                if read_back == test_data:
                    print(f"Success! Read: {read_back}")
                else:
                    print(f"MISMATCH! Wrote: {test_data}, Read: {read_back}")
                    raise typer.Exit(code=1)

                print()
                print("VM test passed!")

    except (KVMError, VMError, MemoryError) as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1)


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
        help="Enable PL011 UART for serial console output",
    ),
):
    """
    Run a binary in the VM.

    Loads the binary at the entry point address and runs until it halts.
    By default, the PL011 UART is enabled so guest code can print output.

    Example:
        god run tests/guest_code/hello.bin
        god run my_kernel.bin --entry 0x40000000 --ram 128
    """
    from god.kvm.system import KVMSystem, KVMError
    from god.vm.vm import VirtualMachine, VMError
    from god.vcpu.runner import VMRunner, RunnerError
    from god.vcpu import registers
    from god.devices import DeviceRegistry, PL011UART

    # Parse entry point (support hex with 0x prefix or decimal)
    entry_point = int(entry, 16) if entry.startswith("0x") else int(entry)

    ram_bytes = ram_mb * 1024 * 1024

    print(f"Creating VM with {ram_mb} MB RAM...")

    try:
        with KVMSystem() as kvm:
            with VirtualMachine(kvm, ram_size=ram_bytes) as vm:
                # Set up device registry
                devices = DeviceRegistry()

                if with_uart:
                    uart = PL011UART()
                    devices.register(uart)

                runner = VMRunner(vm, kvm, devices)
                vcpu = runner.create_vcpu()

                # Set initial register state
                # PC = entry point (where code starts)
                vcpu.set_pc(entry_point)

                # SP = top of RAM (stack grows down)
                stack_top = vm.ram_base + vm.ram_size
                vcpu.set_sp(stack_top)

                # PSTATE = EL1h with all interrupts masked
                # EL1h means: Exception Level 1, using SP_EL1
                # This is "kernel mode" on ARM64
                pstate = (
                    registers.PSTATE_MODE_EL1H |  # Exception Level 1, SP_EL1
                    registers.PSTATE_A |           # Mask async aborts
                    registers.PSTATE_I |           # Mask IRQs
                    registers.PSTATE_F             # Mask FIQs
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


if __name__ == "__main__":
    app()
