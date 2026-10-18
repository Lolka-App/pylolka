lolka.pycord
============

**Fork of** `pycord <https://github.com/Pycord-Development/pycord>`_ **(MIT License, © 2015-2021 Rapptz, © 2021-present Pycord Development).**

``lolka.pycord`` (imported as ``import lolka``) is a Python wrapper for the **lolka** bot API, built on
py-cord 2.8.1. It targets lolka by default — no manual REST/Gateway overrides needed — decodes lolka
snowflakes, and resolves lolka CDN asset URLs. Slash commands, cogs, UI components, ``lolka.ext.commands``,
``lolka.ext.bridge``, ``lolka.ext.pages`` and ``lolka.ext.tasks`` keep the py-cord API. See ``LICENSE`` for
licensing (the original MIT copyrights are retained).

Installing
----------

**Python 3.10 or higher is required**

.. code:: sh

    # Linux/macOS
    python3 -m pip install -U lolka.pycord

    # Windows
    py -3 -m pip install -U lolka.pycord

Quick Example
-------------

.. code:: py

    import lolka

    bot = lolka.Bot()

    @bot.slash_command()
    async def ping(ctx):
        await ctx.respond("pong")

    bot.run("token")

Bot Example
~~~~~~~~~~~

.. code:: py

    import lolka
    from lolka.ext import commands

    intents = lolka.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=">", intents=intents)

    @bot.command()
    async def ping(ctx):
        await ctx.send("pong")

    bot.run("token")

Configuration
-------------

The defaults point to lolka. They are read from the environment when ``lolka`` is imported,
so set them before ``import lolka``:

- ``LOLKA_API_BASE`` — REST API base URL (default ``https://lolka.app/api/bot``)
- ``LOLKA_GATEWAY`` — Gateway URL (default ``wss://lolka.app/ws/bot``)
- ``LOLKA_CDN`` — CDN base URL for asset links (default ``https://cdn.lolka.app``)

Voice
-----

Voice works over WebRTC (lolka's voice stack), not Discord's UDP/PyNaCl/DAVE transport.
The required dependencies (``aiortc``, ``pymediasoup``) ship with the base install — nothing extra to add.

The API stays py-cord-compatible — ``await channel.connect()`` returns a ``VoiceClient`` that speaks
lolka's WebRTC voice under the hood:

.. code:: py

    vc = await channel.connect()
    vc.play(lolka.FFmpegPCMAudio("song.mp3"))

    # receive other participants' audio (optional):
    def on_track(track, user_id, producer_id):
        ...  # track is an aiortc MediaStreamTrack
    vc.on_receive_track = on_track

    ...
    await vc.disconnect()

Recording works through the usual sinks. Every participant's incoming audio is written to the sink as
48 kHz 16-bit stereo PCM under the participant's user ID (only received audio — no silence is inserted
between talking). The callback gets the sink first, followed by the extra arguments, and may be a
coroutine function:

.. code:: py

    async def finished(sink, channel):
        files = [lolka.File(audio.file, f"{user_id}.wav") for user_id, audio in sink.audio_data.items()]
        await channel.send("Done recording!", files=files)

    vc = await channel.connect()
    vc.start_recording(lolka.sinks.WaveSink(), finished, text_channel)
    ...
    vc.stop_recording()

Recording and ``on_receive_track`` can be used together. Raw UDP helpers (``send_audio_packet``) are not
available over WebRTC — use ``vc.play()`` with an audio source.

Migrating from pycord
---------------------

Already have a pycord bot? You don't have to rewrite it — just alias the import:

.. code:: py

    import lolka as discord
    from lolka.ext import commands

    # everything below is unmodified pycord bot code
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.default())

    @bot.slash_command()
    async def ping(ctx):
        await ctx.respond("pong")

    bot.run("token")

``import lolka as discord`` covers every ``discord.Whatever`` attribute access in your existing code
(``discord.Bot``, ``discord.Intents``, ``discord.Embed``, ``discord.option``, ``discord.sinks``,
``discord.ui`` and so on). The only lines that actually need editing are explicit submodule imports,
since Python resolves those by real module path rather than by the alias:

- ``import discord`` → ``import lolka as discord``
- ``from discord import X`` → ``from lolka import X``
- ``from discord import option`` → ``from lolka import option``
- ``from discord.ext import commands`` → ``from lolka.ext import commands`` (same for ``bridge``, ``pages``, ``tasks``)
- ``from discord.commands import ...`` → ``from lolka.commands import ...``
- ``from discord.ui import ...`` → ``from lolka.ui import ...``

That's it — the rest of your bot's code stays exactly as it is.
