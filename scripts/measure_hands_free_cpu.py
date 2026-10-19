"""Measure what idle hands-free listening costs: CPU time as a share of one core (S08).

    uv run python scripts/measure_hands_free_cpu.py                 # 60 s, the real models
    uv run python scripts/measure_hands_free_cpu.py --seconds 120 --noise-rms 200
    uv run python scripts/measure_hands_free_cpu.py --microphone    # the real GStreamer capture

It runs the production pipeline: `HandsFree`, its mailbox and inference thread, the real
`HandsFreeEngine` over Silero VAD and openWakeWord's models (hash-checked from the directory
`scripts/wake-models` fills), fed 80 ms chunks of audio paced in real time, in the state hands-free
spends nearly all of its life in: waiting for the wake word. The audio is low-level noise by
default (a quiet room: no speech, nothing to wake it; the wake models are then behind their loudness
gate and mostly not run, so use `--noise-rms 400` to measure a room with sound, where they run),
or the real microphone with `--microphone` (needs GStreamer; the first use may ask for microphone
permission). The result is the whole process's CPU time (user + system, every thread) over the
wall-clock time of the measured part, after the models have loaded and the pipeline has warmed up,
as a percentage of one core; the plan's target is at most 15% on Apple Silicon, native, and the exit
status is 1 above `--max`.

Without `--microphone` the capture itself (CoreAudio, GStreamer's resampling) is not in the number:
it is a callback that copies a few kilobytes into the mailbox. `--fake-models` replaces the models
by trivial detectors, to try the script itself without them (the tests do).
"""

import argparse
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.config import VadConfig, WakeConfig
from tumbleweed.voice.engine import HandsFreeEngine
from tumbleweed.voice.fake import FakeAudioSource, FakeAudioStream, FakeStt, FakeVad, FakeWake
from tumbleweed.voice.hands_free import HandsFree, HandsFreePhase
from tumbleweed.voice.input import VoiceInput
from tumbleweed.voice.limits import BYTES_PER_SECOND, VoiceLimits
from tumbleweed.voice.modelfile import check_models_directory
from tumbleweed.voice.models import EMBEDDING, MELSPECTROGRAM, SILERO_VAD, WAKE_WORDS
from tumbleweed.voice.store import ModelStore
from tumbleweed.voice.vad import SileroVad
from tumbleweed.voice.wake import OpenWakeWordDetector

if TYPE_CHECKING:
    from tumbleweed.voice.audio import AudioStream
    from tumbleweed.voice.wake import WakeDetector

CHUNK_SECONDS = 0.08


