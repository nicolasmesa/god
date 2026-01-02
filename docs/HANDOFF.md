# Agent Handoff: VMM Tutorial Implementation

## Development Environment Setup

**IMPORTANT**: This project requires running inside a Lima VM with KVM support. You cannot run the VMM code directly on macOS.

### Starting the Lima VM

```bash
# From the project root on your Mac
limactl start lima/god.yaml
limactl shell god
```

### Inside the Lima VM

Once inside the VM, you'll be at a bash prompt. The project is mounted at `~/workplace/veleiro-god`:

```bash
cd ~/workplace/veleiro-god

# Run commands with the god alias (automatically uses sudo + correct venv)
god --help
god kvm info

# Or use uv directly
sudo uv run god --help
```

### Key Details

- **Venv location**: `/opt/god/venv` (VM-local, not mounted from host)
- **`uv` is available system-wide**: Works with both regular user and `sudo`
- **`UV_PROJECT_ENVIRONMENT` is preserved**: When using `sudo`, the env var is kept
- **Aliases available**:
  - `god` → `sudo uv run --directory ~/workplace/veleiro-god god`
  - `clskip` → `claude --dangerously-skip-permissions`

### Why sudo?

KVM requires root access (or `kvm` group membership). The Lima VM is configured to run `uv` commands with `sudo` to access `/dev/kvm`.

### Stopping the VM

```bash
# From your Mac (not inside the VM)
limactl stop god
```

---

## Project Context

We are building a **Virtual Machine Monitor (VMM)** from scratch using Python and KVM on ARM64. The ultimate goal is to boot a full Linux kernel inside our virtual machine.

**Key Principles:**
- This is an **educational project** - learning is the primary goal
- **No unexplained acronyms** - every term must be defined when first used
- **Documentation is paramount** - we want to deeply understand how everything works
- Performance and security are secondary to understanding (but we document trade-offs)

## Project Structure

```
veleiro-god/
├── lima/
│   └── god.yaml       # Lima VM configuration (start here!)
├── src/god/           # Main Python package
│   ├── cli.py         # Typer CLI commands
│   ├── kvm/           # KVM interface layer
│   ├── vm/            # Virtual machine management
│   ├── vcpu/          # Virtual CPU management
│   ├── devices/       # Emulated devices
│   └── boot/          # Linux boot support
├── tests/
│   └── guest_code/    # ARM64 assembly test programs
└── docs/
    └── tutorial/      # Tutorial chapters (this is what we're implementing)
```

## Tutorial Chapters

| Chapter | Topic | Status |
|---------|-------|--------|
| 00 | Introduction & Setup | Draft |
| 01 | KVM Foundation | Draft |
| 02 | VM Creation & Memory | Draft |
| 03 | vCPU & Run Loop | Draft |
| 04 | Serial Console (PL011) | Draft |
| 05 | Interrupt Controller (GIC) | Draft |
| 06 | Timer | Draft |
| 07 | Booting Linux | Draft |
| 08 | Virtio Devices | Draft |
| 09 | Appendix | Draft |

---

## Current Assignment

### Chapter to Implement: `[CHAPTER_NUMBER]`

**Chapter File:** `docs/tutorial/[CHAPTER_FILENAME]`

---

## Your Mission

Work through this chapter **together with the user**, implementing the code step-by-step. This is a learning exercise, so:

### 1. Go Slowly

- Don't rush through the material
- Implement one section at a time
- Verify each piece works before moving on

### 2. Encourage Questions

- If something seems unclear, ask the user if they understand
- If the user asks a question, answer it thoroughly
- If a concept needs more explanation, provide it

### 3. Test Everything

- After writing code, test it immediately
- Don't accumulate untested code
- If something breaks, debug it together

### 4. Document As You Go

- Note any clarifications that come up
- Track any code changes from the original draft
- Record any "aha moments" or insights

---

## Workflow

### Starting the Chapter

1. Read the chapter draft together with the user
2. Identify what needs to be implemented
3. Create a plan for the implementation steps

### For Each Section

1. **Explain** the concept (verify user understanding)
2. **Implement** the code together
3. **Test** that it works
4. **Discuss** any issues or questions that arise

### When Issues Arise

- If the draft code doesn't work, **fix it and explain why**
- If a concept is confusing, **add clarification to the chapter**
- If something is missing, **add it**

---

## End Deliverables

When the chapter is complete, you must update the chapter file with:

### 1. Incorporated Clarifications

Any questions or discussions that came up during implementation should be **woven into the chapter text itself**.

**DO NOT** add:
- FAQ sections
- "Q&A" blocks
- Footnotes with clarifications

**DO** update the relevant sections to include the clarification naturally, as if it was always part of the explanation.

**Example:**

Before (original draft):
```markdown
We use `MAP_SHARED` for the kvm_run mapping.
```

After (with clarification incorporated):
```markdown
We use `MAP_SHARED` for the kvm_run mapping. This is different from `MAP_PRIVATE`
which we used for guest RAM. The difference matters here because kvm_run is
shared with the kernel - when KVM writes exit information, we need to see those
changes. With `MAP_PRIVATE`, we'd get our own copy and never see the kernel's updates.
```

### 2. Fixed Code

If any code in the draft didn't work or needed changes:

1. **Update the code in the chapter** to the working version
2. **Add explanation** of why the fix was needed (if it's educational)
3. **Keep the narrative flowing** - don't say "we fixed a bug", just present the correct code

### 3. Working Implementation

The actual code files in `src/god/` should be created/updated and working:

- All code from the chapter should be implemented
- Tests should pass
- The chapter's "success criteria" should be met

---

## Testing Environment

We're on an **M4 Mac** and use **Lima** to run an ARM64 Linux VM for testing. See the **Development Environment Setup** section at the top of this document for full details.

**Quick reference:**
```bash
# Start/enter the VM
limactl start lima/god.yaml
limactl shell god

# Inside VM - run commands
god kvm info
god run tests/guest_code/hello.bin
```

KVM is available at `/dev/kvm` inside the Lima VM and requires root access.

---

## Important Reminders

1. **No unexplained acronyms** - If you use an acronym, define it
2. **Explain the "why"** - Don't just show code, explain reasoning
3. **Test incrementally** - Don't write 100 lines before testing
4. **Ask questions** - If the user seems confused, check in
5. **Update the chapter** - The deliverable is an improved chapter, not just working code

---

## Success Criteria

The chapter is complete when:

1. All code from the chapter is implemented and working
2. The chapter's stated success criteria are met
3. Any questions/clarifications from the session are incorporated into the text
4. Any code fixes are reflected in the chapter with explanations
5. The user understands the material (not just copied it)

---

## Let's Begin!

Start by reading through Chapter `[CHAPTER_NUMBER]` together and identifying the first thing to implement.

Ask the user: *"Ready to start with [first section]? Do you have any questions about what we're about to build?"*
