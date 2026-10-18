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
    RecordingCallback = Callable[..., Any]

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
    In order to play Opus based AudioSources (``is_opus()`` returns ``True``) you
    must have the libopus library available (it is loaded automatically when it
    can be found): they are decoded to PCM first. PCM based AudioSources do not
    need libopus, audio is encoded to Opus by aiortc before it is sent.

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
        :meth:`start_recording`. The track only delivers frames from the moment
        it was handed over and keeps just the latest frame, so read it
        (``await track.recv()`` in a loop until ``MediaStreamError``) or call
        ``track.stop()`` when you are not interested in it.
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
                'run "pip install -U pylolka" to install them.'
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
        self._tasks: set[asyncio.Task] = set()
        self._request_timeout: float = 15.0
        # The backend moves the bot between rooms by kicking it from the old SFU
        # room and issuing a new token (VOICE_STATE_UPDATE, then
        # VOICE_SERVER_UPDATE with the same endpoint on the same instance).
        self._room_changed: bool = False
        self._channel_id: Any = getattr(channel, "id", None)
        self._moved: asyncio.Event = asyncio.Event()
        self._kick_grace: float = 5.0
        self._disconnecting: bool = False
        self._left: asyncio.Event = asyncio.Event()
        self._leave_wait: float = 1.5

        self.on_receive_track: Callable[[Any, Any, Any], Any] | None = None

        # producer_id -> (track, user_id, relay, drain task); the relay lets the
        # drain, recording and on_receive_track read the same incoming track
        # independently
        self._remote_tracks: dict[str, tuple[Any, Any, Any, asyncio.Task]] = {}
        self._record_tasks: dict[str, asyncio.Task] = {}
        self._record_errors: list[BaseException] = []
        self._finishing: set[asyncio.Task] = set()
        self._sink: Sink | None = None
        self._recording_after: tuple[RecordingCallback | None, tuple[Any, ...]] = (
            None,
            (),
        )
        self.recording: bool = False

    # tasks

    def _spawn(self, coro: Any, name: str | None = None) -> asyncio.Task:
        task = self.loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error(
                "unhandled error in voice task %s", task.get_name(), exc_info=exc
            )

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
        if data.channel_id is None:
            if self._disconnecting:
                self._left.set()
                return
            if not self._state_ready.is_set():
                # a late "left the channel" of the previous session of this guild
                return
            self._session_id = data.session_id
            # we were disconnected from outside (kick/move/delete)
            self._spawn(self._teardown(), name="voice-teardown")
            return
        self._session_id = data.session_id
        if data.channel_id != self._channel_id:
            # also while the first session is still coming up (connect())
            if self._connected or self._conn is not None:
                self._room_changed = True
            self._channel_id = data.channel_id
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
        # A repeated VOICE_SERVER_UPDATE for an already connected client means
        # that the voice server changed (failover or the channel moved to another
        # instance) or that the bot was moved to another room (new token for the
        # new room, possibly on the same instance): rebuild the session. A token
        # change alone keeps the session (the backend reissues the token on every
        # VOICE_STATE_UPDATE, e.g. on self_mute).
        server_changed = self._connected and (
            endpoint != self._endpoint or self._room_changed
        )
        self._token = token
        self._endpoint = endpoint
        self._server_ready.set()
        if server_changed:
            self._room_changed = False
            self._moved.set()
            self._spawn(self._migrate(), name="voice-migrate")

    def _make_conn(
        self, endpoint: str, token: str, out_track: Any = None
    ) -> Any:
        conn = self._ms.VoiceConnection(
            endpoint,
            token,
            on_receive_track=self._handle_track,
            out_track=out_track,
            on_closed=lambda reason: self._spawn(
                self._on_conn_closed(conn, reason), name="voice-conn-closed"
            ),
            on_track_ended=self._forget_producer,
            request_timeout=self._request_timeout,
        )
        return conn

    async def _on_conn_closed(self, conn: Any, reason: str) -> None:
        """The voice server dropped our session (kicked / connection lost).

        When the bot is being moved to another room, the new VOICE_SERVER_UPDATE
        follows and rebuilds the session (``_migrate``); otherwise the client is
        disconnected (the voice state is cleared); a lost connection is not
        re-established by the library.
        """
        if conn is not self._conn:
            return
        _log.info("voice session ended (%s)", reason)
        try:
            await asyncio.wait_for(self._moved.wait(), self._kick_grace)
        except asyncio.TimeoutError:
            pass
        else:
            return
        if self._conn is conn:
            # leave the room properly (op4 channel=None), or the backend keeps
            # showing the bot in the channel for a while
            await self.disconnect(force=True)

    async def _migrate(self) -> None:
        """Rebuilds the WebRTC session on the new voice server / room.

        The outgoing track is carried over to the new session, so the current
        playback (source, ``after`` callback, pause) is not interrupted. An
        active recording continues on the tracks of the new session.
        """
        try:
            async with self._migrate_lock:
                old = self._conn
                endpoint, token = self._endpoint, self._token
                if old is None or not self._connected or not endpoint or not token:
                    return
                if old._endpoint == endpoint and old._token == token:
                    return  # already moved (events arrived in a batch)

                _log.info("voice server changed, migrating to endpoint=%s", endpoint)
                # detach the track from the old session, or its close() would reset the source
                out_track = old.out_track
                conn = self._make_conn(endpoint, token, out_track)
                # Set the new session right away: play()/stop()/is_playing() keep
                # working with the same track while it comes up; only then detach
                # the track from the old one (threads never see a track-less conn).
                self._conn = conn
                old.out_track = None
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
        finally:
            self._moved.clear()

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        """|coro|

        Connects to the voice channel.

        ``reconnect`` is accepted for py-cord compatibility but has no effect:
        if the voice connection is lost (the voice server restarted, the network
        dropped), the client disconnects and leaves the channel; reconnecting is
        up to the bot.
        """
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
        conn = self._make_conn(self._endpoint, self._token)
        self._conn = conn
        try:
            await conn.start()
        except BaseException:
            await self.disconnect(force=True)
            raise
        self._connected = True
        if conn._endpoint != self._endpoint or (
            self._room_changed and conn._token != self._token
        ):
            # the bot was moved while the session was coming up
            self._room_changed = False
            self._moved.set()
            self._spawn(self._migrate(), name="voice-migrate")

    def _handle_track(self, track: Any, user_id: Any, producer_id: Any) -> None:
        relay = self._ms.MediaRelay()
        uid = _to_int(user_id)
        old = self._remote_tracks.pop(producer_id, None)
        if old is not None:
            old[3].cancel()
        # Always read the track: aiortc queues every decoded frame of a received
        # track without limit, and the relay (unbuffered) keeps only the latest
        # frame for the other subscribers.
        drain = self._spawn(
            self._drain(relay.subscribe(track, buffered=False)),
            name=f"voice-drain: {producer_id}",
        )
        self._remote_tracks[producer_id] = (track, uid, relay, drain)
        track.on("ended", lambda: self._forget_track(producer_id, track))

        if self.recording:
            self._start_record_task(producer_id)

        cb = self.on_receive_track
        if cb is not None:
            result = cb(relay.subscribe(track, buffered=False), user_id, producer_id)
            if inspect.isawaitable(result):
                self._spawn(result, name=f"voice-on-receive-track: {producer_id}")

    async def _drain(self, proxy: Any) -> None:
        try:
            await self._ms.drain_track(proxy)
        finally:
            proxy.stop()

    def _forget_track(self, producer_id: str, track: Any) -> None:
        entry = self._remote_tracks.get(producer_id)
        if entry is not None and entry[0] is track:
            self._forget_producer(producer_id)

    def _forget_producer(self, producer_id: str) -> None:
        """The participant's track is gone: stop everything attached to it."""
        entry = self._remote_tracks.pop(producer_id, None)
        if entry is not None:
            entry[3].cancel()
        task = self._record_tasks.pop(producer_id, None)
        if task is not None:
            task.cancel()

    def _drop_remote_tracks(self) -> None:
        for entry in self._remote_tracks.values():
            entry[3].cancel()
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
        self._disconnecting = True
        self._connected = False  # no new play() from after-callbacks of stop()
        self.stop()
        await self._finish_recording()
        await self._wait_finishing()
        conn, self._conn = self._conn, None
        try:
            if conn is not None:
                await conn.close()
        finally:
            self._drop_remote_tracks()
            left = False
            try:
                await self.channel.guild.change_voice_state(channel=None)
                left = self._state_ready.is_set()
            except Exception:
                pass
            if left:
                # The backend answers with VOICE_STATE_UPDATE(channel=null); let it
                # arrive here, otherwise it lands in the next VoiceClient of this
                # guild and tears it down.
                try:
                    await asyncio.wait_for(self._left.wait(), self._leave_wait)
                except asyncio.TimeoutError:
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
        self._connected = False
        self.stop()
        await self._finish_recording()
        await self._wait_finishing()
        conn, self._conn = self._conn, None
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

    async def _wait_finishing(self) -> None:
        if self._finishing:
            await asyncio.gather(*list(self._finishing), return_exceptions=True)

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
        stopped or an error occurred. It runs in a separate thread, *after*
        ``source.cleanup()`` (upstream calls it the other way round). If
        ``source.read()`` hangs, ``after`` is called after 2 seconds, before
        ``cleanup()``, which runs once ``read()`` returns.

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
        OpusNotLoaded
            The source is Opus based (``is_opus()``) and libopus is not available.
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

        conn = self._conn
        track = conn.out_track if conn else None
        if track is None:
            raise ClientException("Not connected to voice.")

        future: asyncio.Future[Exception | None] | None = None
        if wait_finish:
            future = self.loop.create_future()
            self._player_future = future

        # ``_after`` runs in a helper thread, not in the event loop (like in
        # py-cord, where it runs in the player thread): a blocking ``after`` does
        # not freeze the bot and may call :meth:`play` again.
        def _after(exc: Exception | None) -> None:
            try:
                if after is not None:
                    after(exc)
                elif exc is not None:
                    _log.error("Error while playing audio", exc_info=exc)
            finally:
                if future is not None:
                    self._resolve_future(future, exc)

        try:
            track.set_source(source, _after)
        except BaseException:
            if future is not None and self._player_future is future:
                self._player_future = None
            raise
        return future

    def _resolve_future(
        self, future: asyncio.Future[Any], result: Exception | None
    ) -> None:
        """Resolves a ``wait_finish`` future from any thread."""

        def resolve() -> None:
            if not future.done():
                future.set_result(result)
            if self._player_future is future:
                self._player_future = None

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            resolve()
            return
        try:
            self.loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            pass  # the loop is closed

    def is_playing(self) -> bool:
        """Indicates if we're playing audio."""
        conn = self._conn
        track = conn.out_track if conn else None
        return track is not None and track.is_playing

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        conn = self._conn
        track = conn.out_track if conn else None
        return track is not None and track.is_paused

    def stop(self) -> None:
        """Stops playing audio, if applicable.

        The ``after`` callback of the stopped source is called.
        """
        future, self._player_future = self._player_future, None
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.stop_source()
        if future is not None:
            self._resolve_future(future, None)

    def pause(self) -> None:
        """Pauses the audio playing."""
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.resume()

    @property
    def source(self) -> AudioSource | None:
        """The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        The previous source is *not* cleaned up (the new one usually wraps it, e.g.
        ``vc.source = PCMVolumeTransformer(vc.source)``); the ``after`` callback and
        the paused state are kept.

        Raises
        ------
        TypeError
            The new source is not an :class:`AudioSource` (setter).
        ValueError
            The client is not playing anything (setter).
        """
        conn = self._conn
        track = conn.out_track if conn else None
        return track.source if track is not None else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f"expected AudioSource, not {value.__class__.__name__}")

        conn = self._conn
        track = conn.out_track if conn else None
        if track is None or track.source is None:
            raise ValueError("the client is not playing anything")
        track.replace_source(value)

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
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None and track.source is not None:
            return datetime.timedelta(milliseconds=track.played_frames * 20)
        return datetime.timedelta()

    # recording

    def start_recording(
        self,
        sink: Sink,
        callback: RecordingCallback | None = None,
        *args: Any,
        sync_start: bool = MISSING,
    ) -> None:
        r"""Start recording the audio from the current connected channel to the provided sink.

        Every participant's incoming WebRTC audio track is decoded to 48 kHz
        16-bit stereo PCM and written to ``sink`` under the participant's user ID.
        Only audio received after this call is written, and no silence is inserted
        between talking: the tracks of different users are not time-aligned.

        .. versionadded:: 2.0

        Parameters
        ----------
        sink: :class:`~.Sink`
            A Sink in which all audio will be processed in.
        callback: Callable[..., Any]
            Called after the recording has stopped and the sink was cleaned up
            (the cleanup runs in a worker thread, so the event loop is not
            blocked by e.g. ffmpeg conversion). Both py-cord contracts are
            supported:

            - if ``*args`` are passed or ``callback`` is a coroutine function
              (py-cord 2.0 - 2.6 style, ``async def callback(sink, channel)``):
              ``callback(sink, *args)``; a coroutine is scheduled as a task;
            - otherwise (py-cord 2.7+ style): ``callback(error)`` where ``error``
              is the first exception raised while recording or cleaning up the
              sink, or ``None``.
        \*args:
            The arguments to pass to the callback (selects the first contract).
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
        if self.is_recording() or self._sink is not None:
            raise ClientException("Already recording audio")

        self._sink = sink
        self._recording_after = (callback, args)
        self._record_errors = []
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
        track, user_id, relay, _ = entry
        # unbuffered: only frames that arrive after this point are recorded
        proxy = relay.subscribe(track, buffered=False)
        self._record_tasks[producer_id] = self._spawn(
            self._record(proxy, sink, user_id, producer_id, self._record_errors),
            name=f"voice-record: {producer_id}",
        )

    async def _record(
        self,
        proxy: Any,
        sink: Sink,
        user_id: Any,
        producer_id: str,
        errors: list[BaseException],
    ) -> None:
        def write(data: bytes, user: Any) -> None:
            if self.recording and self._sink is sink:
                sink.write(data, user)

        try:
            await self._ms.record_track(proxy, write, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("error while recording audio of user %s", user_id)
            errors.append(exc)
        finally:
            proxy.stop()
            if self._record_tasks.get(producer_id) is asyncio.current_task():
                del self._record_tasks[producer_id]

    def stop_recording(self) -> None:
        """Stops the recording of the provided ``sink``.

        The sink is finalized asynchronously: it is cleaned up (audio is formatted,
        off the event loop) and then the callback passed to :meth:`start_recording`
        is called, i.e. the callback arrives after this method returns. A new
        recording can be started right away when this is called from the event
        loop. From another thread the finalization is scheduled through the loop,
        and until it runs :meth:`start_recording` raises :exc:`ClientException`.

        .. versionadded:: 2.0

        Raises
        ------
        RecordingException
            You are not recording.
        """
        if not self.is_recording():
            raise RecordingException("You are not recording")
        self.recording = False  # no more writes, a second call raises
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._begin_finish_recording()
        else:
            self.loop.call_soon_threadsafe(self._begin_finish_recording)

    stop_listening = stop_recording

    def _begin_finish_recording(self) -> None:
        # the state is taken right away, so start_recording() may be called again
        # while the previous sink is still being finalized
        state = self._take_recording()
        if state is None:
            return
        task = self._spawn(
            self._finish_recording(state), name="voice-finish-recording"
        )
        self._finishing.add(task)
        task.add_done_callback(self._finishing.discard)

    def _take_recording(self) -> Any:
        """Detach the recording state from the client; ``None`` if there is none."""
        sink = self._sink
        if sink is None:
            return None
        state = (
            sink,
            *self._recording_after,
            self._record_errors,
            list(self._record_tasks.values()),
        )
        self.recording = False
        self._sink = None
        self._recording_after = (None, ())
        self._record_errors = []
        self._record_tasks.clear()
        return state

    async def _finish_recording(self, state: Any = None) -> None:
        if state is None:
            state = self._take_recording()
            if state is None:
                return
        sink, callback, args, errors, tasks = state
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.loop.run_in_executor(None, sink.cleanup)
        except Exception as exc:
            _log.exception("error while cleaning up the sink")
            errors.append(exc)
        if callback is None:
            return
        try:
            if args or inspect.iscoroutinefunction(callback):
                result = callback(sink, *args)
            else:
                result = callback(errors[0] if errors else None)
            if inspect.isawaitable(result):
                self._spawn(result, name="voice-recording-callback")
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