class Queue:
    """A `MainThreadDispatcher` the main thread pumps."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    def post(self, callback: Callable[[], None]) -> None:
        self._queue.put(callback)

    def pump(self) -> None:
        while True:
            try:
                self._queue.get_nowait()()
            except queue.Empty:
                return


class Counting:
    """A `WakeDetector` that counts the chunks it is given: proof that audio reached the models."""

    scored = 0

    def __init__(self, inner: "WakeDetector") -> None:
        self._inner = inner

    def score(self, chunk: bytes) -> float:
        Counting.scored += 1
        return self._inner.score(chunk)

    def reset(self) -> None:
        self._inner.reset()


def noise(rms: float, seconds: float, seed: int) -> bytes:
    import numpy as np

    rng = np.random.default_rng(seed)
    return rng.normal(0, rms, int(seconds * 16_000)).astype("<i2").tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=60.0, help="measured time (default 60)")
    parser.add_argument("--warmup", type=float, default=5.0, help="seconds not measured")
    parser.add_argument("--noise-rms", type=float, default=50.0, help="int16 rms of the noise")
    parser.add_argument(
        "--start-timeout", type=float, default=30.0, help="seconds to reach WAITING"
    )
    parser.add_argument("--die-after", type=float, help=argparse.SUPPRESS)  # for the tests
    parser.add_argument("--max", type=float, default=15.0, help="percent of a core that passes")
    parser.add_argument(
        "--models", help="the models directory (default: where wake-models puts it)"
    )
    parser.add_argument("--microphone", action="store_true", help="capture the real microphone")
    parser.add_argument("--fake-models", action="store_true", help="trivial detectors, no models")
    args = parser.parse_args()

    limits = VoiceLimits()
    dispatcher = Queue()
    voice = VoiceInput(
        audio=lambda: FakeAudioSource(limits.max_utterance_bytes),  # push-to-talk is not used
        stt=FakeStt([]),
        dispatcher=dispatcher,
        limits=limits,
    )

    def engine() -> HandsFreeEngine:
        if args.fake_models:
            wake, vad = FakeWake(), FakeVad()
        else:
            data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            directory = args.models or os.environ.get("WAKE_MODEL_DIR") or f"{data}/tumbleweed/wake"
            store = ModelStore(check_models_directory(directory))
            wake = OpenWakeWordDetector(
                store.get(MELSPECTROGRAM), store.get(EMBEDDING), store.get(WAKE_WORDS["hey_jarvis"])
            )
            vad = SileroVad(store.get(SILERO_VAD))
        return HandsFreeEngine(
            wake=Counting(wake),
            vad=vad,
            wake_config=WakeConfig(),
            vad_config=VadConfig(),
            max_utterance_bytes=ByteCount(limits.max_utterance_bytes),
        )

    stream: AudioStream
    fake = FakeAudioStream()
    if args.microphone:
        from tumbleweed.voice.gstreamer import GStreamerAudioStream

        stream = GStreamerAudioStream()
    else:
        stream = fake
    hands_free = HandsFree(
        stream=lambda: stream,
        engine=engine,
        voice=voice,
        speech=None,
        dispatcher=dispatcher,
        limits=limits,
    )
    problems: list[str] = []

    class Watch:
        def phase_changed(self, phase: HandsFreePhase) -> None:
            return

        def microphone_changed(self, is_open: bool) -> None:
            return

        def problem(self, message: str) -> None:
            problems.append(message)

    hands_free.subscribe(Watch())
    stop = threading.Event()

    def feed() -> None:
        # The audio a microphone would deliver: 80 ms every 80 ms, from a pre-made second of noise.
        second = noise(args.noise_rms, 1.0, seed=1)
        step = int(CHUNK_SECONDS * BYTES_PER_SECOND)
        offset, due = 0, time.monotonic()
        began = due
        while not stop.is_set() and (args.die_after is None or due - began < args.die_after):
            fake.feed(second[offset : offset + step])
            offset = (offset + step) % (len(second) - step)
            due += CHUNK_SECONDS
            time.sleep(max(0.0, due - time.monotonic()))

    hands_free.set_enabled(True)
    feeder = None if args.microphone else threading.Thread(target=feed, daemon=True)
    if feeder is not None:
        feeder.start()
    # Warm up until it is waiting for the wake word with audio reaching the models (and for at least
    # `--warmup` seconds); a pipeline that never gets there, within `--start-timeout`, is dead.
    started = time.monotonic()
    while not problems and time.monotonic() - started < args.start_timeout:
        dispatcher.pump()
        if (
            hands_free.phase is HandsFreePhase.WAITING
            and Counting.scored >= 5
            and time.monotonic() - started >= args.warmup
        ):
            break
        time.sleep(0.01)
    if problems:
        print(f"hands-free did not start: {problems[0]}", file=sys.stderr)
        return 2
    if hands_free.phase is not HandsFreePhase.WAITING or Counting.scored < 5:
        print("hands-free did not start: it never reached the wake word", file=sys.stderr)
        return 2
    scored_before = Counting.scored

    cpu0, wall0, times0 = time.process_time(), time.monotonic(), os.times()
    deadline = wall0 + args.seconds
    while time.monotonic() < deadline:
        dispatcher.pump()
        time.sleep(0.05)
    cpu, wall, times = time.process_time() - cpu0, time.monotonic() - wall0, os.times()
    dispatcher.pump()
    alive = (
        not problems
        and hands_free.phase is HandsFreePhase.WAITING
        and Counting.scored - scored_before >= int(args.seconds / CHUNK_SECONDS / 2)
    )
    stop.set()
    hands_free.close()
    voice.close()

    if not alive:
        # A dead pipeline costs almost nothing, and would pass: say so, not a number.
        reason = problems[0] if problems else "it stopped waiting for the wake word or fell behind"
        print(f"hands-free died while measuring: {reason}", file=sys.stderr)
        return 2
    share = 100.0 * cpu / wall
    user, system = times.user - times0.user, times.system - times0.system
    print(
        f"idle hands-free: {share:.2f}% of one core "
        f"(cpu {cpu:.2f} s in {wall:.1f} s; user {user:.2f} s, system {system:.2f} s; "
        f"{'microphone' if args.microphone else f'noise rms {args.noise_rms:g}'}; "
        f"{'fake detectors' if args.fake_models else 'real models'})"
    )
    return 0 if share <= args.max else 1


if __name__ == "__main__":
    sys.exit(main())
