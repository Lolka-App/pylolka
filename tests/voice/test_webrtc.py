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
import wave
from fractions import Fraction

import av
import pytest
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

import lolka.voice.client as voice_client_module
from lolka.player import PCMAudio
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
        "print(lolka.__version__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_no_missing_voice_dependencies():
    assert get_missing_voice_dependencies() == ()


async def _read_all(track, count):
    return [await track.recv() for _ in range(count)]


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
    assert bytes(frame.planes[0])[:2] == b"\x01\x00"
    assert track.is_playing

    track.pause()
    assert track.is_paused
    silent = await track.recv()
    assert not any(bytes(silent.planes[0])[:FRAME_BYTES])
    assert track.played_frames == 1

    track.resume()
    await track.recv()
    assert results == []
    await track.recv()  # source exhausted
    assert results == [None]
    assert track.source is None


async def test_stop_source_calls_after_once():
    track = _voice_impl.SourceAudioTrack()
    results = []
    track.set_source(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 5)), results.append)
    track.stop_source()
    track.stop_source()
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
    return vc


async def test_play_wait_finish_resolves_future():
    vc = _make_vc()
    seen = []
    future = vc.play(
        PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)), after=seen.append, wait_finish=True
    )
    assert isinstance(future, asyncio.Future)
    assert vc.is_playing()
    await vc._conn.out_track.recv()
    assert not future.done()
    await vc._conn.out_track.recv()
    assert future.done() and future.result() is None
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
    assert seen == [None]


async def test_play_returns_none_without_wait_finish():
    vc = _make_vc()
    assert vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES))) is None
    with pytest.raises(voice_client_module.ClientException):
        vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES)))


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


async def test_recording_into_sink_and_callback():
    vc = _make_vc()
    hook_calls = []
    vc.on_receive_track = lambda track, uid, pid: hook_calls.append((track, uid, pid))

    early = _FakeRemoteTrack([_frame(1), _frame(2)])
    vc._handle_track(early, "42", "p-early")
    assert len(hook_calls) == 1

    done = []
    sink = WaveSink()

    async def callback(s, *extra):
        done.append((s, extra))

    vc.start_recording(sink, callback, "extra")
    assert vc.is_recording()

    late = _FakeRemoteTrack([_frame(3)])
    vc._handle_track(late, 43, "p-late")

    await asyncio.sleep(0.1)
    vc.stop_recording()
    assert not vc.is_recording()
    await asyncio.sleep(0)

    assert done == [(sink, ("extra",))]
    assert set(sink.audio_data) == {42, 43}
    for audio in sink.audio_data.values():
        assert audio.finished
        assert len(audio.file.getvalue()) > 0
    with wave.open(sink.audio_data[42].file, "rb") as wav:
        assert wav.getframerate() == 48000
        assert wav.getnchannels() == 2
        assert wav.getnframes() == 2 * 960
    with pytest.raises(RecordingException):
        vc.stop_recording()


async def test_hook_track_is_readable_while_recording():
    vc = _make_vc()
    got = []
    vc.on_receive_track = lambda track, uid, pid: got.append(track)
    remote = _FakeRemoteTrack([_frame(1), _frame(2), _frame(3)])
    vc._handle_track(remote, 7, "p")
    sink = WaveSink()
    vc.start_recording(sink)
    frames = []
    while True:
        try:
            frames.append(await got[0].recv())
        except MediaStreamError:
            break
    assert len(frames) == 3
    vc.stop_recording()
    assert len(sink.audio_data[7].file.getvalue()) > 0


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
