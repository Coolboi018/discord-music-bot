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
loop_mode = {}  # New: Track loop mode per guild

# Enhanced yt-dlp options for better audio quality
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
    'no_check_certificate': True,
    'extract_flat': False,
    'cachedir': False,
    'nocheckcertificate': True,
    'age_limit': None,
    'geo_bypass': True,
    'prefer_ffmpeg': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'best',
    }],
}

# Enhanced FFmpeg options for better audio quality
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
        self.search_query = data.get('search_query')  # Store for loop functionality

    @classmethod
    async def from_url(cls, url, *, loop=None, store_query=True):
        loop = loop or asyncio.get_event_loop()

        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False)),
                timeout=60.0
            )

            if 'entries' in data:
                if not data['entries']:
                    raise Exception("❌ No results found for this song.")
                data = data['entries'][0]

            if store_query:
                data['search_query'] = url

            return cls(
                discord.FFmpegPCMAudio(data['url'], **ffmpeg_opts),
                data=data
            )

        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout: YouTube took too long to respond. Try again!")
        except Exception as e:
            logger.error(f"YTDL Error: {e}")
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


def extract_spotify_title(spotify_url):
    """Try to extract song title from a Spotify link (no API needed)"""
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
    """Returns list of 'Song Artist' search queries from Spotify URL"""
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
                    artist = item.get('artists')[0].get('name') if item.get('artists') else ''
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
    """Play a song from YouTube, Spotify, or search query"""
    if not ctx.author.voice:
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
    """Play the next song in queue"""
    guild_id = ctx.guild.id

    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        player = music_queues[guild_id].popleft()
        now_playing[guild_id] = player

        def after(error):
            if error:
                logger.error(f"Playback error: {error}")
            
            # Handle loop modes
            if guild_id in loop_mode:
                mode = loop_mode[guild_id]
                
                if mode == "one":
                    # Re-add current song to front of queue
                    async def requeue_current():
                        try:
                            if guild_id in now_playing:
                                current = now_playing[guild_id]
                                if hasattr(current, 'search_query') and current.search_query:
                                    new_player = await YTDLSource.from_url(
                                        current.search_query, 
                                        loop=bot.loop
                                    )
                                    music_queues[guild_id].appendleft(new_player)
                        except Exception as e:
                            logger.error(f"Error re-queuing song: {e}")
                        
                        await play_next(ctx)
                    
                    asyncio.run_coroutine_threadsafe(requeue_current(), bot.loop)
                    return
                
                elif mode == "all":
                    # Re-add current song to end of queue
                    async def requeue_for_all():
                        try:
                            if guild_id in now_playing:
                                current = now_playing[guild_id]
                                if hasattr(current, 'search_query') and current.search_query:
                                    new_player = await YTDLSource.from_url(
                                        current.search_query, 
                                        loop=bot.loop
                                    )
                                    music_queues[guild_id].append(new_player)
                        except Exception as e:
                            logger.error(f"Error re-queuing song: {e}")
                        
                        await play_next(ctx)
                    
                    asyncio.run_coroutine_threadsafe(requeue_for_all(), bot.loop)
                    return
            
            # Normal behavior: play next song
            future = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error in after callback: {e}")

        ctx.voice_client.play(player, after=after)
        
        # Show loop status in now playing message
        loop_status = ""
        if guild_id in loop_mode and loop_mode[guild_id] != "off":
            if loop_mode[guild_id] == "one":
                loop_status = " 🔂"
            elif loop_mode[guild_id] == "all":
                loop_status = " 🔁"
        
        await ctx.send(f"🎵 Now playing: **{player.title}**{loop_status}")
    else:
        now_playing.pop(guild_id, None)
        await start_idle_timer(ctx)


async def start_idle_timer(ctx):
    """Disconnect the bot after 3 minutes of inactivity"""
    await asyncio.sleep(180)
    if ctx.voice_client and not ctx.voice_client.is_playing():
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Leaving due to inactivity. See you later! 💤")


@bot.command(name='loop', aliases=['l', 'repeat'])
async def loop(ctx, mode: str = None):
    """Toggle loop mode: off, one (current song), all (queue)"""
    guild_id = ctx.guild.id
    
    if guild_id not in loop_mode:
        loop_mode[guild_id] = "off"
    
    if mode is None:
        # Cycle through modes: off -> one -> all -> off
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
    else:
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
    """Skip the current song"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name='pause')
async def pause(ctx):
    """Pause the current song"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused!")
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name='resume', aliases=['r'])
async def resume(ctx):
    """Resume the paused song"""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed!")
    else:
        await ctx.send("❌ Nothing is paused right now.")


@bot.command(name='stop')
async def stop(ctx):
    """Stop playback and clear queue"""
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
    """Disconnect the bot from voice channel"""
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
async def queue(ctx):
    """Display the current queue"""
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
    """Show currently playing song"""
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
    """Clear the queue"""
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("❌ Queue is already empty!")


@bot.command(name='volume', aliases=['vol'])
async def volume(ctx, vol: int = None):
    """Change the player volume (0-100)"""
    if vol is None:
        if ctx.voice_client and ctx.voice_client.source:
            current_vol = int(ctx.voice_client.source.volume * 100)
            await ctx.send(f"🔊 Current volume: **{current_vol}%**")
        else:
            await ctx.send("❌ Not playing anything.")
        return
    
    if not 0 <= vol <= 100:
        await ctx.send("❌ Volume must be between 0 and 100!")
        return
    
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = vol / 100
        await ctx.send(f"🔊 Volume set to **{vol}%**")
    else:
        await ctx.send("❌ Not playing anything right now.")


@bot.command(name='commands')
async def commands_list(ctx):
    """Show all available commands"""
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

**Supported:** YouTube links, YouTube search, Spotify tracks/playlists/albums
    """
    await ctx.send(help_text)


async def health_check(request):
    """Responds to Render's health check request."""
    return web.Response(text="Bot is running")

async def start_web_server():
    """Starts a minimal aiohttp web server to satisfy Render's Web Service requirements."""
    # Use the PORT environment variable provided by Render, defaulting to 8080
    port = int(os.getenv('PORT', 8080)) 
    
    app = web.Application()
    app.router.add_get('/', health_check) 
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Bind to 0.0.0.0 and the determined port
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Health check server started on port {port}")
    
if __name__ == "__main__":
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("DISCORD_TOKEN not found in environment variables!")
        exit(1)
    
    # CRITICAL: Get the event loop and schedule the web server task
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server()) 
    
    # Run the Discord bot (this is the blocking call)
    bot.run(token)
