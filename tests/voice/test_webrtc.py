"""
The MIT License (MIT)

Copyright (c) 2015-2021 Rapptz
Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import threading
import wave
from fractions import Fraction

import av
import pytest
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

import lolka.voice.client as voice_client_module
from lolka.player import AudioSource, PCMAudio, PCMVolumeTransformer
from lolka.sinks import WaveSink
from lolka.sinks.errors import RecordingException
from lolka.utils import get_missing_voice_dependencies
from lolka.voice import _voice_impl

FRAME_BYTES = _voice_impl.FRAME_BYTES


def test_imports_without_nacl_and_davey():
    code = (
        "import sys\n"
        "sys.modules['nacl'] = None\n"
        "sys.modules['davey'] = None\n"
        "import lolka, lolka.voice, lolka.sinks\n"
        "assert 'nacl' not in [k for k, v in sys.modules.items() if v is not None]\n"
        "assert 'aiortc' not in sys.modules\n"
        "print(lolka.__version__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_no_missing_voice_dependencies():
    assert get_missing_voice_dependencies() == ()


async def _pump_until(track, predicate, timeout=3.0):
    """Reads frames from the track (like aiortc's sender) until ``predicate()``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await track.recv()


async def _audio_data(track, tries=200):
    for _ in range(tries):
        data = bytes((await track.recv()).planes[0])
        if any(data):
            return data
    raise AssertionError("no audio frame arrived")


async def test_source_track_frames_pause_and_after():
    track = _voice_impl.SourceAudioTrack()
    results = []
    track.set_source(
        PCMAudio(io.BytesIO(b"\x01\x00" * (FRAME_BYTES // 2) * 2)), results.append
    )

    frame = await track.recv()
    assert isinstance(frame, av.AudioFrame)
    assert frame.samples == 960
    assert frame.format.name == "s16"
    assert frame.layout.name == "stereo"
    assert frame.sample_rate == 48000
    assert track.is_playing

    first = await _audio_data(track)
    assert first[:2] == b"\x01\x00"
    track.pause()
    assert track.is_paused
    silent = await track.recv()
    assert not any(bytes(silent.planes[0])[:FRAME_BYTES])
    played = track.played_frames

    track.resume()
    await _pump_until(track, lambda: results == [None])
    assert track.source is None
    assert track.played_frames >= played


async def test_stop_source_calls_after_once():
    track = _voice_impl.SourceAudioTrack()
    results = []
    track.set_source(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 5)), results.append)
    track.stop_source()
    track.stop_source()
    await asyncio.sleep(0.2)
    assert results == [None]


class _FakeConn:
    def __init__(self):
        self.out_track = _voice_impl.SourceAudioTrack()
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, loop):
        self._connection = type("S", (), {"loop": loop, "user": None})()
        self.loop = loop


def _make_vc():
    loop = asyncio.get_running_loop()
    vc = voice_client_module.VoiceClient(_FakeClient(loop), channel=object())
    vc._conn = _FakeConn()
    vc._connected = True
    vc._state_ready.set()
    return vc


async def test_play_wait_finish_resolves_future():
    vc = _make_vc()
    seen = []
    future = vc.play(
        PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)), after=seen.append, wait_finish=True
    )
    assert isinstance(future, asyncio.Future)
    assert vc.is_playing()
    await _pump_until(vc._conn.out_track, future.done)
    assert future.result() is None
    assert seen == [None]
    assert not vc.is_playing()


async def test_stop_resolves_wait_finish_future_and_calls_after():
    vc = _make_vc()
    seen = []
    future = vc.play(
        PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 10)),
        after=seen.append,
        wait_finish=True,
    )
    vc.stop()
    assert future.done() and future.result() is None
    await asyncio.sleep(0.2)
    assert seen == [None]


async def test_play_returns_none_without_wait_finish():
    vc = _make_vc()
    assert vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES))) is None
    with pytest.raises(voice_client_module.ClientException):
        vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)))


