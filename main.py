# main.py - Full feature music bot (YouTube + Spotify fallback via YouTube)
import discord
from discord.ext import commands
import yt_dlp
import asyncio
from aiohttp import web
from collections import deque
import os
import re
import requests
import logging

# =========================================
# Auto Leave with Message (2 minutes)
# =========================================
async def check_inactivity(ctx, vc, guild_id):
    await asyncio.sleep(120)  # wait 2 minutes
    # if vc disconnected already, just return
    if not vc or not vc.channel:
        return
    if not vc.is_playing() and not vc.is_paused():
        try:
            await ctx.send("💤 Leaving the voice channel after 2 minutes of inactivity.")
        except Exception:
            pass
        try:
            await vc.disconnect()
        except Exception:
            pass
        print(f"💤 Disconnected from {vc.channel} due to inactivity")


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Optional Spotify support (Spotipy)
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except Exception:
    SPOTIFY_AVAILABLE = False
    logger.warning("Spotipy not available; Spotify playlist/album extraction needs credentials and Spotipy.")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Queue system
music_queues = {}   # guild_id -> deque of YTDLSource
now_playing = {}    # guild_id -> current YTDLSource
loop_mode = {}      # guild_id -> "off"|"one"|"all"

# yt-dlp options (tweaked)
ytdl_opts = {
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'socket_timeout': 30,
    'retries': 5,
    'fragment_retries': 5,
    'skip_unavailable_fragments': True,
    'ignoreerrors': True,
    'nocheckcertificate': True,
    'age_limit': None,
    'geo_bypass': True,
    'prefer_ffmpeg': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'best',
    }],
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'youtube_include_dash_manifest': False,
    'extractor_args': {'youtube': {'player_skip': ['configs']}},
    # cookiefile optional: put cookies.txt alongside main.py if needed to avoid 403s
    'cookiefile': 'cookies.txt',
}

ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -b:a 192k -ar 48000 -ac 2'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume: float = 0.6):
        super().__init__(source, volume)
        self.data = data or {}
        self.title = self.data.get('title', 'Unknown Title')
        self.url = self.data.get('url')
        self.duration = self.data.get('duration')
        self.thumbnail = self.data.get('thumbnail')
        self.search_query = self.data.get('search_query')

    @classmethod
    async def from_url(cls, url, *, loop=None, store_query=True):
        """
        url can be:
         - direct youtube link
         - ytsearch:query
         - any URL supported by yt-dlp
        """
        loop = loop or asyncio.get_event_loop()

        try:
            def extract():
                try:
                    return ytdl.extract_info(url, download=False)
                except Exception as e:
                    logger.error(f"yt-dlp extraction failed for {url}: {e}")
                    return None

            data = await asyncio.wait_for(loop.run_in_executor(None, extract), timeout=60.0)

            if not data:
                raise Exception("❌ Could not extract audio info (blocked/invalid).")

            # Search results return 'entries'
            if 'entries' in data:
                entries = data.get('entries') or []
                if not entries:
                    raise Exception("❌ No results found.")
                data = entries[0]

            if store_query:
                try:
                    data['search_query'] = url
                except Exception:
                    pass

            stream_url = data.get('url')
            if not stream_url:
                raise Exception("❌ No usable audio stream found in extracted data.")

            return cls(discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts), data=data)

        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout: yt-dlp took too long.")
        except Exception as e:
            logger.error(f"YTDL extraction error: {e}", exc_info=True)
            raise Exception(f"⚠️ yt-dlp error: {str(e)[:150]}")


@bot.event
async def on_ready():
    logger.info(f'✅ {bot.user} is online!')
    await bot.change_presence(activity=discord.Game(name="!commands for help"))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(f"Command error: {error}", exc_info=True)
    try:
        await ctx.send(f"❌ An error occurred: {str(error)[:200]}")
    except Exception:
        pass


def extract_spotify_title(spotify_url):
    """Fallback: try scraping the open.spotify page for a track title."""
    try:
        resp = requests.get(spotify_url, timeout=10, headers={'User-Agent': ytdl_opts['user_agent']})
        html = resp.text
        # Try og:title meta tag (commonly present)
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if m:
            title_text = m.group(1)
            title_text = title_text.replace('| Spotify', '').strip()
            return title_text
        # fallback: title tag
        m2 = re.search(r'<title>([^<]+)</title>', html)
        if m2:
            return m2.group(1).replace('| Spotify','').strip()
    except Exception as e:
        logger.error(f"Spotify page scrape failed: {e}")
    return None


