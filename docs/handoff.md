# Project Handoff: AI-First OS (working name TBD)

## Goal
A bare Linux-based OS where the primary interface is a prompt (voice first, text second). There are no traditional apps. When the user wants to do something ("edit this photo"), the system generates a purpose-built tool on demand from trusted primitives, then saves and refines it for reuse. Inspiration: the Star Trek TNG computer and holodeck, where software is generated from intent rather than installed.

Explicitly **not** a copilot or assistant bolted onto an existing desktop.

## Decisions made by the user
- **Base:** Linux, for a working kernel and drivers.
- **UI:** Stripped back. No desktop metaphor, no app switching.
- **Primary input/output:** Speech-to-text and text-to-speech.
- **Secondary UI:** Side panel with chat history, plus a text input box.
- **Escape hatch:** Terminal always available.
- **Primitives:** GTK or KDE/Qt widgets.
- **Composition:** No app switching. To combine two things, the user just asks.
- **Files:** A file browser may not be needed; the user already prefers asking an AI over browsing Google Drive. Keep as a fallback at most.
- **Security:** A root agent acts as traffic cop, granting permissions to per-task workspace agents.

## Proposed architecture (suggestions, open to revision)
| Layer | Proposal | Why |
|---|---|---|
| Base distro | NixOS | Whole system is declarative and every change is rollback-able, so agents can modify the system safely. |
| Filesystem | btrfs with automatic snapshots of home | "Undo whatever the AI just did" as a core feature. |
| Compositor | Sway, or Cage (single fullscreen surface) | Minimal Wayland base. |
| Shell | One app: chat panel, voice indicator, canvas for generated tools | The only "real" app. |
| STT | whisper.cpp (local) | Low latency. |
| TTS | Piper (local) | Low latency. |
| Wake word | openWakeWord | Hands-free activation. |
| Generated UI | Model emits a **declarative JSON UI spec**; a fixed renderer maps it to GTK4 (or Qt) widgets | Faster than generating GTK code, can't crash the shell, easy to save/reuse. Real code is only generated for data processing, and runs sandboxed. |
| Composition | Tools exchange typed data over a shared bus (D-Bus or a small local broker) | Unix-pipes model: the agent wires tools together. |
| File access | SQLite index + embeddings + metadata over the real filesystem | Natural-language retrieval ("the contract Dave sent in March"). |
| Sandboxing | Each workspace agent in a bubblewrap container: no network by default, only its task folder mounted. Landlock + seccomp for syscalls, systemd-run for CPU/memory limits. | Least privilege per task. |
| Root broker | Mostly **deterministic rules, not an LLM**. Grants extra access via a Flatpak-portal-style prompt ("this tool wants ~/Photos, allow?"). | A prompt injection in a document must not be able to talk it into escalating privileges. |

## MVP order (proposed)
1. Voice + chat shell running on top of a normal desktop.
2. JSON-spec to GTK widget renderer.
3. Sandboxed workspace agents + root broker.
4. Saved and reusable generated tools.
5. Remove the regular desktop only once it is no longer being used.

## Dev environment
- Host: macOS on Apple Silicon (ARM64).
- NixOS fully supports aarch64-linux (official ISOs, binary cache).
- VM options:
  - **UTM** (free): QEMU backend with virtio-gpu for accelerated Wayland graphics. Apple Virtualization backend is faster but has weaker graphics.
  - **VMware Fusion / Parallels:** smoother graphics and integration if UTM is sluggish.
  - **OrbStack / Lima:** headless; good for building agent and sandbox layers, not the GUI shell.
- Tip: run whisper.cpp natively on macOS (Metal GPU) during prototyping and stream text into the VM to avoid VM audio latency.
- The Nix package manager (or nix-darwin) can run natively on macOS to prototype agent code before moving Linux-specific parts into the VM.

## Open questions
- GTK4 or Qt/KDE for the widget renderer? (Pick one.)
- Local model vs cloud API for the main agent, or a hybrid.
- Exact schema for the JSON UI spec and the inter-tool data format.
- How saved tools are stored, versioned and refined over time.
- Language for the shell and broker (e.g., Python + PyGObject for speed of iteration, or Rust for the broker).

## Known hard problems
- **Consistency:** shifting UIs break muscle memory; saved tools mitigate this.
- **Latency and cost** of generating interfaces versus launching an installed app.
- **Reliability** of generated tools touching real files (snapshots mitigate).
- **Precision:** language is poor for fine adjustments; generated tools still need sliders and direct manipulation.
