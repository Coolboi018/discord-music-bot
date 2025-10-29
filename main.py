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
loop_mode = {}

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
    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'best'}],
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'youtube_include_dash_manifest': False,
    'extractor_args': {'youtube': {'player_skip': ['configs']}},
    'cookiefile': 'cookies.txt',
}

ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -b:a 192k -ar 48000 -ac 2'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.6):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.search_query = data.get('search_query')

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
                data['search_query'] = url

            stream_url = data.get('url')
            if not stream_url:
                raise Exception("❌ No valid audio stream found.")

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
    logger.error(f"Command error: {error}")
    await ctx.send(f"❌ An error occurred: {str(error)[:200]}")


# all your command definitions remain unchanged ...


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
    print(f"✅ Health check server started on port {port}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ DISCORD_TOKEN not found in environment variables!")
        exit(1)

    async def main():
        port = int(os.getenv("PORT", 8080))
        logger.info(f"🌐 Starting health check server on port {port}")
        await start_web_server()

        logger.info("🤖 Starting Discord bot...")
        await bot.start(token)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shut down manually.")
