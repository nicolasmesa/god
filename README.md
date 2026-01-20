# Veleiro God

An educational Virtual Machine Monitor (VMM) built from scratch in Python. This project boots a real Linux kernel on ARM64 using KVM, with the primary goal of deeply understanding how virtualization works at every level.

## What This Project Does

Veleiro God is a working hypervisor that:

- Boots Linux 6.12 kernel on ARM64 architecture
- Emulates essential hardware: UART serial console, Generic Interrupt Controller (GIC), ARM timer
- Provides a complete execution environment with BusyBox userspace
- Uses Python + cffi for readable, understandable code over raw performance

This is **not** intended for production use. It's a learning tool for understanding operating systems, virtualization internals, and systems programming.

## Prerequisites

- **ARM64 Linux host with KVM support** (or x86_64 with nested virtualization)
- **Python 3.12+**
- **uv** package manager

### Running on macOS

Since macOS doesn't have KVM, use [Lima](https://lima-vm.io/) to run an ARM64 Linux VM with nested KVM support:

```bash
# Install Lima
brew install lima

# Start the VM using the included configuration
limactl start lima/god.yaml

# Enter the VM
limactl shell god

# Navigate to the project (mounted automatically)
cd /Users/<your-username>/workplace/veleiro-god
```

The Lima configuration includes Docker, uv, and KVM support pre-configured.

## Installation

```bash
# Install dependencies
uv sync

# Verify KVM access
uv run god kvm info
```

## Usage

### Check KVM capabilities

```bash
uv run god kvm info
```

### Boot Linux

```bash
uv run god run \
    --kernel build/linux/arch/arm64/boot/Image \
    --initramfs build/initramfs.cpio.gz
```

You'll see the Linux kernel boot and get a BusyBox shell.

### Run test programs

The project includes ARM64 assembly test programs in `tests/guest_code/`:

```bash
# Run the hello world test
uv run god run --binary tests/guest_code/hello.bin
```

## Project Structure

```
veleiro-god/
├── src/god/                  # Main VMM implementation
│   ├── cli.py               # Command-line interface
│   ├── kvm/                 # Low-level KVM bindings via cffi
│   ├── vm/                  # Virtual machine management
│   ├── vcpu/                # Virtual CPU handling
│   ├── devices/             # Emulated hardware
│   │   ├── uart.py          # PL011 serial console
│   │   ├── gic.py           # Interrupt controller
│   │   ├── timer.py         # ARM generic timer
│   │   └── virtio/          # Virtio devices
│   └── boot/                # Linux kernel boot support
├── docs/tutorial/           # Educational tutorials (14,000+ lines)
├── tests/                   # Test suite
│   └── guest_code/          # ARM64 assembly test programs
├── build/                   # Built kernel and initramfs
└── lima/                    # Lima VM configuration for macOS
```

## Tutorial

The project includes extensive documentation explaining every concept:

| Chapter | Topic |
|---------|-------|
| [00 - Introduction](docs/tutorial/00-introduction.md) | Project overview and setup |
| [01 - KVM Foundation](docs/tutorial/01-kvm-foundation.md) | KVM API basics |
| [02 - VM Creation & Memory](docs/tutorial/02-vm-creation-memory.md) | Memory management |
| [03 - vCPU Run Loop](docs/tutorial/03-vcpu-run-loop.md) | CPU execution |
| [04 - Serial Console](docs/tutorial/04-serial-console.md) | UART emulation |
| [05 - Interrupt Controller](docs/tutorial/05-interrupt-controller.md) | GIC implementation |
| [06 - Timer](docs/tutorial/06-timer.md) | Timer handling |
| [07 - Booting Linux](docs/tutorial/07-booting-linux.md) | Linux kernel boot |
| [08 - Virtio Devices](docs/tutorial/08-virtio-devices.md) | Paravirtualized I/O |
| [09 - Appendix](docs/tutorial/09-appendix.md) | Reference material |

## Architecture

```
┌─────────────────────────────────────────────┐
│              CLI (god command)              │
├─────────────────────────────────────────────┤
│  Emulated Devices (UART, GIC, Timer, etc.)  │
├─────────────────────────────────────────────┤
│      VM / vCPU / Memory Management          │
├─────────────────────────────────────────────┤
│         KVM Interface (cffi bindings)       │
├─────────────────────────────────────────────┤
│           Linux Kernel (KVM module)         │
└─────────────────────────────────────────────┘
```

## Development

```bash
# Run tests
uv run pytest

# Format code
uv run ruff format

# Lint
uv run ruff check
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for detailed development guidelines.

## License

Educational use.