def get_spotify_track_queries(spotify_url):
    """
    Prefer Spotipy if available with client credentials.
    Returns list of "Song Artist" queries to search on YouTube.
    """
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if SPOTIFY_AVAILABLE and client_id and client_secret:
        try:
            auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
            sp = spotipy.Spotify(auth_manager=auth_manager)

            if "track" in spotify_url and "playlist" not in spotify_url and "album" not in spotify_url:
                track = sp.track(spotify_url)
                name = track.get('name')
                artist = track.get('artists')[0].get('name') if track.get('artists') else ''
                queries.append(f"{name} {artist}")

            elif "playlist" in spotify_url:
                results = sp.playlist_items(spotify_url)
                while results:
                    items = results.get('items', [])
                    for item in items:
                        t = item.get('track')
                        if not t:
                            continue
                        name = t.get('name')
                        artist = t.get('artists')[0].get('name') if t.get('artists') else ''
                        queries.append(f"{name} {artist}")
                    if results.get('next'):
                        results = sp.next(results)
                    else:
                        break

            elif "album" in spotify_url:
                results = sp.album_tracks(spotify_url)
                while results:
                    items = results.get('items', [])
                    for t in items:
                        name = t.get('name')
                        artist = t.get('artists')[0].get('name') if t.get('artists') else ''
                        queries.append(f"{name} {artist}")
                    if results.get('next'):
                        results = sp.next(results)
                    else:
                        break

            return queries

        except Exception as e:
            logger.error(f"Spotify API extraction failed: {e}")

    # If Spotipy not available or credentials missing:
    # For single track, attempt to scrape title and return one query.
    if "track" in spotify_url:
        title = extract_spotify_title(spotify_url)
        if title:
            return [title]
        return []

    # For playlists/albums without API, warn user to set credentials
    return []