class _Tracked(AudioSource):
    def __init__(self, frames=1000):
        self.frames = frames
        self.cleaned = threading.Event()

    def read(self):
        if self.cleaned.is_set() or self.frames <= 0:
            return b""
        self.frames -= 1
        return b"\x01\x00" * (FRAME_BYTES // 2)

    def cleanup(self):
        self.cleaned.set()


async def test_source_setter_wraps_without_cleaning_up_the_old_source():
    vc = _make_vc()
    orig = _Tracked()
    vc.play(orig)
    track = vc._conn.out_track
    await _audio_data(track)
    vc.source = PCMVolumeTransformer(orig)
    assert isinstance(vc.source, PCMVolumeTransformer)
    await asyncio.sleep(0.2)
    assert not orig.cleaned.is_set()
    assert vc.is_playing()
    assert (await _audio_data(track))[:2] == b"\x01\x00"
    vc.stop()
    await asyncio.sleep(0.2)
    assert orig.cleaned.is_set()


async def test_stop_while_paused_cleans_up_the_source():
    vc = _make_vc()
    first = _Tracked()
    vc.play(first)
    vc.pause()
    vc.stop()
    assert not vc.is_playing()
    await asyncio.sleep(0.2)
    assert first.cleaned.is_set()


async def test_after_runs_outside_the_event_loop_thread():
    vc = _make_vc()
    loop_thread = threading.get_ident()
    seen = {}
    done = threading.Event()

    def after(exc):
        seen["thread"] = threading.get_ident()
        done.set()

    vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)), after=after)
    await _pump_until(vc._conn.out_track, done.is_set)
    assert seen["thread"] != loop_thread


async def test_after_may_start_the_next_source_from_its_thread():
    vc = _make_vc()
    second_done = threading.Event()
    second = PCMAudio(io.BytesIO(b"\x01\x00" * (FRAME_BYTES // 2) * 2))

    def after_first(exc):
        vc.play(second, after=lambda e: second_done.set())

    vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)), after=after_first)
    await _pump_until(vc._conn.out_track, second_done.is_set)


async def test_after_blocking_on_the_loop_does_not_deadlock():
    # the FAQ recipe: run a coroutine on the bot loop and wait for it in ``after``
    vc = _make_vc()
    done = threading.Event()
    ran = []

    async def coro():
        ran.append(1)

    def after(exc):
        asyncio.run_coroutine_threadsafe(coro(), vc.loop).result(timeout=5)
        done.set()

    vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)), after=after)
    await _pump_until(vc._conn.out_track, done.is_set)
    assert ran == [1]


async def test_stop_from_after_future_captured_before_stop():
    vc = _make_vc()
    second_future = []
    done = threading.Event()

    def after(exc):
        second_future.append(
            vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 50)), wait_finish=True)
        )
        done.set()

    first = vc.play(
        PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 50)), after=after, wait_finish=True
    )
    vc.stop()
    await asyncio.wait_for(first, 2)
    await _pump_until(vc._conn.out_track, done.is_set)
    assert not second_future[0].done()
    vc.stop()


class _FakeRemoteTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, frames):
        super().__init__()
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            raise MediaStreamError
        await asyncio.sleep(0)
        return self._frames.pop(0)


def _frame(value: int, samples: int = 960):
    frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    frame.sample_rate = 48000
    frame.pts = 0
    frame.time_base = Fraction(1, 48000)
    frame.planes[0].update(bytes([value]) * frame.planes[0].buffer_size)
    return frame


