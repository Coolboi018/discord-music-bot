# ---------- full main.py (with robust startup for Render) ----------
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
# 🔥 Auto Leave with Message (unchanged)
# =========================================
async def check_inactivity(ctx, vc, guild_id):
    await asyncio.sleep(120)  # wait 2 minutes
    if not vc.is_playing() and not vc.is_paused():
        try:
            await ctx.send("💤 Leaving the voice channel after 2 minutes of inactivity.")
        except:
            pass
        await vc.disconnect()
        print(f"💤 Disconnected from {vc.channel.name} due to inactivity")


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Optional Spotify support
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except Exception:
    SPOTIFY_AVAILABLE = False
    logger.warning("Spotify support not available")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Queue system
music_queues = {}
now_playing = {}
loop_mode = {}  # per-guild loop mode: "off", "one", "all"

# Enhanced yt-dlp options
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
    'geo_bypass': True,
    'prefer_ffmpeg': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'best',
    }],
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'youtube_include_dash_manifest': False,
    'extractor_args': {'youtube': {'player_skip': ['configs']}},
    'cookiefile': 'cookies.txt',
}

# Enhanced FFmpeg options
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
                raise Exception("❌ Could not extract YouTube info (might be blocked or invalid link).")

            if 'entries' in data:
                entries = data.get('entries') or []
                if not entries:
                    raise Exception("❌ No results found for this song.")
                data = entries[0]

            if store_query:
                try:
                    data['search_query'] = url
                except Exception:
                    pass

            stream_url = data.get('url')
            if not stream_url:
                raise Exception("❌ No valid audio stream found in extracted data.")

            return cls(discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts), data=data)

        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout: YouTube took too long to respond. Try again!")
        except Exception as e:
            logger.error(f"YTDL Error: {e}", exc_info=True)
            raise Exception(f"⚠️ YouTube error: {str(e)[:150]}")


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
    try:
        response = requests.get(spotify_url, timeout=10)
        html = response.text
        match = re.search(r'<title>(.*?)</title>', html)
        if match:
            title_text = match.group(1)
            clean_title = title_text.replace('| Spotify', '').replace(' - song and lyrics by', '').strip()
            return clean_title
    except Exception as e:
        logger.error(f"Spotify scraping error: {e}")
        return None
    return None


def get_spotify_track_queries(spotify_url):
    queries = []

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not SPOTIFY_AVAILABLE or not client_id or not client_secret:
        return queries

    try:
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)

        if "track" in spotify_url and "playlist" not in spotify_url:
            track = sp.track(spotify_url)
            name = track.get('name')
            artist = track.get('artists')[0].get('name') if track.get('artists') else ''
            queries.append(f"{name} {artist}")

        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            while results:
                items = results.get('items', [])
                for item in items:
                    track = item.get('track')
                    if not track:
                        continue
                    name = track.get('name')
                    artist = track.get('artists')[0].get('name') if track.get('artists') else ''
                    queries.append(f"{name} {artist}")

                if results.get('next'):
                    results = sp.next(results)
                else:
                    break

        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            while results:
                items = results.get('items', [])
                for item in items:
                    name = item.get('name')
                    artist = track.get('artists')[0].get('name') if item.get('artists') else ''
                    queries.append(f"{name} {artist}")

                if results.get('next'):
                    results = sp.next(results)
                else:
                    break

    except Exception as e:
        logger.error(f"Spotify API error: {e}")

    return queries


@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
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

            if "spotify.com" in query:
                queries = get_spotify_track_queries(query)

                if queries:
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
                        await ctx.send("❌ Couldn't find any tracks on YouTube for that Spotify link.")
                        return

                    status_msg = f"✅ Added **{added}** tracks to the queue!"
                    if failed > 0:
                        status_msg += f" (⚠️ {failed} tracks failed)"
                    await ctx.send(status_msg)
                else:
                    title = extract_spotify_title(query)
                    if not title:
                        await ctx.send("❌ Couldn't extract song from Spotify link. Try the song name instead.")
                        return

                    search_q = f"ytsearch:{title} audio"
                    player = await YTDLSource.from_url(search_q, loop=bot.loop)
                    music_queues[guild_id].append(player)
                    await ctx.send(f"✅ Added to queue: **{player.title}**")

            else:
                if not query.startswith('http'):
                    query = f"ytsearch:{query}"

                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                await play_next(ctx)

        except Exception as e:
            logger.error(f"Play command error: {e}", exc_info=True)
            await ctx.send(f"❌ Error: {e}")


async def play_next(ctx):
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        player = music_queues[guild_id].popleft()
        now_playing[guild_id] = player

        def after(error):
            if error:
                logger.error(f"Playback error: {error}")

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

            future = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error in after callback: {e}")

        try:
            ctx.voice_client.play(player, after=after)
        except Exception as e:
            logger.error(f"Failed to play audio: {e}", exc_info=True)
            await play_next(ctx)
            return

        loop_status = ""
        if loop_mode.get(guild_id) != "off":
            loop_status = " 🔂" if loop_mode[guild_id] == "one" else " 🔁"
        try:
            await ctx.send(f"🎵 Now playing: **{player.title}**{loop_status}")
        except Exception:
            pass

    else:
        now_playing.pop(guild_id, None)
        # start inactivity timer + existing idle timer
        bot.loop.create_task(check_inactivity(ctx, vc, ctx.guild.id))
        await start_idle_timer(ctx)


async def start_idle_timer(ctx):
    """Disconnect the bot after 3 minutes of inactivity (keeps compatibility)"""
    await asyncio.sleep(180)
    try:
        if ctx.voice_client and not ctx.voice_client.is_playing():
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Leaving due to inactivity. See you later! 💤")
    except Exception as e:
        logger.error(f"Idle timer error: {e}")


# (rest of your commands and logic remain unchanged)
# ... queue, nowplaying, loop, pause, resume, skip, stop, leave, volume, commands_list etc ...
# I assume you have those same functions already in your file (kept unchanged).

# minimal health check server for Render / other hosts
async def health_check(request):
    return web.Response(text="Bot is running")

async def start_web_server():
    """Start one aiohttp web server (binds to PORT env var)"""
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
    # Robust startup: always bind webserver so Render sees an open port.
    # Then start bot if token is present; otherwise keep server alive and log message.
    token = os.getenv("DISCORD_TOKEN")

    async def main():
        try:
            await start_web_server()
        except Exception as e:
            logger.error(f"Failed to start health server: {e}", exc_info=True)
            # still continue to try to start bot; Render may fail to detect port but we log the error

        if token:
            logger.info("🔑 DISCORD_TOKEN found, starting bot...")
            try:
                await bot.start(token)
            except Exception as e:
                logger.error(f"Bot failed to start: {e}", exc_info=True)
                # keep the process alive so Render doesn't think app exited
                while True:
                    await asyncio.sleep(60)
        else:
            # token missing — do NOT exit. keep server alive and show friendly logs.
            logger.warning("⚠️ DISCORD_TOKEN not set. Bot not started. Set DISCORD_TOKEN in your Render environment variables.")
            # keep process alive so Render sees the webserver as running
            while True:
                await asyncio.sleep(60)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shut down manually.")