@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    """Play a song from YouTube, Spotify, or search query"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Join a voice channel first! 🎧")
        return

    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        try:
            await channel.connect()
        except Exception as e:
            await ctx.send(f"❌ Couldn't connect to voice channel: {e}")
            return

    async with ctx.typing():
        try:
            guild_id = ctx.guild.id
            if guild_id not in music_queues:
                music_queues[guild_id] = deque()
            if guild_id not in loop_mode:
                loop_mode[guild_id] = "off"

            # Spotify link handling
            if "spotify.com" in query:
                queries = get_spotify_track_queries(query)

                if queries:
                    # We have one or many queries to search on YouTube
                    await ctx.send(f"🎧 Spotify link detected! Adding {len(queries)} tracks to queue...")
                    added = 0
                    failed = 0
                    for q in queries:
                        search_q = f"ytsearch:{q} audio"
                        try:
                            player = await YTDLSource.from_url(search_q, loop=bot.loop)
                            music_queues[guild_id].append(player)
                            added += 1
                        except Exception as e:
                            logger.error(f"YT search error for {q}: {e}")
                            failed += 1
                            continue

                    if added == 0:
                        await ctx.send("❌ Couldn't find any matching tracks on YouTube for that Spotify link.")
                        return

                    status_msg = f"✅ Added **{added}** tracks to the queue!"
                    if failed > 0:
                        status_msg += f" (⚠️ {failed} tracks failed)"
                    await ctx.send(status_msg)
                else:
                    # No queries extracted (likely playlist/album without Spotify creds).
                    # Try scraping single track fallback:
                    title = extract_spotify_title(query)
                    if title:
                        search_q = f"ytsearch:{title} audio"
                        player = await YTDLSource.from_url(search_q, loop=bot.loop)
                        music_queues[guild_id].append(player)
                        await ctx.send(f"✅ Added to queue: **{player.title}**")
                    else:
                        await ctx.send("❌ Couldn't extract tracks from Spotify link. For full playlist/album support, set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET as environment variables.")
                        return

            else:
                # YouTube link or search query
                if not query.startswith('http'):
                    query = f"ytsearch:{query}"

                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            # If nothing playing, start playback
            if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                await play_next(ctx)

        except Exception as e:
            logger.error(f"Play command error: {e}", exc_info=True)
            await ctx.send(f"❌ Error: {e}")


async def play_next(ctx):
    """Play the next song in queue"""
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        player = music_queues[guild_id].popleft()
        now_playing[guild_id] = player

        def after(error):
            if error:
                logger.error(f"Playback error: {error}")

            # Handle loop modes
            mode = loop_mode.get(guild_id, "off")

            if mode == "one":
                async def requeue_current():
                    try:
                        current = now_playing.get(guild_id)
                        if current and hasattr(current, 'search_query') and current.search_query:
                            new_player = await YTDLSource.from_url(current.search_query, loop=bot.loop)
                            music_queues[guild_id].appendleft(new_player)
                    except Exception as e:
                        logger.error(f"Error re-queuing song (one): {e}")
                    await play_next(ctx)

                asyncio.run_coroutine_threadsafe(requeue_current(), bot.loop)
                return

            if mode == "all":
                async def requeue_for_all():
                    try:
                        current = now_playing.get(guild_id)
                        if current and hasattr(current, 'search_query') and current.search_query:
                            new_player = await YTDLSource.from_url(current.search_query, loop=bot.loop)
                            music_queues[guild_id].append(new_player)
                    except Exception as e:
                        logger.error(f"Error re-queuing song (all): {e}")
                    await play_next(ctx)

                asyncio.run_coroutine_threadsafe(requeue_for_all(), bot.loop)
                return

            # Normal: play next
            future = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error in after callback: {e}")

        # start playback
        try:
            vc.play(player, after=after)
        except Exception as e:
            logger.error(f"Failed to play audio: {e}", exc_info=True)
            # try next track
            await play_next(ctx)
            return

        # Show loop status and announce now playing
        try:
            loop_status = ""
            if loop_mode.get(guild_id) != "off":
                loop_status = " 🔂" if loop_mode[guild_id] == "one" else " 🔁"
            await ctx.send(f"🎵 Now playing: **{player.title}**{loop_status}")
        except Exception:
            pass

    else:
        now_playing.pop(guild_id, None)
        # start inactivity timer + existing idle timer
        try:
            bot.loop.create_task(check_inactivity(ctx, vc, ctx.guild.id))
        except Exception as e:
            logger.error(f"Failed to schedule inactivity task: {e}")
        await start_idle_timer(ctx)


async def start_idle_timer(ctx):
    """Disconnect the bot after 3 minutes of inactivity (compat fallback)"""
    await asyncio.sleep(180)
    try:
        if ctx.voice_client and not ctx.voice_client.is_playing():
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Leaving due to inactivity. See you later! 💤")
    except Exception as e:
        logger.error(f"Idle timer error: {e}")


@bot.command(name='loop', aliases=['l', 'repeat'])
async def loop(ctx, mode: str = None):
    guild_id = ctx.guild.id
    if guild_id not in loop_mode:
        loop_mode[guild_id] = "off"

    if mode is None:
        current = loop_mode[guild_id]
        if current == "off":
            loop_mode[guild_id] = "one"
            await ctx.send("🔂 **Loop: Current Song** - This song will repeat!")
        elif current == "one":
            loop_mode[guild_id] = "all"
            await ctx.send("🔁 **Loop: Queue** - The entire queue will repeat!")
        else:
            loop_mode[guild_id] = "off"
            await ctx.send("➡️ **Loop: Off** - Playing normally")
        return

    mode = mode.lower()
    if mode in ["off", "none", "disable", "0"]:
        loop_mode[guild_id] = "off"
        await ctx.send("➡️ **Loop: Off** - Playing normally")
    elif mode in ["one", "single", "song", "current", "1"]:
        loop_mode[guild_id] = "one"
        await ctx.send("🔂 **Loop: Current Song** - This song will repeat!")
    elif mode in ["all", "queue", "playlist", "2"]:
        loop_mode[guild_id] = "all"
        await ctx.send("🔁 **Loop: Queue** - The entire queue will repeat!")
    else:
        await ctx.send("❌ Invalid mode! Use: `!loop [off/one/all]` or just `!loop` to cycle")


@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused!")
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name='resume', aliases=['r'])
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed!")
    else:
        await ctx.send("❌ Nothing is paused right now.")


@bot.command(name='stop')
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
    if guild_id in loop_mode:
        loop_mode[guild_id] = "off"
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue!")
        await start_idle_timer(ctx)
    else:
        await ctx.send("❌ Not playing anything.")


@bot.command(name='leave', aliases=['disconnect', 'dc'])
async def leave(ctx):
    if ctx.voice_client:
        guild_id = ctx.guild.id
        if guild_id in music_queues:
            music_queues[guild_id].clear()
        if guild_id in loop_mode:
            loop_mode[guild_id] = "off"
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Bye! 🥹")
    else:
        await ctx.send("❌ I'm not in a voice channel.")


@bot.command(name='queue', aliases=['q'])
async def queue_cmd(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
        if guild_id in now_playing:
            loop_status = ""
            if guild_id in loop_mode and loop_mode[guild_id] != "off":
                if loop_mode[guild_id] == "one":
                    loop_status = " 🔂"
                elif loop_mode[guild_id] == "all":
                    loop_status = " 🔁"
            await ctx.send(f"🎵 **Now Playing:** {now_playing[guild_id].title}{loop_status}\n\n❌ Queue is empty!")
        else:
            await ctx.send("❌ Queue is empty!")
        return

    queue_text = ""
    if guild_id in now_playing:
        loop_status = ""
        if guild_id in loop_mode and loop_mode[guild_id] != "off":
            if loop_mode[guild_id] == "one":
                loop_status = " 🔂"
            elif loop_mode[guild_id] == "all":
                loop_status = " 🔁"
        queue_text += f"🎵 **Now Playing:** {now_playing[guild_id].title}{loop_status}\n\n"

    queue_text += "📃 **Queue:**\n"
    queue_list = list(music_queues[guild_id])[:15]
    for i, player in enumerate(queue_list, 1):
        queue_text += f"{i}. {player.title}\n"

    if len(music_queues[guild_id]) > 15:
        queue_text += f"\n... and {len(music_queues[guild_id]) - 15} more tracks"

    await ctx.send(queue_text)


@bot.command(name='nowplaying', aliases=['np'])
async def nowplaying(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        player = now_playing[guild_id]
        loop_status = ""
        if guild_id in loop_mode and loop_mode[guild_id] != "off":
            if loop_mode[guild_id] == "one":
                loop_status = " 🔂"
            elif loop_mode[guild_id] == "all":
                loop_status = " 🔁"
        await ctx.send(f"🎵 **Now Playing:** {player.title}{loop_status}")
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name='clear')
async def clear(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("❌ Queue is already empty!")


@bot.command(name='volume', aliases=['vol'])
async def volume(ctx, vol: int = None):
    if vol is None:
        if ctx.voice_client and ctx.voice_client.source:
            try:
                current_vol = int(ctx.voice_client.source.volume * 100)
                await ctx.send(f"🔊 Current volume: **{current_vol}%**")
            except Exception:
                await ctx.send("🔊 Current volume: unknown")
        else:
            await ctx.send("❌ Not playing anything.")
        return

    if not 0 <= vol <= 100:
        await ctx.send("❌ Volume must be between 0 and 100!")
        return

    if ctx.voice_client and ctx.voice_client.source:
        try:
            ctx.voice_client.source.volume = vol / 100
            await ctx.send(f"🔊 Volume set to **{vol}%**")
        except Exception as e:
            logger.error(f"Failed to set volume: {e}")
            await ctx.send("❌ Couldn't set volume.")
    else:
        await ctx.send("❌ Not playing anything right now.")


@bot.command(name='commands')
async def commands_list(ctx):
    help_text = """
