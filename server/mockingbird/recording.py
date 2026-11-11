"""In-memory recording of both sides of a call, and the WAV files they become.

Audio on both sockets is raw pcm_s16le, mono, 16 kHz, in 20 ms frames of 640 bytes. Each track
keeps what it is given, up to a cap, so a call that runs long or a browser that sends oversized
frames cannot grow memory without bound.
"""

import struct
from collections.abc import Callable
from typing import Literal

SAMPLE_RATE = 16000
FRAME_BYTES = 640  # one 20 ms frame of 16 kHz s16le mono
FRAMES_PER_SECOND = 50
# The cap leaves room past the call's own time cap for the hang-up to finish.
CAP_MARGIN_S = 60


def wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """The PCM (s16le, mono) wrapped in a canonical 44-byte RIFF/WAVE header."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,  # size of the fmt chunk
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,  # bytes per second
        2,  # bytes per sample frame
        16,  # bits per sample
        b"data",
        len(pcm),
    )
    return header + pcm


class TrackRecorder:
    """One direction's audio. Keeps at most `max_frames` frames and `max_frames * 640` bytes.

    A frame that would go past either limit is dropped and counted in `dropped`.
    """

    def __init__(self, max_frames: int) -> None:
        self.max_frames = max_frames
        self.frames = 0
        self.dropped = 0
        self.first_frame_ms: float | None = None
        self._max_bytes = max_frames * FRAME_BYTES
        self._buf = bytearray()

    def append(self, frame: bytes, at_ms: float) -> None:
        if self.frames >= self.max_frames or len(self._buf) + len(frame) > self._max_bytes:
            self.dropped += 1
            return
        if self.first_frame_ms is None:
            self.first_frame_ms = at_ms
        self._buf += frame
        self.frames += 1

    def pcm(self) -> bytes:
        return bytes(self._buf)


class CallRecorder:
    """Both tracks of one call, with times measured from `start()` on `clock` (in seconds)."""

    def __init__(self, clock: Callable[[], float], cap_s: int) -> None:
        self._clock = clock
        self._t0: float | None = None
        max_frames = (cap_s + CAP_MARGIN_S) * FRAMES_PER_SECOND
        self.candidate = TrackRecorder(max_frames)
        self.interviewer = TrackRecorder(max_frames)

    def start(self) -> None:
        self._t0 = self._clock()

    def on_audio(self, direction: Literal["candidate", "interviewer"], frame: bytes) -> None:
        if self._t0 is None:
            return  # the call is not live yet
        track = self.candidate if direction == "candidate" else self.interviewer
        track.append(frame, (self._clock() - self._t0) * 1000)

    def metadata(self) -> dict:
        return {
            name: {
                "frames": track.frames,
                "dropped": track.dropped,
                "first_frame_ms": track.first_frame_ms,
            }
            for name, track in (("candidate", self.candidate), ("interviewer", self.interviewer))
        }
