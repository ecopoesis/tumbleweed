# Tumbleweed

An AI-first OS. The primary interface is a prompt: voice first, text second. There are no
installed apps. When you want to do something, Tumbleweed generates a purpose-built tool
from trusted primitives, sandboxes anything that runs, and saves the tool for reuse.

**Status:** pre-alpha, under construction. See [`docs/PLAN.md`](docs/PLAN.md) for the v1
plan and progress, [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it fits
together, and [`docs/handoff.md`](docs/handoff.md) for the original vision. What exists so
far is the typed foundation in `src/tumbleweed/core`, the build and test setup around it, and the
GTK4 shell skeleton (`tumbleweed-shell`: a window with a canvas, a chat panel that talks to Claude,
and a status strip; see [Running the shell](#running-the-shell) and
[Talking to Claude](#talking-to-claude)), the renderer core that turns a UI spec into live
widgets (`tumbleweed-render`; see [UI spec](#ui-spec)), push-to-talk voice input to a local
whisper.cpp ([Voice input](#voice-input)), and spoken replies through Piper
([Speech output](#speech-output)).

## Development setup (macOS)

You need [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/) and, for the Linux
tests, Docker Desktop.

```sh
brew install gtk4 libadwaita gobject-introspection pkg-config cairo uv
uv sync
```

`uv sync` uses Python 3.12 (see `.python-version`), installs the locked dependencies into
`.venv`, and builds PyGObject from source against Homebrew's GTK. If that build fails, check
that `pkg-config --modversion girepository-2.0` prints a version.

On Apple Silicon, Homebrew's libraries (`/opt/homebrew/lib`) are not on the dynamic linker's
search path, and the GTK typelibs name their libraries without a path. So that PyGObject can
load them, put this in your shell profile (`~/.zshrc`):

```sh
export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"
```

Without it, importing GTK fails with a bare `AssertionError` from `gi/overrides` and a
`Failed to load shared library 'libgobject-2.0.0.dylib'` warning, unless your Python happens
to be built with Homebrew's lib directory in its rpath. gtk tests then skip, with that reason.
macOS strips `DYLD_*` variables from processes started through system shells and
`/usr/bin/env`; `uv run ...` from your own shell is fine. One catch: when the path of the
venv is long (over 127 characters), uv gives console scripts such as `pytest` a `/bin/sh`
shebang, and the variable is lost. Run `uv run python -m pytest` then.

This is for the tools you run yourself (pytest, a Python prompt): `tumbleweed-shell` does not
need it, because it finds Homebrew's libraries on its own (next section).

Check the setup:

```sh
uv run python -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print(Gtk.get_major_version())"
```

### Running the shell

```sh
uv run tumbleweed-shell            # the same as: uv run python -m tumbleweed.shell
uv run tumbleweed-shell --version  # answered at once, with no window and no GTK
```

One window opens, with no desktop around it:

- the **canvas** (left), where generated tools render (`CanvasView.show_spec`); it shows a placeholder
  until a tool is shown, and nothing in the shell shows one yet (S12 connects the agent);
- the **side panel** (right): the chat transcript, a multi-line input and a Send button. Messages are
  answered by Claude, streamed in as they are written (with no key configured the chat says how to
  set one; `responder = "echo"` brings back the S02 placeholder that echoes). While a reply streams
  the Send button waits, and the input stays editable; **Esc** stops the reply (while it is being read
  aloud, the first Esc stops the voice and a second the reply: [Speech output](#speech-output));
- the **status strip** (bottom) with the voice indicator (idle, listening, transcribing, speaking; thinking
  arrives with S08), the **Mic** button ([Voice input](#voice-input)), the **Mute** button
  ([Speech output](#speech-output), there when Piper is set up) and a **Terminal** button;
- a **terminal pane** (above the status strip, hidden until you open it), see "The terminal" below;
- a **reserved strip for trusted prompts** along the top, outside the canvas, which is empty until S18.

Enter sends, Shift+Enter inserts a newline. Ctrl+L focuses the input (and shows the side panel if it
is hidden), F9 shows or hides the side panel, F12 or the Terminal button opens the terminal, Ctrl+Q quits. (Ctrl, also on a Mac.) SIGTERM and Ctrl+C
in the terminal quit the same way. Text from the chat is shown as plain text, never as markup, and
very long or hostile text (a 5000-character grapheme cluster, 64 KiB with no spaces) cannot widen the
window or freeze it: it is shown with zero-width spaces added. Ctrl+C and Copy in a message's menu put
the message's own text on the clipboard, without them, and so does dragging the selection out. A
selection pasted with a middle click (the primary selection) still has them: GTK puts its laid-out text
there, and replacing it makes the label drop the selection (#63 stays open for it).

The command takes no arguments but `--help` and `--version`; anything else, a file name included, is
refused with a one-line error and exit status 2. The application is non-unique: it claims no
well-known name on the session bus, and a second `tumbleweed-shell` is a second shell, not a request
to the first. That does **not** keep it off the bus: GLib still exports the application's and the
window's actions (`quit`, `focus-input`, `toggle-side-panel`) there, so any process of your user that
can reach the session bus can activate them. A test starts a private bus and pins that list (#54);
nothing security-relevant, such as a permission prompt or a grant, is ever to be an action, nor is
anything that starts a process: the terminal's key and button are not actions. Without a
display (no `WAYLAND_DISPLAY` or `DISPLAY` that works) the command says so in one line and exits
with status 1.

### The terminal

A terminal is one keystroke away whether or not an API key is configured, and whatever state the agent
is in (no key, an unreadable config file, a failed reply): **F12** or the **Terminal** button in the
status strip. It is your tool, not the agent's: nothing the model says can type into it or read it.

- **Where VTE (the GTK 4 build, `gir1.2-vte-3.91` on Debian and Ubuntu) is installed**, which includes the
  Linux image: the terminal is a pane under the canvas running your **login shell** as the passwd database
  (`getent passwd`) has it, not `$SHELL`, in your home directory. F12 or the button again hides the pane and
  keeps the shell; `exit` closes the pane and the next F12 starts a new shell; closing the window ends the
  shell (SIGHUP, then SIGKILL after a second). While the terminal has the focus, Ctrl+L is the shell's (clear
  screen) and not "focus the input"; Ctrl+Q and F9 still act on the shell (#90). Ctrl+Space is the
  shell's too (set-mark in Emacs and readline): push-to-talk ([Voice input](#voice-input)) does nothing,
  and lets the key through untouched, while the terminal has the focus; use the Mic button there. Esc is yours too, except that it abandons a voice utterance in progress (not stopping a reply being spoken: Esc stays the terminal's while it has the focus).
- **Where there is no VTE** (macOS, whose Homebrew has no GTK 4 build of it): F12 opens an external terminal
  window, in your home directory: Terminal.app (`open -b com.apple.Terminal`) on a Mac, otherwise the first of
  `x-terminal-emulator`, `gnome-terminal`, `kgx`, `konsole`, `xfce4-terminal`, `alacritty`, `kitty`, `foot`,
  `xterm` found on your `PATH`. Every F12 opens another window (at most eight started from one shell are
  kept track of); closing the shell ends those still running as its children (not Terminal.app on a
  Mac, nor terminals that hand the window to a server, such as gnome-terminal). If none is found the pane says which to
  install.
- The terminal's environment is built from nothing, not copied: your `PATH`, locale, display and session
  variables (including `XAUTHORITY`), `HOME`, `SHELL`, `USER` and `LOGNAME` from your passwd entry, and
  the constants `TERM` (`xterm-256color`) and `COLORTERM` (`truecolor`). On a Mac, Terminal.app's shell gets
  your login environment from the system, not this one. Your API key, and any other
  variable, is not passed. Programs are started from argument lists, never through a shell, and nothing
  configures which terminal is used. Why: `docs/DECISIONS.md` D18.

**On macOS** the command works from any shell, with or without the `DYLD_FALLBACK_LIBRARY_PATH` line
above. Homebrew's libraries are not on dyld's search path, and the variable is read when a process
starts, so `tumbleweed-shell` starts itself again once with `/opt/homebrew/lib` first on
`DYLD_FALLBACK_LIBRARY_PATH` (if the variable was unset it gets the default that `man dyld` documents
for older binaries, `/usr/local/lib:/usr/lib`, because setting it replaces that; nothing derived from
`$HOME` is ever added), and `/opt/homebrew/share` added to `XDG_DATA_DIRS` if that is set (a terminal
that sets it without Homebrew's directory leaves GTK without its GSettings schemas). The relaunch is
an `execve` of the same Python with the same arguments, so it also works when uv gives the console
script a `/bin/sh` shebang, which macOS strips `DYLD_*` variables from. It happens only on macOS, only
for Apple Silicon's `/opt/homebrew` (Intel Homebrew's `/usr/local/lib` is on dyld's path already),
and only once. The directory is fixed in the code, not read from `HOMEBREW_PREFIX` or from `brew`.
Once GTK is loaded the shell puts your two variables back into its environment as you had them, so
programs it starts, and a `tumbleweed-shell` started from it, do not inherit ours. On Linux nothing of
this runs: the process and its environment are left as they are. If GTK still cannot be loaded, the
command says so in one line and exits with status 1.

**In the container**, the Linux image has no screen, so the shell runs on its headless compositor
and is for tests, not for looking at: `scripts/test-linux uv run pytest -m gtk -v` runs the GTK tests,
which include starting the real `python -m tumbleweed.shell` there (also with warnings as errors),
waiting for its window, and stopping it with SIGTERM and SIGINT. To try the shell on a real Linux
desktop, run `uv run tumbleweed-shell` in a session with GTK 4.14 and libadwaita 1.5 installed.

### Talking to Claude

The chat is answered by the agent (`tumbleweed.agent`) through the Anthropic SDK, behind an
`LlmProvider` interface (DECISIONS D3). It needs an API key. Either export it,

```sh
ANTHROPIC_API_KEY=sk-ant-... uv run tumbleweed-shell
```

or put it in a file only you can read (the default is `$XDG_CONFIG_HOME/tumbleweed/api-key`, that
is `~/.config/tumbleweed/api-key`; `key_file` in the config names another one):

```sh
mkdir -p ~/.config/tumbleweed && chmod 700 ~/.config/tumbleweed
(umask 077; cat > ~/.config/tumbleweed/api-key)      # paste the key, then Ctrl+D
```

The key file must be a regular file you own with mode 0600: a looser mode, a symlink, a FIFO or
a device is refused, and the chat says why. The shell takes the key out of its own environment
when it starts, so the programs it starts later never inherit it, and it never reaches the logs,
the invocation log, the chat or the model's context (ARCHITECTURE I6). `ANTHROPIC_LOG=debug` is
ignored: the SDK's debug output would hold your prompts.

Settings live in `$XDG_CONFIG_HOME/tumbleweed/config.toml` (all optional, no secrets in it; the
file must not be writable by others). Environment variables override it:

```toml
[agent]
responder = "anthropic"                 # or "echo": the placeholder   (TUMBLEWEED_RESPONDER)
base_url = "https://api.anthropic.com"  # a LiteLLM-style proxy later  (TUMBLEWEED_BASE_URL)
key_file = "/home/me/keys/anthropic"    # absolute path                (TUMBLEWEED_KEY_FILE)
effort = "low"                          # thinking depth; unset = the model's default

[agent.models]
chat = "claude-sonnet-5-5"              # one setting per role         (TUMBLEWEED_MODEL)

[agent.limits]                          # per reply (ARCHITECTURE I9)
max_iterations = 8                      # model calls, each tool round trip is one more
max_turn_output_tokens = 16000          # up to 128000, thinking included: cost and time, not size
reply_timeout_seconds = 120             # wall-clock, retries and tools included
```

`base_url` must be `https` (plain `http` only to `localhost`, for a local proxy) with a valid port: the
key is sent there. The client follows no redirects and ignores `HTTP_PROXY`, `ALL_PROXY` and
`SSL_CERT_FILE`, so the key goes only to that URL. A rate limit, an overload or a dropped connection is
retried twice, a few seconds apart (a `Retry-After` of more than 10 s is reported at once instead). The
request says `Accept-Encoding: identity` and a compressed response is refused, so that the size of a
stream, bounded as it arrives (1 MiB plus 256 bytes per output token the reply may use; 64 KiB of an error
body), is the size of what is read.

A reply's size is bounded by bytes (64 KiB, the transcript's own cap), not by the token limit, which
counts thinking tokens and so is about cost and time. Errors (no key, no network, rate limit, overload, a timeout, a limit reached) appear under
the reply in the chat; the shell keeps running. Every model call is appended to
`$XDG_DATA_HOME/tumbleweed/shell/invocations.jsonl` (mode 0600, rotated at 10 MiB): its ID, session,
model, token usage, latency, stop reason, the provider's request ID and how it ended. It holds no
prompt, answer or secret. The system prompt is the versioned file
`src/tumbleweed/agent/prompts/system_v1.md`.

**A real call** (the `live` test, never run in CI) sends one tiny request and checks the answer, the
invocation log and that the key is not in it:

```sh
TUMBLEWEED_LIVE=1 ANTHROPIC_API_KEY="$(cat path/to/key)" uv run pytest -m live tests/agent -v
```

### Chat history

The chat is kept between runs. `tumbleweed-shell` restores the last session when it starts (its newest
messages, at most 998 and 4 MiB of text; the chat then has room for more) and saves each message as it
goes: yours when you send it, the reply when it ends. **New session** (the button above the chat) stops
the reply in flight, starts an empty session and keeps the old one in the file; it is also what to press
when the chat says it is full (1000 messages). The model is told about the restored conversation, the
newest exchanges that fit a budget of at most 120,000 estimated tokens (a text counts as its UTF-8
bytes, which overcounts ordinary English several times over, on purpose; ARCHITECTURE section 9 and
D16).

It lives in `$XDG_DATA_HOME/tumbleweed/shell/chat.sqlite3` (default `~/.local/share/...`), in directories of
mode 0700 and files of mode 0600, and holds only what the chat shows: no API key, no configuration (what you
type is stored as you typed it, so do not paste a secret into the chat). To forget everything, quit the shell
and delete `chat.sqlite3*` and `chat.locks/` there. A `tumbleweed/` or `shell/` directory that is a symlink is
refused (the shell then runs without history, and says so). It keeps the newest 20 sessions that have
messages, of at most 1000 messages each. Each write waits at most about 2 seconds for another shell's, then
fails and is reported (opening, restoring and starting a session are a few writes each), so no one write
holds the window for longer than that.

**New session and quitting.** An empty session you made with the **New session** button is kept: it is the
last session, so after you quit and start again the chat is empty (your old chat is still in the file, but is
not shown: there is no session browser yet, #80). Only the one newest such session is kept (starting another, in any shell, deletes the
empty ones nobody is using), and one that got messages is an ordinary session. The empty session a shell makes by itself when it starts is not kept: it is
deleted when that shell closes or starts another session, and one left by a shell that was killed is deleted
by the next shell that starts a session.

Two shells can run at once, and each owns its own session: a session a running shell holds is never taken by
another. A shell started while another runs gets the newest session that has messages (or that you started
empty) and that nobody holds, which is an older chat if there is one, or else an empty chat. An accidental
second shell therefore leaves nothing behind: its empty session is deleted when it closes, and the next start
restores the real chat. If a message cannot be saved (the disk is full, another shell held the file for too
long) the chat says so above the messages and the rest of that session is not saved; **New session** tries
the history again. If the file cannot be used (not a database, a symlink, another user's, written by a newer
version) the shell starts anyway, says so above the chat, and does not save; it leaves the file alone. If the
last session's data is damaged, a new session starts and the old data stays in the file. The mode of a
directory or file of yours that is too open is corrected, with a log line. Deferred pieces (a session
browser, and others) are in #80.

### Voice input

Hold **Ctrl+Space** (anywhere but the terminal) and speak; let go and the words are transcribed and sent as your message (or click
**Mic** in the status strip to start, and click again to stop). The voice indicator shows
*listening* while you talk and *transcribing* while the speech server works; **Esc** abandons the
utterance. Speech-to-text is whisper.cpp's `whisper-server`, which the shell reaches over HTTP, so
it can run natively on a Mac (with Metal, which a VM does not have) while the shell runs anywhere.
Replies are read aloud when Piper is set up ([Speech output](#speech-output)), and a wake word can
start a turn without any key ([Hands-free voice](#hands-free-voice)). On a Mac, Ctrl+Space may be
taken by the system's input-source shortcut (System Settings, Keyboard, Keyboard Shortcuts, Input
Sources); the Mic button works regardless (a configurable key is #100).

**1. The speech server.** Homebrew's `whisper-cpp` does not include `whisper-server` (its formula
turns it off), so build it from the source, at a release tag; on Apple Silicon Metal is on by default:

```sh
brew install cmake
git clone --depth 1 --branch v1.9.5 https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
cmake -S ~/whisper.cpp -B ~/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/whisper.cpp/build -j --config Release
scripts/whisper-model                                  # base.en, 142 MiB, checked against a pinned SHA-256
WHISPER_SERVER_BIN=~/whisper.cpp/build/bin/whisper-server scripts/whisper-server
```

`scripts/whisper-model [tiny.en|base.en|base]` downloads over https to
`~/.local/share/tumbleweed/whisper/` and installs the file only if its SHA-256 is the pinned one.
`scripts/whisper-server` runs the server on `127.0.0.1:8080` with that model (`WHISPER_MODEL`,
`WHISPER_HOST`, `WHISPER_PORT` change it). The startup log of whisper.cpp says which backend it uses
(`ggml_metal_init` on a Mac with Metal). `base` is multilingual: set `language` below to your language.

**2. The shell.** With the server on its default address nothing needs configuring. Otherwise, in
`config.toml` (all optional) or the environment:

```toml
[voice]
enabled = true                       # false: no Mic button, no key, and no speech output
stt_url = "http://127.0.0.1:8080"    # whisper-server's address         (TUMBLEWEED_STT_URL)
language = "auto"                    # or a code such as "en", "de"
edit_before_send = false             # true: the words wait in the input for you to edit and send
                                     # (always, over plain http to an address that is not local)

[voice.limits]                       # ARCHITECTURE I9
max_utterance_seconds = 30           # recording stops by itself here (up to 120)
request_timeout_seconds = 30         # the whole exchange with the server (up to 120)
max_response_bytes = 65536           # the most of the server's answer that is read
```

**From a VM**, run the server on the Mac bound to the host-only address the VM sees (for example
`WHISPER_HOST=192.168.64.1 scripts/whisper-server`) and give the VM `stt_url = "http://192.168.64.1:8080"`.
whisper-server has no authentication or encryption, and anyone who reaches the port can send it audio,
use the machine's CPU and GPU and change its model, so bind it to the loopback or to a private
interface and never to a network you share. Over plain `http` to an address that is not local, the
shell logs a warning and **never sends a transcript by itself**: it waits in the input for you to read
it (`edit_before_send` is forced on), because anyone on that network could have rewritten it on its
way back. `https` works if you put a TLS proxy in front.

`request_timeout_seconds` bounds the whole exchange with the server, however slow it is (the lookup of
its name, connecting, sending the audio and reading the answer each get what is left of it, so a
server that answers a byte at a time is cut off at the limit); Esc abandons a request at once. Only
the operating system's name lookup, once started, cannot be shortened (the request stops waiting for
it); an IP address needs none.

**The microphone** is read through GStreamer, 16 kHz mono, only while you talk, and the shell
never writes audio anywhere: it lives in memory from the microphone to the HTTP request. A recording is capped at
`max_utterance_seconds` however long the key is held. GStreamer is needed only for voice and is not
part of the base setup: on a Mac `brew install gstreamer` (it bundles `osxaudiosrc` and the
typelibs, and macOS asks for microphone permission the first time you talk); on Linux
`gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base` plus
`gstreamer1.0-pipewire` or `gstreamer1.0-pulseaudio`. Without it the shell starts as usual and the
Mic says what is missing. The server's answer is untrusted text: it is size-limited, read as strict
UTF-8 JSON, stripped of control and direction-changing characters, and shown as plain text. It then
enters the chat exactly as if you had typed it: it is *your* message, with no more authority than
typing, and nothing a transcript says can approve anything (prompts take the keyboard and pointer
only, I10). If your input already has a draft, the words are added to it and not sent.

**A real server** is exercised by the `live` test (never run in CI): it has macOS's `say` speak a
sentence, sends it to the server and checks the words that come back:

```sh
TUMBLEWEED_LIVE=1 TUMBLEWEED_STT_URL=http://127.0.0.1:8080 uv run pytest -m live tests/voice -v
```

### Speech output

When Piper is set up, the assistant's replies are **read aloud as they stream in**: the first sentence
starts playing while the rest of the reply is still being written. Code blocks and Markdown marks are
not spoken (links keep their text, addresses become the word "link", the words of an inline code span stay).
The voice indicator says *speaking*. **Barge-in is immediate**: hold Ctrl+Space (or click Mic) and speech
stops before the microphone opens, and **Esc** stops it, except while the terminal has the focus (a reply can be read for a long time, and
vim must keep its Esc; use Mute, or click out of the terminal). A first Esc stops the voice; the reply goes on
arriving as text, and a second Esc stops that too. The **Mute** button beside the Mic stops what is
being said and keeps the rest of that reply, and every later one, silent until you click it again;
sending a new message also stops the speech of the last reply.

Nothing is spoken until you point the shell at Piper and a voice. Install Piper
([rhasspy/piper](https://github.com/rhasspy/piper): a release binary, or `pip install piper-tts`, which
puts a `piper` program in its environment) and download a voice (an `.onnx` file with its `.onnx.json`
beside it, from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)), then, in `config.toml`:

```toml
[voice.tts]
enabled = true
piper_path = "/usr/local/bin/piper"        # absolute; a symlink is followed (Homebrew's are)
voice_model = "/home/me/.local/share/tumbleweed/piper/en_US-lessac-medium.onnx"
                                           # absolute, a regular file, no symlink and no ".." in the path
sample_rate_hz = 22050                     # the voice's: "audio" > "sample_rate" in its .onnx.json (medium: 22050, low: 16000)
max_sentence_chars = 400                   # one synthesis request (up to 1000)
max_reply_chars = 4000                     # the most of one reply that is spoken (up to 20000); the rest is dropped
synthesis_timeout_seconds = 20             # per sentence (up to 120)
max_audio_seconds = 60                     # the most audio one sentence may become (up to 300)
```

`[voice] enabled = false` turns speech off too (it is part of voice). A voice with the wrong `sample_rate_hz` plays too fast or too slow, nothing worse (reading it from the voice is #113). If a path or any setting of
`[voice.tts]` is wrong, the shell starts anyway with the Mic and the key working, says so next to the Mic
(once, in fixed words; the log has the details), and does not speak. The shell runs `piper --model
<voice> --output_raw` once per sentence, with the sentence on its standard input (never on the command
line), no shell, a fixed minimal environment (none of your variables, so the API key never reaches it), a
time limit and a size limit on its audio; Esc and Mute kill it at once. The audio is played through
GStreamer, and never written anywhere. Without GStreamer the shell starts as usual and the first spoken
sentence says what is missing; on a Mac `brew install gstreamer` has `osxaudiosink`, on Linux you need
`gstreamer1.0-plugins-base` and `gstreamer1.0-pipewire` or `gstreamer1.0-pulseaudio` (or `gstreamer1.0-plugins-good`
for `autoaudiosink`) next to the packages the microphone needs. A new process per sentence means Piper
loads the voice each time (a fraction of a second on a Mac): a long-lived Piper is #112.

The assistant must not hear itself: nothing is spoken while the microphone is open for you, and holding
the key stops speech first. Hands-free mode keeps the microphone open while a reply is spoken (how else
would the wake word interrupt it), and so asks for a much surer wake word then ([Hands-free
voice](#hands-free-voice)); headphones are still a good idea, and echo cancellation is not attempted.

A **real Piper** is exercised by the `live` test (never run in CI):

```sh
TUMBLEWEED_LIVE=1 TUMBLEWEED_PIPER=/usr/local/bin/piper TUMBLEWEED_PIPER_MODEL=/path/to/voice.onnx \
    uv run pytest -m live tests/voice/test_live.py -k piper -v
```

### Hands-free voice

**Hands-free** mode listens for a wake word and needs no key: say "hey jarvis" (or the word you chose),
then your request; speech detection (VAD) hears when you have finished and the words are sent exactly as
if you had held the key (the same speech server, the same checks, `edit_before_send` applies). The
**Hands-free** toggle beside Mute turns it on and off (it is off when the shell starts, and turning it
off really closes the microphone). The voice indicator says *waiting for the wake word* while it
listens, then *listening*, *transcribing*, *thinking* (a voice turn, hands-free or key, has been sent
and the assistant has not yet started speaking or finished) and *speaking*; and **Microphone on**
is shown next to it whenever audio may be being captured, in every one of those states and for a
recording held with the key too: it comes on when the microphone is asked for and goes off only
when it is confirmed closed (after Esc the state says idle at once, and the label stays until the
device is closed). Say the wake word while a
reply is being read and the speech stops before the shell listens, as with the key; **Esc** abandons a
turn being recorded (anywhere, the terminal included, as it does an utterance held with the key; while
only waiting for the wake word Esc is not taken) and so does **Mute**; holding Ctrl+Space takes the
microphone back.

**Models.** Silero VAD (about 2 MiB) and openWakeWord's melspectrogram, embedding and wake-word models
(about 3 MiB) are not part of the repository: `scripts/wake-models [hey_jarvis|alexa|hey_mycroft]`
downloads them over https (a ceiling on size and time, redirects https only) to
`~/.local/share/tumbleweed/wake/`, each checked against a pinned SHA-256 and installed only if it
matches; the shell checks the same hashes again, byte for byte, before it loads a file, refuses a symlink
anywhere on the path and a file that is not an ordinary file, and gives `onnxruntime` the checked bytes, never
a path. Licences: **Silero VAD is MIT**; **openWakeWord's code is Apache-2.0**, its melspectrogram and
embedding models derive from Google's Apache-2.0 speech-embedding model, and **its pre-trained wake words
(`hey_jarvis`, `alexa`, `hey_mycroft`) are CC BY-NC-SA 4.0: non-commercial use only**. Train your own
wake word with openWakeWord if that does not suit you (using a model of your own needs a new pinned entry,
`src/tumbleweed/voice/models.py`).

```toml
[voice.vad]                          # also trims push-to-talk recordings (#103)
models_dir = "/home/me/.local/share/tumbleweed/wake"   # absolute, no symlink; nothing happens until it is set
threshold = 0.5                      # how likely a 32 ms window must be speech (0.1 to 0.95)
end_silence_ms = 800                 # this much quiet after speech ends a turn (200 to 3000)
min_speech_ms = 128                  # less speech than this is a click, not speech
wait_for_speech_seconds = 6          # a turn in which nothing is said is given up (1 to 30)

[voice.wake]                         # hands-free mode itself: opt in by naming the directory
enabled = true
models_dir = "/home/me/.local/share/tumbleweed/wake"
model = "hey_jarvis"                 # "alexa" or "hey_mycroft"
threshold = 0.5                      # how sure the model must be (0.1 to 0.99)
speaking_threshold = 0.9             # ... while a reply is spoken, and for speaking_holdoff_ms after
speaking_holdoff_ms = 500
```

A mistake in either table turns hands-free off (and the trimming) with one fixed message next to the Mic
(the log has the details) and nothing else: push-to-talk, speech and the speech server go on. So do model
files that are missing or not as pinned. A recording is capped at `[voice.limits] max_utterance_seconds`,
as with the key. Hands-free needs GStreamer, like the Mic (see "The microphone" above).

**Audio and the assistant's own voice.** Audio is never written anywhere (a test runs a whole turn under
an audit hook), the buffers between the microphone and the models have fixed caps, and everything that
waits has a timeout. The wake word and VAD run on an inference thread, never the GTK main thread, with
`onnxruntime` on one CPU thread. The assistant's voice through the speakers is *not* cancelled
acoustically: while a reply is spoken, and for half a second after, the wake word needs
`speaking_threshold` instead of `threshold`, and nothing is transcribed except after a wake word. A loud
enough speaker, or a reply that says the wake word, can still get through that: use headphones, or
`speaking_threshold = 1` (no wake word is accepted while a reply is spoken). The wake phrase is also
removed from the reply text before it is spoken. Hands-free never moves the keyboard focus, so a
turn started by a sound cannot take what you are typing in the terminal. Anyone or anything that says the wake word near the microphone (a video, a
person) starts a turn, and what it says is sent as your message (so with `edit_before_send = true` it
waits for you); it still cannot approve anything: prompts take the keyboard and pointer only (I10).

**Performance.** `uv run python scripts/measure_hands_free_cpu.py` runs the real pipeline on the
real models for a minute, paced in real time on a quiet room's noise, and prints idle CPU as a share of
one core (the target is 15% on Apple Silicon; `--microphone` includes the real capture). In a quiet
room the wake-word models are not run at all (a loudness gate: after 2.4 s under an RMS of about -50
dBFS they stop, and catch up when sound returns, scoring exactly as they would have), which costs
about 0.5% of a core on an Apple M3 Max; with sound they run, about 10% there (`--noise-rms 400`); a
microphone with a noise floor above the gate never lets it close. The numbers are in the PR. Real models, heard on speech made by macOS's `say`, are exercised by the `live` tests (never
run in CI): `scripts/wake-models && TUMBLEWEED_LIVE=1 uv run pytest -m live tests/voice/test_live.py -k "silero or wake or whole_turn"`.

### Checks

```sh
uv run ruff format --check .     # formatting
uv run ruff check .              # lint
uv run mypy                      # strict type check
uv run lint-imports              # layering contracts
uv run pytest                    # tests runnable on this Mac; others skip with a reason
scripts/test-linux               # all of the above in the Linux container
```

CI runs the same commands (the macOS job on Python 3.12, 3.13 and 3.14; the container has
3.12). On 3.14 the `gtk` smoke test currently fails, because PyGObject's `gi.events` uses an
asyncio API that 3.14 deprecates and our tests treat warnings as errors (#47); the macOS job
deselects `gtk` tests, and `.python-version` pins 3.12.
`uv run pytest` includes a coverage report.

### Dependencies

`uv.lock` is resolved for macOS arm64, Linux x86_64 and Linux aarch64 only (`[tool.uv]
environments`), and with a cooldown: `exclude-newer = "7 days"` keeps uv from choosing a release
younger than a week, because a compromised release is usually noticed within days. The window is
relative and recorded in the lock (`exclude-newer-span`), so the lock does not go stale as time
passes. `tests/test_lock.py` checks that nothing in the lock is younger.

The runtime dependencies are `pydantic`, `PyGObject`, `regex` (a UAX 29 grapheme-cluster
segmenter for the transcript's display transform; DECISIONS D10, with `types-regex` for the type
checker), `anthropic` (the official Claude SDK, behind our provider interface; it brings `jiter` and a
few small pure-Python packages, all locked for the three platforms, and 1.13 is the newest release
inside the cooldown today) and `httpx2`, the SDK's HTTP library, which is a direct dependency because
`agent.anthropic_provider` imports it: for the transport that bounds a response, for refusing
redirects and environment proxies, and for its exception types, and by `voice.whisper`, the speech
server's client (one HTTP stack for the project, not two: DECISIONS D20, #43). `httpcore2`, httpx2's own
transport library, is declared too (it was already locked as httpx2's dependency): `voice.budget` and
`voice.whisper` use it to put one deadline on every operation of a speech request. `onnxruntime` and
`numpy` run hands-free mode's speech-detection and wake-word models (DECISIONS D23: not the
`openwakeword` package, whose Linux build needs `tflite-runtime`, which has no wheels for Python 3.12
and up, #42); both have wheels for Python 3.12 to 3.14 on every platform in the lock, and
`tests/test_lock_hands_free.py` checks it. A new one needs its justification in the PR and a lock for macOS and Linux.

- To update: `uv lock --upgrade`, which re-resolves with the window as of today.
- To take a newer release of one package before its week is up (a security fix), add
  `exclude-newer-package = { name = "2026-10-16T00:00:00Z" }` to `[tool.uv]` in `pyproject.toml`
  with a comment saying why and linking the issue, run `uv lock --upgrade-package name`, and
  remove the entry once the window has caught up.
- To change the cooldown, edit `exclude-newer` in `pyproject.toml` and run `uv lock`.

### Linux tests

Parts of Tumbleweed only exist on Linux: the sandbox (bubblewrap, Landlock, seccomp). Tests
that need those kernel features are marked `linux` and skip on macOS, with the reason printed
(`pytest -rs`). Tests of the btrfs snapshot backend are marked `btrfs` instead, because they
also need a btrfs mount that the CI container cannot provide. `scripts/test-linux` runs everything inside an Ubuntu 24.04
container (the `Dockerfile`) as a non-root user, with GTK on a headless Wayland compositor
(weston, `GSK_RENDERER=cairo`) and the security options that bubblewrap needs to create
user namespaces (see [ARCHITECTURE section 11](docs/ARCHITECTURE.md)). It needs Docker
running. It builds the image first, which is fast once Docker's layer cache is warm, and runs
exactly the image that build produced (by ID, not by tag), so that two worktrees testing at once
cannot run each other's code.

```sh
scripts/test-linux                          # format, lint, mypy, import contracts, pytest
scripts/test-linux uv run pytest -k bwrap   # any other command, in the same environment
TUMBLEWEED_CI=1 scripts/test-linux          # as CI: a skipped or xfailed gtk/linux test fails
```

### IPC

The shell and the broker talk over a typed, bounded, authenticated Unix-socket protocol
(`src/tumbleweed/ipc`, [ARCHITECTURE: IPC](docs/ARCHITECTURE.md)). Its messages are pydantic
models, and `schemas/ipc.v1.json` is generated from them; a test fails when the file is stale:

```sh
uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json
```

Sockets live in `$XDG_RUNTIME_DIR/tumbleweed/`. macOS has no `XDG_RUNTIME_DIR`, so there the
per-user temporary directory is used instead (`confstr(_CS_DARWIN_USER_TEMP_DIR)`), and the
server keeps its socket and lock file young there, because macOS purges files nobody has
accessed for three days. Some of the framing and codec tests are property tests (hypothesis, run
with a fixed seed so that CI and a laptop test the same examples). The tests that a different
user is refused, as a client and as a server, need a second uid: the Linux image has one
(`tumbleweed-peer`, with a sudo rule that lets the test user run commands as it and nothing
more), so those tests run in `scripts/test-linux` and are skipped on a Mac, where the same rule
is covered with a fake credentials provider.

### Sandbox

`tumbleweed.sandbox` ([ARCHITECTURE: Sandbox launcher](docs/ARCHITECTURE.md)) runs one argv list in
bubblewrap: no network, only its workspace and what it was granted, hard limits. It is a library
for the broker (below) and has no command of its own. Its policy and bubblewrap argument builder
are tested on every OS; running a sandbox needs Linux, so on a Mac use the container:

```sh
scripts/test-linux uv run pytest tests/sandbox -v
```

The results say which limits the host could not enforce. In the container that is the memory total,
`pids.max` and the CPU quota, because there is no systemd user manager (`systemd-run --user
--scope`); the rlimits still apply. Two switches make a missing tool fail a run instead of skipping
a test: `TUMBLEWEED_REQUIRE_DOCKER=1` (the Docker build-context check) and `TUMBLEWEED_REQUIRE_GIT=1`
(the lock-age check needs the git history; the macOS CI jobs set it).

Inside the namespaces every sandbox also runs under a seccomp filter (no `ptrace`, mounts,
keyring calls, unix sockets, nested namespaces, `io_uring`, ...) and, first, a launcher,
`tumbleweed-sandbox-exec`, that confines the filesystem to the policy's mounts with Landlock. The
target requires Landlock, so `SandboxPolicy.require_landlock` is true unless you say otherwise
and a kernel without it refuses to run anything. Docker Desktop's kernel has none (#44): there
the Landlock tests skip with the kernel's answer, and a script of yours that runs a sandbox
there must set `require_landlock=False`, after which each result says `hardening.landlock` is
`unavailable`. In CI (an Ubuntu kernel) the Landlock tests are required, and the log of the Linux
job lists each one as run (`-v -rP`); a local `TUMBLEWEED_CI=1 scripts/test-linux` on Docker
Desktop fails exactly those (`TestWithLandlock` in `tests/sandbox/test_landlock.py`) and, on
Apple silicon, the few tests of x86-only ABIs (skipped there with that reason), which is the guard
working.

### Test markers

| Marker | Needs | Notes |
|---|---|---|
| `gtk` | a display | runs natively on a Mac with a desktop session (tests that need a layout present a window, which shows briefly), and under weston in the container |
| `linux` | Linux kernel features | skips on macOS |
| `btrfs` | a btrfs mount | the `btrfs` gate skips (no loopback btrfs: not Linux, no `mkfs.btrfs`, not root) and that setup skip alone is exempt from the `TUMBLEWEED_CI` guard, also on a test that carries `linux` or `gtk` too; any other skip of it fails the run. CI has no root, so the gate always fires there: a `linux` + `btrfs` test never runs in CI, and `btrfs` is reserved for tests of the btrfs backend (S27's VM test covers it) |
| `live` | real external services | the `live` gate skips unless `TUMBLEWEED_LIVE=1`; that is an error together with `TUMBLEWEED_CI=1`, and under the guard a `live` test may not also be marked `gtk` or `linux`, so live tests never run in CI and are never guarded |
| `xfail_allowed_in_ci(reason=...)` | | lets a `gtk`/`linux` test xfail under `TUMBLEWEED_CI=1`, where an xfail fails the run like a skip does; the reason must reference an issue, such as `reason="tracked in #123"` |

The first lines of a pytest run say whether the `TUMBLEWEED_CI` guard is active.

### Broker policy core

`src/tumbleweed/broker` is the decision logic behind every grant (ARCHITECTURE section 7), as
pure code with no daemon, IPC or storage (the daemon, below, is built on it): capabilities, grant
scopes, the static deny rules with the ancestor rule, sticky taint, the write gate, and the
path handling (`ResolvedPath` when deciding, `StoredPath` and a component-by-component open
that follows no symlink when running). Its tests include property tests (hypothesis) that
generate escape attempts. A few tests need a case-insensitive volume, which a Mac has and the
Linux container does not (they skip there, with the reason), and the `openat2` ones need Linux
(marked `linux`, run by `scripts/test-linux`); between them both ways of opening are covered.

### Broker daemon

`tumbleweed-broker` (`src/tumbleweed/broker`, [ARCHITECTURE: Broker daemon](docs/ARCHITECTURE.md),
D17) serves tasks, grants, file access and sandbox runs over the IPC above. Nothing in the shell
starts it yet (S19 wires it in); to try it by hand, in one terminal:

```sh
uv run tumbleweed-broker --no-landlock --allow-unenforced-limits   # a development host
```

and from Python, a `BrokerClient` that starts the daemon if nobody is there:

```python
from tumbleweed.broker.client import BrokerClient, BrokerConnector, spawn_broker_process
from tumbleweed.ipc.sockdir import SocketPath


async def connect(socket_path: SocketPath) -> BrokerClient:
    connector = BrokerConnector(
        socket_path,
        spawn=lambda: spawn_broker_process(
            extra_args=("--no-landlock", "--allow-unenforced-limits")
        ),
    )
    return await connector.connect()
```

Until S18 there is nobody to ask, so every
`request_grant` is denied and nothing can be granted: the tests give the daemon a scripted user.
`--no-landlock` and `--allow-unenforced-limits` are for a development host only (Docker Desktop's
kernel has no Landlock; the container and CI have no systemd user manager). Without them the daemon
refuses runs on a host that cannot apply Landlock, the process limit and the total-memory limit,
which is what the target needs, and it writes the setting to its audit log
(`$XDG_DATA_HOME/tumbleweed/broker/audit.jsonl`, one JSON entry per decision, run and revocation).
The tests that run a real sandbox through the daemon are marked `linux` (`scripts/test-linux`);
the rest use a fake sandbox and run everywhere.

### Tool store

The broker keeps saved tools (`src/tumbleweed/tools`, `src/tumbleweed/broker/toolstore.py`,
[ARCHITECTURE section 5](docs/ARCHITECTURE.md), D19): versioned, immutable, hashed packages (manifest
with typed ports, UI spec with state defaults, code modules, the taint of the task they were saved
from) in `$XDG_DATA_HOME/tumbleweed/broker/`, an index with full-text search in `broker.sqlite3` and
the packages themselves in `tool-blobs/`, each file named by its SHA-256 and checked on every read.
Nothing saves a tool from a conversation until S24; what works today is the store, bundle import and
export, and the `tumbleweed-tools` command, which talks to the running broker (it opens no store of
its own, and does not start the broker):

```sh
uv run tumbleweed-broker --no-landlock --allow-unenforced-limits &   # a development host
uv run tumbleweed-tools list                      # newest version of each tool
uv run tumbleweed-tools list "csv viewer"         # search name, description and tags
uv run tumbleweed-tools show tool_01j... [--version 2] [--json]
uv run tumbleweed-tools export tool_01j... csv.tool.json   # a new file, mode 0600; `-` is stdout
uv run tumbleweed-tools import csv.tool.json      # `-` is stdin; no grant or state comes with it
```

A bundle is one canonical JSON file; an import refuses anything that is not exactly what an export
writes (unknown fields, a wrong hash, pretty-printed or escaped JSON, text that is not NFC with LF
line endings, too big) and never changes a tool the store already has. An imported tool is
tainted as if it had read files (`fs`) whatever the bundle says; `import` prints both what the
package claims and what is kept. `export TOOL -` refuses to print on a terminal; use a file or a
pipe. `uv run pytest tests/tools tests/broker/test_toolstore.py
tests/broker/test_daemon_tools.py` are the tests that matter most here.

### UI spec

The contract between the model and the renderer, `tumbleweed.ui/1`, is documented in
[`docs/ui-spec.md`](docs/ui-spec.md), with golden examples in `tests/ui/spec/golden/`. After a
change to its models, regenerate the committed JSON Schema and the generated parts of the
reference; the tests fail until you do:

```sh
uv run python -m tumbleweed.ui.spec.schema --write
uv run python -m tests.ui.spec.docgen --write
```

#### Previewing a spec

```sh
uv run tumbleweed-render tests/ui/spec/golden/tip_calculator.json
```

opens a window with the spec rendered on a canvas, by the renderer the shell uses and with nothing
behind it: computed state and `set` handlers work (the tip calculator needs no agent), `sandbox` and
`agent` handlers do nothing yet (S20, S12). A file that is not a valid spec is printed as the model
would be told, one problem per line, with exit status 1 and no window. The renderer is
`tumbleweed.ui.runtime` (state, expressions, handlers; no GTK, unit-tested without a display) and
`tumbleweed.ui.gtk` (the thin adapter; `pytest -m gtk` in the container). It creates no popover,
dialog, tooltip or drag icon from a spec, so that the canvas can never cover the trusted chrome
(a `dropdown` is a list in place; DECISIONS D14), and images, tables, text views, tabs and expanders
show as a placeholder until S11.