🎵 **Music Bot Commands:**

**Playback:**
`!play <song/link>` - Play a song (YouTube, Spotify)
`!pause` - Pause music
`!resume` - Resume music
`!skip` - Skip current song
`!stop` - Stop and clear queue
`!loop [off/one/all]` - Toggle loop mode

**Queue:**
`!queue` - Show queue
`!nowplaying` - Show current song
`!clear` - Clear queue

**Other:**
`!volume <0-100>` - Set volume
`!leave` - Disconnect bot
`!commands` - Show this message

**Loop Modes:**
- `off` - Normal playback
- `one` - Repeat current song 🔂
- `all` - Repeat entire queue 🔁

**Supported:** YouTube links, YouTube search, Spotify tracks/playlists/albums (Spotify playlists/albums require SPOTIFY credentials for best results)
    """
    await ctx.send(help_text)


# Health server for Render / hosting
async def health_check(request):
    return web.Response(text="Bot is running")


async def start_web_server():
    port = int(os.getenv('PORT', 8080))
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"✅ Health check server started on port {port}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")

    async def main():
        # Start health server first so host detects a listening port
        try:
            await start_web_server()
        except Exception as e:
            logger.error(f"Failed to start health server: {e}", exc_info=True)

        if token:
            logger.info("🔑 DISCORD_TOKEN found — starting bot")
            try:
                await bot.start(token)
            except Exception as e:
                logger.error(f"Bot failed to start: {e}", exc_info=True)
                # keep process alive so host doesn't think it exited
                while True:
                    await asyncio.sleep(60)
        else:
            logger.warning("⚠️ DISCORD_TOKEN not set. Bot will NOT start. Set DISCORD_TOKEN in environment variables.")
            # Keep webserver alive for diagnostics
            while True:
                await asyncio.sleep(60)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shut down manually.")
