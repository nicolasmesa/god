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


if __name__ == "__main__":
    app()
