"""The recorder keeps both audio tracks in memory and turns them into WAV files."""

from mockingbird.recording import CallRecorder, TrackRecorder, upload_recording, wav_bytes

FRAME = b"\x00" * 640


def test_wav_bytes_header():
    out = wav_bytes(b"\x01\x00" * 160)
    assert out[:4] == b"RIFF" and out[8:16] == b"WAVEfmt "
    assert int.from_bytes(out[24:28], "little") == 16000  # sample rate
    assert int.from_bytes(out[22:24], "little") == 1  # mono
    assert int.from_bytes(out[34:36], "little") == 16  # bits
    assert int.from_bytes(out[40:44], "little") == 320  # data bytes
    assert int.from_bytes(out[4:8], "little") == 36 + 320  # RIFF size
    assert len(out) == 44 + 320


def test_track_recorder_caps_frames_and_counts_drops():
    t = TrackRecorder(max_frames=2)
    for i in range(5):
        t.append(FRAME, at_ms=i * 20)
    assert t.frames == 2 and t.dropped == 3 and len(t.pcm()) == 1280
    assert t.first_frame_ms == 0


def test_track_recorder_caps_bytes_so_oversized_frames_cannot_grow_memory():
    t = TrackRecorder(max_frames=2)  # 1280 bytes
    t.append(b"\x01" * 1000, at_ms=0)
    t.append(b"\x02" * 1000, at_ms=20)  # would reach 2000 bytes: dropped
    t.append(b"\x03" * 280, at_ms=40)  # exactly fills the cap
    assert t.frames == 2 and t.dropped == 1 and len(t.pcm()) == 1280
    assert t.pcm() == b"\x01" * 1000 + b"\x03" * 280


def test_track_recorder_first_frame_ms_is_none_until_a_frame_is_kept():
    t = TrackRecorder(max_frames=1)
    assert t.first_frame_ms is None and t.pcm() == b""
    t.append(FRAME, at_ms=5)
    t.append(FRAME, at_ms=25)
    assert t.first_frame_ms == 5


def test_call_recorder_routes_directions_and_stamps_first_frame():
    now = [10.0]
    r = CallRecorder(clock=lambda: now[0], cap_s=60)
    r.start()
    now[0] = 10.25
    r.on_audio("interviewer", b"\x01" * 640)
    now[0] = 10.5
    r.on_audio("candidate", b"\x02" * 640)
    now[0] = 10.52
    r.on_audio("candidate", b"\x03" * 640)
    assert r.interviewer.first_frame_ms == 250
    assert r.candidate.first_frame_ms == 500
    assert r.interviewer.pcm() == b"\x01" * 640
    assert r.candidate.pcm() == b"\x02" * 640 + b"\x03" * 640
    assert r.metadata() == {
        "candidate": {"frames": 2, "dropped": 0, "first_frame_ms": 500},
        "interviewer": {"frames": 1, "dropped": 0, "first_frame_ms": 250},
    }


def test_call_recorder_ignores_audio_before_start():
    r = CallRecorder(clock=lambda: 1.0, cap_s=60)
    r.on_audio("candidate", FRAME)
    assert r.candidate.frames == 0 and r.candidate.dropped == 0
    assert r.metadata()["candidate"]["first_frame_ms"] is None


def test_full_mode_cap_fits_twenty_minutes():
    r = CallRecorder(clock=lambda: 0.0, cap_s=1200)
    assert r.candidate.max_frames == (1200 + 60) * 50
    assert r.interviewer.max_frames == (1200 + 60) * 50


class FakeStores:
    """The interview store and the audio store an upload writes to. `put_error` fails every
    upload."""

    def __init__(self, put_error: Exception | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.recording: dict | None = None
        self.put_error = put_error

    async def put_wav(self, path: str, data: bytes) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.files[path] = data

    async def update_interview(self, interview_id: str, *, recording: dict) -> None:
        self.recording = recording


def recorded_call() -> CallRecorder:
    r = CallRecorder(clock=lambda: 0.0, cap_s=60)
    r.start()
    r.on_audio("candidate", b"\x01" * 640)
    r.on_audio("interviewer", b"\x02" * 640)
    return r


async def test_an_upload_lets_go_of_the_audio_and_keeps_the_counts():
    # Whatever still holds the recorder after the call, its audio is not held with it.
    r, stores = recorded_call(), FakeStores()
    await upload_recording(stores, stores, "i1", r)

    assert stores.files["interviews/i1/candidate.wav"] == wav_bytes(b"\x01" * 640)
    assert stores.files["interviews/i1/interviewer.wav"] == wav_bytes(b"\x02" * 640)
    assert r.candidate.pcm() == r.interviewer.pcm() == b""
    assert stores.recording["status"] == "uploaded"
    assert stores.recording["candidate"]["frames"] == 1


async def test_a_failed_upload_lets_go_of_both_tracks_too():
    r, stores = recorded_call(), FakeStores(put_error=RuntimeError("storage is down"))
    await upload_recording(stores, stores, "i1", r)

    assert stores.recording == {"status": "upload_failed"}
    assert r.candidate.pcm() == r.interviewer.pcm() == b""
