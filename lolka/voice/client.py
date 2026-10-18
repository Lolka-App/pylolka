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
import datetime
import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Literal, overload

from lolka import opus
from lolka.errors import ClientException, MissingVoiceDependenciesError
from lolka.player import AudioSource
from lolka.sinks.core import Sink
from lolka.sinks.errors import RecordingException
from lolka.utils import MISSING

from ..utils import get_missing_voice_dependencies
from ._types import VoiceProtocol

if TYPE_CHECKING:
    from lolka import abc
    from lolka.client import Client
    from lolka.guild import Guild, VocalGuildChannel
    from lolka.member import Member
    from lolka.opus import APPLICATION_CTL, BAND_CTL, SIGNAL_CTL
    from lolka.raw_models import (
        RawVoiceServerUpdateEvent,
        RawVoiceStateUpdateEvent,
    )
    from lolka.state import ConnectionState
    from lolka.user import ClientUser, User

    AfterCallback = Callable[[Exception | None], Any]

_log = logging.getLogger(__name__)

__all__ = ("VoiceClient",)


def _to_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class VoiceClient(VoiceProtocol):
    """Represents a lolka voice connection over WebRTC.

    You do not create these, you typically get them from e.g.
    :meth:`VoiceChannel.connect`.

    Voice in lolka works over WebRTC (not Discord UDP/libsodium/DAVE), so no
    PyNaCl or davey is needed; ``aiortc`` and ``pymediasoup`` are installed
    together with the library. The public interface stays compatible with
    py-cord's :class:`VoiceClient`, so existing bots keep working after
    ``import lolka as discord``.

    Warning
    -------
    In order to use PCM based AudioSources, you must have the opus library
    available (it is loaded automatically when it can be found). Audio is
    (re)encoded to Opus by aiortc before it is sent.

    Attributes
    ----------
    session_id: :class:`str`
        The voice connection session ID.
    token: :class:`str`
        The voice connection token.
    endpoint: :class:`str`
        The voice signaling endpoint we are connecting to.
    channel: Union[:class:`VoiceChannel`, :class:`StageChannel`]
        The channel we are connected to.
    on_receive_track: Optional[Callable]
        Optional callback ``(track, user_id, producer_id)`` invoked for every
        incoming audio track consumed from other participants. ``track`` is an
        :class:`aiortc.MediaStreamTrack`; it can be read independently of
        :meth:`start_recording`.
    recording: :class:`bool`
        Whether a recording is in progress (see :meth:`start_recording`).
    """

    def __init__(
        self,
        client: Client,
        channel: abc.Connectable,
    ) -> None:
        missing = get_missing_voice_dependencies()
        if missing:
            raise MissingVoiceDependenciesError(missing=missing)
        try:
            from . import _voice_impl as _ms
        except ImportError as exc:
            raise RuntimeError(
                "voice requires aiortc and pymediasoup: "
                'run "pip install -U lolka.pycord" to install them.'
            ) from exc

        super().__init__(client, channel)
        self._ms = _ms
        state = client._connection
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state

        self._session_id: str | None = None
        self._token: str | None = None
        self._endpoint: str | None = None
        self._state_ready: asyncio.Event = asyncio.Event()
        self._server_ready: asyncio.Event = asyncio.Event()

        self._conn: Any = None  # _voice_impl.VoiceConnection
        self._connected: bool = False
        self._timeout: float = 30.0
        # migrations to a new voice server run one at a time
        self._migrate_lock: asyncio.Lock = asyncio.Lock()
        self._player_future: asyncio.Future[Exception | None] | None = None

        self.on_receive_track: Callable[[Any, Any, Any], Any] | None = None

        # producer_id -> (track, user_id, relay); the relay lets recording and
        # on_receive_track read the same incoming track independently
        self._remote_tracks: dict[str, tuple[Any, Any, Any]] = {}
        self._record_tasks: dict[str, asyncio.Task] = {}
        self._sink: Sink | None = None
        self._recording_after: tuple[AfterCallback | None, tuple[Any, ...]] = (
            None,
            (),
        )
        self.recording: bool = False

    @property
    def guild(self) -> Guild:
        """Returns the guild the channel we're connecting to is bound to."""
        channel: VocalGuildChannel = self.channel
        return channel.guild

    @property
    def user(self) -> ClientUser:
        """The user connected to voice (i.e. ourselves)"""
        return self._state.user  # type: ignore

    @property
    def session_id(self) -> str | None:
        """The session ID of the current voice call."""
        return self._session_id

    @property
    def token(self) -> str | None:
        """The token of the voice connection. You should not share this."""
        return self._token

    @property
    def endpoint(self) -> str | None:
        """The endpoint where the client is connected."""
        return self._endpoint

    @property
    def timeout(self) -> float:
        """The amount of seconds the client waits before killing connect attempts."""
        return self._timeout

    @property
    def ssrc(self) -> int:
        """Always ``0``: WebRTC voice has no client-side SSRC."""
        return 0

    @property
    def mode(self) -> str:
        """Always ``"webrtc"``: there is no UDP encryption mode."""
        return "webrtc"

    @property
    def secret_key(self) -> list[int]:
        """Always empty: WebRTC voice has no shared secret key."""
        return []

    @property
    def ws(self) -> None:
        """Always ``None``: there is no Discord voice websocket."""
        return None

    @property
    def decoder(self) -> type[opus.Decoder]:
        """Format description (channels, sample size, rate) of recorded PCM, used by sinks."""
        return opus.Decoder

    def is_dave_connection(self) -> bool:
        """Always ``False``: DAVE is not used by WebRTC voice."""
        return False

    @property
    def privacy_code(self) -> str | None:
        """Always ``None``: DAVE is not used by WebRTC voice."""
        return None

    @property
    def latency(self) -> float:
        """Voice latency in seconds.

        Not tracked for the WebRTC transport; always ``0.0``.
        """
        return 0.0

    @property
    def average_latency(self) -> float:
        """Average voice latency in seconds (see :attr:`latency`)."""
        return 0.0

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        _log.debug("Dispatching voice_client event %s", event)
        self.client.dispatch(event, *args, **kwargs)

    # connection related

    async def on_voice_state_update(self, data: RawVoiceStateUpdateEvent) -> None:
        self._session_id = data.session_id
        if data.channel_id is None:
            # we were disconnected from outside (kick/move/delete)
            self.loop.create_task(self._teardown())
            return
        if self.channel is None or data.channel_id != self.channel.id:
            new_channel = data.channel
            if new_channel is not None:
                self.channel = new_channel
        self._state_ready.set()

    async def on_voice_server_update(self, data: RawVoiceServerUpdateEvent) -> None:
        token = data.token
        endpoint = data.endpoint
        if not token or not endpoint:
            _log.warning("Awaiting voice server endpoint...")
            return
        # A repeated VOICE_SERVER_UPDATE with another endpoint for an already
        # connected client means the voice server changed (failover or the
        # channel moved to another instance): move to it. A token change alone
        # keeps the session (the backend reissues the token on every
        # VOICE_STATE_UPDATE, e.g. on self_mute).
        server_changed = self._connected and endpoint != self._endpoint
        self._token = token
        self._endpoint = endpoint
        self._server_ready.set()
        if server_changed:
            self.loop.create_task(self._migrate())

    async def _migrate(self) -> None:
        """Rebuilds the WebRTC session on the new voice server.

        The outgoing track is carried over to the new session, so the current
        playback (source, ``after`` callback, pause) is not interrupted. An
        active recording continues on the tracks of the new session.
        """
        async with self._migrate_lock:
            old = self._conn
            endpoint, token = self._endpoint, self._token
            if old is None or not self._connected or not endpoint or not token:
                return
            if old._endpoint == endpoint:
                return  # already moved (events arrived in a batch)

            _log.info("voice server changed, migrating to endpoint=%s", endpoint)
            # detach the track from the old session, or its close() would reset the source
            out_track = old.out_track
            old.out_track = None
            conn = self._ms.VoiceConnection(
                endpoint,
                token,
                on_receive_track=self._handle_track,
                out_track=out_track,
            )
            # Set the new session right away: play()/stop()/is_playing() keep
            # working with the same track while it comes up.
            self._conn = conn
            self._drop_remote_tracks()
            try:
                await old.close()
            except Exception:
                pass

            try:
                await conn.start()
            except Exception:
                if self._conn is not conn:
                    return  # disconnected while migrating
                _log.exception("failed to migrate to the new voice server")
                await self.disconnect(force=True)

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        self._timeout = timeout
        guild = self.channel.guild
        await guild.change_voice_state(
            channel=self.channel, self_mute=self_mute, self_deaf=self_deaf
        )
        await asyncio.wait_for(
            asyncio.gather(self._state_ready.wait(), self._server_ready.wait()),
            timeout=timeout,
        )
        if not self._endpoint or not self._token:
            raise ClientException("voice server did not provide an endpoint/token")

        _log.info("connecting to voice endpoint=%s", self._endpoint)
        conn = self._ms.VoiceConnection(
            self._endpoint, self._token, on_receive_track=self._handle_track
        )
        self._conn = conn
        try:
            await conn.start()
        except BaseException:
            await self.disconnect(force=True)
            raise
        self._connected = True

    def _handle_track(self, track: Any, user_id: Any, producer_id: Any) -> None:
        relay = self._ms.MediaRelay()
        uid = _to_int(user_id)
        self._remote_tracks[producer_id] = (track, uid, relay)
        track.on("ended", lambda: self._forget_track(producer_id, track))

        if self.recording:
            self._start_record_task(producer_id)

        cb = self.on_receive_track
        if cb is not None:
            result = cb(relay.subscribe(track), user_id, producer_id)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)

    def _forget_track(self, producer_id: str, track: Any) -> None:
        entry = self._remote_tracks.get(producer_id)
        if entry is not None and entry[0] is track:
            del self._remote_tracks[producer_id]

    def _drop_remote_tracks(self) -> None:
        self._remote_tracks.clear()
        for task in list(self._record_tasks.values()):
            task.cancel()
        self._record_tasks.clear()

    def wait_until_connected(self, timeout: float | None = 30.0) -> bool:
        return self._connected

    def is_connected(self) -> bool:
        """Whether the voice client is connected to voice."""
        return self._connected

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        self._finish_recording()
        conn, self._conn = self._conn, None
        self._connected = False
        try:
            if conn is not None:
                await conn.close()
        finally:
            self._drop_remote_tracks()
            try:
                await self.channel.guild.change_voice_state(channel=None)
            except Exception:
                pass
            self.cleanup()

    async def move_to(
        self, channel: abc.Snowflake | None, *, timeout: float | None = 30.0
    ) -> None:
        """|coro|

        Moves you to a different voice channel.

        .. note::

            For the WebRTC transport this changes the gateway voice state to the
            target channel. Re-establishing the media session on a new voice
            server happens through the resulting ``VOICE_SERVER_UPDATE``.

        Parameters
        ----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. If this is ``None``, it is an equivalent of calling :meth:`.disconnect`.
        timeout: Optional[:class:`float`]
            Not used for the WebRTC transport; kept for compatibility.
        """
        if channel is None:
            await self.disconnect()
            return
        if self.channel and channel.id == self.channel.id:
            return
        await self.channel.guild.change_voice_state(channel=channel)

    async def _teardown(self) -> None:
        self.stop()
        self._finish_recording()
        conn, self._conn = self._conn, None
        self._connected = False
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        self._drop_remote_tracks()
        try:
            self.cleanup()
        except Exception:
            pass

    # audio related

    @overload
    def play(
        self,
        source: AudioSource,
        *,
        after: AfterCallback | None = ...,
        application: APPLICATION_CTL = ...,
        bitrate: int = ...,
        fec: bool = ...,
        expected_packet_loss: float = ...,
        bandwidth: BAND_CTL = ...,
        signal_type: SIGNAL_CTL = ...,
        wait_finish: Literal[False] = ...,
    ) -> None: ...

    @overload
    def play(
        self,
        source: AudioSource,
        *,
        after: AfterCallback | None = ...,
        application: APPLICATION_CTL = ...,
        bitrate: int = ...,
        fec: bool = ...,
        expected_packet_loss: float = ...,
        bandwidth: BAND_CTL = ...,
        signal_type: SIGNAL_CTL = ...,
        wait_finish: Literal[True],
    ) -> asyncio.Future[Exception | None]: ...

    def play(
        self,
        source: AudioSource,
        *,
        after: AfterCallback | None = None,
        application: APPLICATION_CTL = "audio",
        bitrate: int = 128,
        fec: bool = True,
        expected_packet_loss: float = 0.15,
        bandwidth: BAND_CTL = "full",
        signal_type: SIGNAL_CTL = "auto",
        wait_finish: bool = False,
    ) -> None | asyncio.Future[Exception | None]:
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted,
        stopped or an error occurred.

        The audio is fed into the WebRTC send track and (re)encoded to Opus by
        aiortc, then streamed. The Opus encoder parameters (``application``,
        ``bitrate``, ``fec``, ``expected_packet_loss``, ``bandwidth``,
        ``signal_type``) are accepted for py-cord API compatibility but are
        managed by aiortc for this transport.

        Parameters
        ----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[Optional[:class:`Exception`]], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.
        wait_finish: :class:`bool`
            If ``True``, then an awaitable is returned that waits for the audio source to be
            exhausted, and will return an optional exception that could have been raised.

            If ``False``, ``None`` is returned and the function does not block.

        Raises
        ------
        ClientException
            Already playing audio, or not connected to voice.
        TypeError
            Source is not a :class:`AudioSource`, or after is not a callable.
        """
        if not self.is_connected():
            raise ClientException("Not connected to voice")
        if self.is_playing():
            raise ClientException("Already playing audio")
        if not isinstance(source, AudioSource):
            raise TypeError(
                f"Source must be an AudioSource, not {source.__class__.__name__}",
            )
        if after is not None and not callable(after):
            raise TypeError(
                f"after must be a callable, not {after.__class__.__name__}",
            )

        future: asyncio.Future[Exception | None] | None = None
        if wait_finish:
            future = self.loop.create_future()
            self._player_future = future

        def _after(exc: Exception | None) -> None:
            try:
                if after is not None:
                    after(exc)
                elif exc is not None:
                    _log.error("Error while playing audio", exc_info=exc)
            finally:
                if future is not None:
                    self._set_future_result_if_pending(future, exc)
                    if self._player_future is future:
                        self._player_future = None

        self._conn.out_track.set_source(source, _after)
        return future

    @staticmethod
    def _set_future_result_if_pending(
        future: asyncio.Future[Any], result: Exception | None
    ) -> None:
        if not future.done():
            future.set_result(result)

    def is_playing(self) -> bool:
        """Indicates if we're playing audio."""
        track = self._conn.out_track if self._conn else None
        return track is not None and track.is_playing

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        track = self._conn.out_track if self._conn else None
        return track is not None and track.is_paused

    def stop(self) -> None:
        """Stops playing audio, if applicable.

        The ``after`` callback of the stopped source is called.
        """
        track = self._conn.out_track if self._conn else None
        if track is not None:
            track.stop_source()
        future, self._player_future = self._player_future, None
        if future is not None:
            self._set_future_result_if_pending(future, None)

    def pause(self) -> None:
        """Pauses the audio playing."""
        track = self._conn.out_track if self._conn else None
        if track is not None:
            track.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        track = self._conn.out_track if self._conn else None
        if track is not None:
            track.resume()

    @property
    def source(self) -> AudioSource | None:
        """The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        track = self._conn.out_track if self._conn else None
        return track.source if track is not None else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f"expected AudioSource, not {value.__class__.__name__}")

        track = self._conn.out_track if self._conn else None
        if track is None or track.source is None:
            raise ValueError("the client is not playing anything")
        track.set_source(value, track._after)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Not supported: WebRTC voice has no raw packet channel.

        Use :meth:`play` with an :class:`AudioSource` instead.

        Raises
        ------
        ClientException
            Always.
        """
        raise ClientException(
            "send_audio_packet is not supported over WebRTC voice, use play() instead"
        )

    def elapsed(self) -> datetime.timedelta:
        """Returns the elapsed time of the playing audio."""
        track = self._conn.out_track if self._conn else None
        if track is not None and track.source is not None:
            return datetime.timedelta(milliseconds=track.played_frames * 20)
        return datetime.timedelta()

    # recording

    def start_recording(
        self,
        sink: Sink,
        callback: AfterCallback | None = None,
        *args: Any,
        sync_start: bool = MISSING,
    ) -> None:
        r"""Start recording the audio from the current connected channel to the provided sink.

        Every participant's incoming WebRTC audio track is decoded to 48 kHz
        16-bit stereo PCM and written to ``sink`` under the participant's user ID.
        Only received audio is written (no silence is inserted between talking).

        .. versionadded:: 2.0

        Parameters
        ----------
        sink: :class:`~.Sink`
            A Sink in which all audio will be processed in.
        callback: Callable[..., Any]
            A function (or coroutine function) which is called after the recording
            has stopped and the sink was cleaned up. It receives the sink as the
            first positional argument, followed by ``*args``.
        \*args:
            The arguments to pass to the callback.
        sync_start: :class:`bool`
            Ignored; kept for compatibility.

        Raises
        ------
        RecordingException
            Not connected to a voice channel
        TypeError
            You did not provide a Sink object.
        ClientException
            Already recording audio.
        """
        if not self.is_connected():
            raise RecordingException("not connected to a voice channel")
        if not isinstance(sink, Sink):
            raise TypeError(f"expected a Sink object, got {sink.__class__.__name__}")
        if callback is not None and not callable(callback):
            raise TypeError(
                f"expected a callable for the callback, got {callback.__class__.__name__}"
            )
        if self.is_recording():
            raise ClientException("Already recording audio")

        self._sink = sink
        self._recording_after = (callback, args)
        self.recording = True
        sink.init(self)
        for producer_id in list(self._remote_tracks):
            self._start_record_task(producer_id)

    start_listening = start_recording

    def _start_record_task(self, producer_id: str) -> None:
        sink = self._sink
        entry = self._remote_tracks.get(producer_id)
        if sink is None or entry is None or producer_id in self._record_tasks:
            return
        track, user_id, relay = entry
        proxy = relay.subscribe(track)
        self._record_tasks[producer_id] = self.loop.create_task(
            self._record(proxy, sink, user_id, producer_id),
            name=f"voice-record: {producer_id}",
        )

    async def _record(
        self, proxy: Any, sink: Sink, user_id: Any, producer_id: str
    ) -> None:
        def write(data: bytes, user: Any) -> None:
            if self.recording and self._sink is sink:
                sink.write(data, user)

        try:
            await self._ms.record_track(proxy, write, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("error while recording audio of user %s", user_id)
        finally:
            proxy.stop()
            if self._record_tasks.get(producer_id) is asyncio.current_task():
                del self._record_tasks[producer_id]

    def stop_recording(self) -> None:
        """Stops the recording of the provided ``sink``.

        The sink is cleaned up (audio is formatted) and then the callback passed to
        :meth:`start_recording` is called.

        .. versionadded:: 2.0

        Raises
        ------
        RecordingException
            You are not recording.
        """
        if not self.is_recording():
            raise RecordingException("You are not recording")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._finish_recording()
        else:
            # called from another thread (e.g. the sink's time filter)
            self.loop.call_soon_threadsafe(self._finish_recording)

    stop_listening = stop_recording

    def _finish_recording(self) -> None:
        sink = self._sink
        if sink is None or not self.recording:
            return
        callback, args = self._recording_after
        self.recording = False
        self._sink = None
        self._recording_after = (None, ())
        for task in list(self._record_tasks.values()):
            task.cancel()
        self._record_tasks.clear()

        try:
            sink.cleanup()
        except Exception:
            _log.exception("error while cleaning up the sink")
        if callback is None:
            return
        try:
            result = callback(sink, *args)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result, loop=self.loop)
        except Exception:
            _log.exception("error while calling the recording callback")

    def is_recording(self) -> bool:
        """Whether the current client is recording in any sink."""
        return self.recording

    def is_speaking(self, member: Member | User) -> bool | None:
        """Not tracked for the WebRTC transport; always ``None`` (unknown).

        .. versionadded:: 2.7
        """
        return None