class _QueueTrack(MediaStreamTrack):
    """Remote track fed by the test; ``None`` ends it."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue()

    async def recv(self):
        item = await self.queue.get()
        if item is None:
            raise MediaStreamError
        return item


async def _wav_frames(sink, user):
    with wave.open(sink.audio_data[user].file, "rb") as wav:
        assert wav.getframerate() == 48000
        assert wav.getnchannels() == 2
        return wav.getnframes(), wav.readframes(wav.getnframes())


async def test_recording_has_no_audio_from_before_start():
    vc = _make_vc()
    hook_calls = []
    vc.on_receive_track = lambda track, uid, pid: hook_calls.append((track, uid, pid))
    remote = _QueueTrack()
    vc._handle_track(remote, "42", "p")
    assert len(hook_calls) == 1
    for _ in range(10):  # spoken before the recording starts
        remote.queue.put_nowait(_frame(1))
    await asyncio.sleep(0.1)

    done = []
    sink = WaveSink()

    async def callback(s, *extra):
        done.append((s, extra))

    vc.start_recording(sink, callback, "extra")
    assert vc.is_recording()
    for _ in range(3):
        remote.queue.put_nowait(_frame(2))
        await asyncio.sleep(0.02)
    remote.queue.put_nowait(None)
    await asyncio.sleep(0.1)
    vc.stop_recording()
    assert not vc.is_recording()
    with pytest.raises(RecordingException):
        vc.stop_recording()
    await asyncio.sleep(0.2)

    assert done == [(sink, ("extra",))]
    assert sink.audio_data[42].finished
    count, raw = await _wav_frames(sink, 42)
    assert count == 3 * 960
    assert set(raw) == {2}


async def test_unread_remote_track_is_drained():
    vc = _make_vc()
    remote = _QueueTrack()
    vc._handle_track(remote, 5, "p")
    for _ in range(20):
        remote.queue.put_nowait(_frame(1))
    await asyncio.sleep(0.2)
    assert remote.queue.empty()


async def test_hook_track_is_readable_while_recording():
    vc = _make_vc()
    got = []
    vc.on_receive_track = lambda track, uid, pid: got.append(track)
    remote = _QueueTrack()
    vc._handle_track(remote, 7, "p")
    sink = WaveSink()
    vc.start_recording(sink)
    frames = []

    async def read_hook():
        while True:
            try:
                frames.append(await got[0].recv())
            except MediaStreamError:
                return

    reader = asyncio.ensure_future(read_hook())
    for i in range(5):
        remote.queue.put_nowait(_frame(3))
        await asyncio.sleep(0.02)
    remote.queue.put_nowait(None)
    await asyncio.wait_for(reader, 2)
    assert len(frames) >= 3
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert len((await _wav_frames(sink, 7))[1]) > 0


async def test_recording_callback_error_contract():
    # py-cord 2.7+/2.8: callback(error) when no extra args are given
    vc = _make_vc()
    got = []
    vc.start_recording(WaveSink(), got.append)
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert got == [None]


async def test_recording_callback_error_contract_gets_sink_cleanup_error():
    class BadSink(WaveSink):
        def cleanup(self):
            raise ValueError("boom")

    vc = _make_vc()
    got = []
    vc.start_recording(BadSink(), got.append)
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert len(got) == 1 and isinstance(got[0], ValueError)


async def test_recording_callback_legacy_contract_with_coroutine_function():
    vc = _make_vc()
    got = []

    async def callback(sink, *args):
        got.append((sink, args))

    sink = WaveSink()
    vc.start_recording(sink, callback)
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert got == [(sink, ())]


async def test_sink_cleanup_runs_off_the_event_loop_thread():
    seen = []

    class ThreadSink(WaveSink):
        def cleanup(self):
            seen.append(threading.get_ident())
            super().cleanup()

    vc = _make_vc()
    vc.start_recording(ThreadSink())
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert seen and seen[0] != threading.get_ident()


async def test_stop_recording_from_another_thread():
    vc = _make_vc()
    got = []
    vc.start_recording(WaveSink(), got.append)
    await asyncio.get_running_loop().run_in_executor(None, vc.stop_recording)
    await asyncio.sleep(0.2)
    assert got == [None]


async def test_voice_server_update_waits_for_endpoint():
    from types import SimpleNamespace

    vc = _make_vc()
    vc._connected = False
    await vc.on_voice_server_update(
        SimpleNamespace(token="t", endpoint=None, guild_id=1)
    )
    assert not vc._server_ready.is_set()
    await vc.on_voice_server_update(
        SimpleNamespace(token="t", endpoint="voice.example:443", guild_id=1)
    )
    assert vc._server_ready.is_set()
    assert vc.endpoint == "voice.example:443"
    assert vc.token == "t"


async def test_voice_state_update_without_channel_tears_down():
    from types import SimpleNamespace

    vc = _make_vc()
    conn = vc._conn
    vc.cleanup = lambda: None
    await vc.on_voice_state_update(
        SimpleNamespace(session_id="s", channel_id=None, channel=None)
    )
    await asyncio.sleep(0.05)
    assert vc.session_id == "s"
    assert not vc.is_connected()
    assert conn.closed


def _opus_available():
    try:
        from lolka import opus

        opus.Decoder()
    except Exception:
        return False
    return True


needs_opus = pytest.mark.skipif(not _opus_available(), reason="libopus is not available")


class _OpusSource(AudioSource):
    def __init__(self, packets):
        self.packets = list(packets)
        self.cleaned = False

    def read(self):
        return self.packets.pop(0) if self.packets else b""

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleaned = True


@needs_opus
async def test_opus_source_is_decoded_in_the_reader_and_60ms_packets_are_kept():
    import struct

    from lolka import opus

    packet = opus.Encoder().encode(struct.pack("<h", 4000) * (2880 * 2), 2880)
    assert len(opus.Decoder().decode(packet)) == FRAME_BYTES * 3
    track = _voice_impl.SourceAudioTrack()
    done = []
    source = _OpusSource([packet] * 10)
    track.set_source(source, done.append)
    await _pump_until(track, lambda: done)
    assert track.played_frames == 30
    assert source.cleaned


async def test_opus_decoder_failure_is_raised_from_play(monkeypatch):
    from lolka import opus

    def boom():
        raise opus.OpusNotLoaded()

    monkeypatch.setattr(_voice_impl._opus, "Decoder", boom)
    vc = _make_vc()
    with pytest.raises(opus.OpusNotLoaded):
        vc.play(_OpusSource([b"x"]), wait_finish=True)
    assert vc._player_future is None
    assert not vc.is_playing()


async def test_read_exception_calls_after_with_it_and_cleans_up():
    class Broken(AudioSource):
        cleaned = False

        def read(self):
            raise ValueError("boom")

        def cleanup(self):
            Broken.cleaned = True

    vc = _make_vc()
    errors = []
    vc.play(Broken(), after=errors.append)
    await _pump_until(vc._conn.out_track, lambda: errors)
    assert isinstance(errors[0], ValueError)
    assert Broken.cleaned
    assert not vc.is_playing()


async def test_replace_source_keeps_the_read_ahead_frames():
    class Counter(AudioSource):
        def __init__(self):
            self.n = 0

        def read(self):
            self.n += 1
            return bytes([self.n]) * FRAME_BYTES

    class Wrapper(AudioSource):
        def __init__(self, inner):
            self.inner = inner

        def read(self):
            return self.inner.read()

    track = _voice_impl.SourceAudioTrack()
    inner = Counter()
    track.set_source(inner)
    await asyncio.sleep(0.2)  # the reader fills its read-ahead queue
    seen = []
    for _ in range(3):
        seen.append(bytes(( await track.recv()).planes[0])[0])
    track.replace_source(Wrapper(track.source))
    for _ in range(40):
        data = bytes((await track.recv()).planes[0])
        if any(data):
            seen.append(data[0])
        await asyncio.sleep(0.005)
    assert seen[0] == 1
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert all(g == 1 for g in gaps[:-1]) or sum(g - 1 for g in gaps) <= 1
    track.stop_source()


async def test_stop_while_waiting_for_the_previous_reader_cleans_up_after_it_exits():
    release = threading.Event()
    events = []

    class Blocking(AudioSource):
        def read(self):
            release.wait(5)
            return b"\x00" * FRAME_BYTES

    class Inner(AudioSource):
        def read(self):
            return b""

        def cleanup(self):
            events.append("cleanup")

    track = _voice_impl.SourceAudioTrack()
    track.set_source(Blocking())
    await asyncio.sleep(0.1)
    track.replace_source(Inner())
    track.stop_source()
    await asyncio.sleep(0.3)
    assert events == []  # the old reader is still inside read()
    release.set()
    await asyncio.sleep(0.5)
    assert events[:1] == ["cleanup"]  # (AudioSource.__del__ may add another)


async def test_start_recording_right_after_stop_recording():
    vc = _make_vc()
    got = []
    first, second = WaveSink(), WaveSink()
    vc.start_recording(first, got.append)
    first.write(b"\0" * 3840, 1)
    vc.stop_recording()
    vc.start_recording(second, lambda err: got.append("second"))
    assert vc.is_recording()
    await asyncio.sleep(0.2)
    assert got == [None]
    assert first.audio_data[1].file.read(4) == b"RIFF"
    vc.stop_recording()
    await asyncio.sleep(0.2)
    assert got == [None, "second"]


async def test_replaced_reader_chain_never_reads_in_parallel_with_a_hung_read():
    release = threading.Event()

    class Inner(AudioSource):
        def __init__(self):
            self.i = 0
            self.readers = 0
            self.overlap = False
            self.lock = threading.Lock()

        def read(self):
            with self.lock:
                self.readers += 1
                self.overlap |= self.readers > 1
            try:
                if self.i == 1:
                    release.wait(4)
                self.i += 1
                return bytes([self.i]) * FRAME_BYTES
            finally:
                with self.lock:
                    self.readers -= 1

    class Wrapper(AudioSource):
        def __init__(self, inner):
            self.inner = inner

        def read(self):
            return self.inner.read()

    inner = Inner()
    track = _voice_impl.SourceAudioTrack()
    track.set_source(inner)
    await asyncio.sleep(0.1)
    track.replace_source(Wrapper(inner))
    track.replace_source(Wrapper(inner))
    await asyncio.sleep(_voice_impl.AFTER_JOIN_TIMEOUT + 0.6)
    overlapped = inner.overlap
    release.set()
    track.stop_source()
    await asyncio.sleep(0.3)
    assert not overlapped


async def test_replace_source_plays_every_frame_of_both_sources_and_calls_after_once():
    class Counter(AudioSource):
        def __init__(self, tag, n):
            self.tag, self.n, self.i = tag, n, 0

        def read(self):
            if self.i >= self.n:
                return b""
            self.i += 1
            return bytes([self.tag]) * FRAME_BYTES

    afters = []
    track = _voice_impl.SourceAudioTrack()
    track.set_source(Counter(1, 3), afters.append)
    await asyncio.sleep(0.2)  # 3 frames and the end marker of the first reader are queued
    track.replace_source(Counter(2, 5))
    got = []
    for _ in range(60):
        data = bytes((await track.recv()).planes[0])
        if any(data):
            got.append(data[0])
        if afters:
            break
    assert got == [1] * 3 + [2] * 5
    await asyncio.sleep(0.2)
    assert afters == [None]
