from flask import Flask
from threading import Thread
import discord
from discord.ext import commands
import yt_dlp
import asyncio
from collections import deque
import os
import re
import requests
import traceback

# Optional Spotify support
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    spotipy = None

# ---- Flask keep-alive ----
app = Flask(__name__)

@app.route('/')
def home():
    return "🎵 Music Bot is alive and running!"

@app.route('/health')
def health():
    return {"status": "healthy", "bot": "online"}

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
    print("✅ Flask server started on port 8080")

# ---- Bot setup ----
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Disable default help so our commands alias 'help' works
try:
    bot.remove_command('help')
except Exception:
    pass

# ---- Queues / state ----
music_queues = {}    # guild_id -> deque of players
now_playing = {}     # guild_id -> current player
loop_mode = {}       # guild_id -> 'off' | 'track' | 'queue'
loop_queue_backup = {}

# ---- yt-dlp + ffmpeg options (improved audio) ----
ytdl_opts = {
    # Prefer webm/opus where available, fallback to best audio
    'format': 'bestaudio[ext=webm+acodec=opus]/bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'socket_timeout': 30,
    'retries': 3,
    'fragment_retries': 3,
    'skip_unavailable_fragments': True,
    'ignoreerrors': True,
    'no_check_certificate': True,
    'extract_flat': False,
    'noplaylist': False,
}

# Send good audio to Discord (Discord expects 48kHz, stereo)
ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    # -vn remove video, -ac 2 stereo, -ar 48000 sample rate, -b:a 192k target bitrate
    'options': '-vn -ac 2 -ar 48000 -b:a 192k'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

# ---- Helper class to wrap sources ----
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data or {}
        self.title = self.data.get('title') or "Unknown title"
        # 'url' in ytdl result might be the direct stream url (best), or None
        self.url = self.data.get('url')
        # webpage_url is the original youtube/watch url when available
        self.webpage_url = self.data.get('webpage_url') or self.data.get('webpage_url') or None
        # store id too
        self.id = self.data.get('id')

    @classmethod
    async def from_url(cls, url, *, loop=None, download=False):
        """Extract info via yt-dlp in threadpool, return YTDLSource (with FFmpegPCMAudio).
           `url` can be a direct url or a 'ytsearch:' query.
        """
        loop = loop or asyncio.get_event_loop()
        try:
            # run blocking ytdl.extract_info in executor
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=download)),
                timeout=90.0
            )

            if not data:
                raise Exception("No data returned by yt-dlp.")

            # If a playlist/search was returned, pick first valid entry
            if isinstance(data, dict) and 'entries' in data:
                entries = data.get('entries') or []
                # find first non-empty entry
                entry = None
                for e in entries:
                    if e:
                        entry = e
                        break
                if not entry:
                    raise Exception("No valid entries found in yt-dlp response.")
                data = entry

            # At this point data should be an item dict
            stream_url = data.get('url')
            # Sometimes the 'url' is not present (older versions), but 'formats' exist
            if not stream_url:
                formats = data.get('formats') or []
                # pick best audio format url if available
                best = None
                for f in reversed(formats):
                    if f and f.get('acodec') != 'none' and f.get('filesize') is not None:
                        best = f
                        break
                if not best and formats:
                    best = formats[-1]
                if best:
                    stream_url = best.get('url')

            if not stream_url:
                raise Exception("Missing audio stream URL in yt-dlp data.")

            # Create FFmpeg audio source from stream_url
            ff_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts)
            return cls(ff_source, data=data)

        except asyncio.TimeoutError:
            raise Exception("Timeout: yt-dlp took too long to respond.")
        except Exception as e:
            # Keep traceback on console for debugging, but raise readable message up
            print("YTDL error:", e)
            traceback.print_exc()
            raise Exception(f"yt-dlp error: {str(e)}")

# ---- Events ----
@bot.event
async def on_ready():
    print(f'✅ {bot.user} is online!')

# ---- Spotify helpers ----
def extract_spotify_title(spotify_url: str):
    """Try to extract song title from Spotify page HTML (no API required).
       This is a best-effort fallback and can fail for private/blocked content.
    """
    try:
        response = requests.get(spotify_url, timeout=10)
        if response.status_code != 200:
            return None
        html = response.text
        match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if match:
            title_text = match.group(1)
            clean_title = title_text.replace('| Spotify', '').strip()
            return clean_title
    except Exception:
        return None
    return None

def get_spotify_track_queries(spotify_url: str):
    """Return list of 'Song Artist' strings using spotipy if credentials available.
       If Spotipy or credentials missing, returns empty list.
    """
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not spotipy or not client_id or not client_secret:
        return queries

    try:
        auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        sp = spotipy.Spotify(auth_manager=auth_manager)

        # single track
        if "track" in spotify_url and "playlist" not in spotify_url:
            track = sp.track(spotify_url)
            if track:
                name = track.get('name') or ''
                artists = track.get('artists') or []
                artist = artists[0].get('name') if artists else ''
                queries.append(f"{name} {artist}".strip())

        # playlist
        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            items = results.get('items', []) if results else []
            while True:
                for item in items:
                    track = item.get('track') if item else None
                    if not track:
                        continue
                    name = track.get('name') or ''
                    artists = track.get('artists') or []
                    artist = artists[0].get('name') if artists else ''
                    queries.append(f"{name} {artist}".strip())
                if results and results.get('next'):
                    results = sp.next(results)
                    items = results.get('items', []) if results else []
                else:
                    break

        # album
        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            items = results.get('items', []) if results else []
            while True:
                for item in items:
                    name = item.get('name') or ''
                    artists = item.get('artists') or []
                    artist = artists[0].get('name') if artists else ''
                    queries.append(f"{name} {artist}".strip())
                if results and results.get('next'):
                    results = sp.next(results)
                    items = results.get('items', []) if results else []
                else:
                    break

    except Exception as e:
        print("Spotify API error:", e)
        traceback.print_exc()

    return queries

# ---- YouTube playlist extractor (safe) ----
async def get_youtube_playlist(url: str):
    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False)), timeout=90.0)
        if not data:
            return []
        entries = []
        # sometimes data is dict with 'entries'
        if isinstance(data, dict) and 'entries' in data:
            raw = data.get('entries') or []
            for e in raw:
                if e:
                    entries.append(e)
        # sometimes extract_info returns list already
        elif isinstance(data, list):
            for e in data:
                if e:
                    entries.append(e)
        return entries
    except Exception as e:
        print(f"Playlist extraction error: {e}")
        traceback.print_exc()
        return []

# ---- Playback helpers ----
async def safe_connect_voice(channel: discord.VoiceChannel):
    """Ensure connection to voice channel and return voice_client"""
    if channel.guild.voice_client:
        # already connected somewhere in this guild
        vc = channel.guild.voice_client
        if vc.channel.id != channel.id:
            await vc.move_to(channel)
        return vc
    else:
        return await channel.connect()

async def play_next(ctx):
    guild_id = ctx.guild.id

    # If there are queued tracks, pop the next (or handle 'track loop')
    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        if loop_mode.get(guild_id) == 'track' and guild_id in now_playing:
            # re-use same track (try to re-create source if possible)
            current = now_playing.get(guild_id)
            player = current
            try:
                # try to re-create from webpage_url if available to avoid stale streams
                if getattr(current, 'webpage_url', None):
                    player = await YTDLSource.from_url(current.webpage_url, loop=bot.loop)
            except Exception:
                # fallback to current object if recreate failed
                player = current
        else:
            player = music_queues[guild_id].popleft()
            if loop_mode.get(guild_id) == 'queue':
                # append a copy reference to queue for queue looping
                music_queues[guild_id].append(player)

        now_playing[guild_id] = player

        def after(error):
            if error:
                print(f"Playback after error: {error}")
            # schedule next track on bot.loop
            fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                fut.result()
            except Exception as e:
                print("Error scheduling next:", e)
                traceback.print_exc()

        try:
            # If not connected, try connect
            if not ctx.voice_client:
                await safe_connect_voice(ctx.author.voice.channel)
            ctx.voice_client.play(player, after=after)
        except Exception as e:
            print("Error playing source:", e)
            traceback.print_exc()
            # try to continue to next track
            fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                fut.result()
            except Exception:
                pass
            return

        loop_emoji = ""
        if loop_mode.get(guild_id) == 'track':
            loop_emoji = " 🔂"
        elif loop_mode.get(guild_id) == 'queue':
            loop_emoji = " 🔁"

        # Announce now playing
        try:
            await ctx.send(f"🎵 Now playing: **{player.title}**{loop_emoji}")
        except Exception:
            # failing to send message shouldn't stop playback
            pass

    else:
        # nothing to play -> clear now_playing and start idle disconnect timer
        now_playing.pop(guild_id, None)
        await start_idle_timer(ctx)

async def start_idle_timer(ctx):
    # After 2 minutes of nothing playing, disconnect
    await asyncio.sleep(120)
    if ctx.voice_client and not ctx.voice_client.is_playing():
        try:
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Leaving due to inactivity.")
        except Exception:
            pass

# ---- Commands ----
@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first! 🤡")
        return

    channel = ctx.author.voice.channel
    # connect or move to that channel
    try:
        if not ctx.voice_client:
            await safe_connect_voice(channel)
    except Exception as e:
        await ctx.send("❌ Unable to connect to your voice channel.")
        print("Voice connect error:", e)
        traceback.print_exc()
        return

    async with ctx.typing():
        try:
            guild_id = ctx.guild.id
            if guild_id not in music_queues:
                music_queues[guild_id] = deque()
            if guild_id not in loop_mode:
                loop_mode[guild_id] = 'off'

            # Spotify link handling
            if "spotify.com" in query:
                queries = get_spotify_track_queries(query)

                if queries:
                    # Found track list via API -> add each as ytsearch
                    await ctx.send(f"🎧 Spotify link detected! Adding {len(queries)} tracks to queue...")
                    added = 0
                    for q in queries:
                        search_q = f"ytsearch:{q} audio"
                        try:
                            player = await YTDLSource.from_url(search_q, loop=bot.loop)
                            music_queues[guild_id].append(player)
                            added += 1
                        except Exception as e:
                            print("YT search error for:", q, e)
                            continue

                    if added == 0:
                        await ctx.send("❌ Couldn't find any tracks on YouTube for that Spotify link.")
                        return
                    await ctx.send(f"✅ Added **{added}** tracks to the queue!")
                else:
                    # Try HTML title fallback
                    title = extract_spotify_title(query)
                    if not title:
                        await ctx.send("❌ Couldn't extract song name from Spotify link. Try giving the song name instead.")
                        return
                    # show only one detection message
                    await ctx.send(f"🎧 Spotify song detected! Searching for: **{title}**")
                    search_q = f"ytsearch:{title} audio"
                    player = await YTDLSource.from_url(search_q, loop=bot.loop)
                    music_queues[guild_id].append(player)
                    await ctx.send(f"✅ Added to queue: **{player.title}**")

            # YouTube playlist detection
            elif "youtube.com/playlist" in query or "youtu.be/playlist" in query or "&list=" in query:
                await ctx.send("📋 YouTube playlist detected! Extracting tracks...")
                entries = await get_youtube_playlist(query)
                if not entries:
                    await ctx.send("❌ Couldn't extract playlist tracks.")
                    return

                added = 0
                for entry in entries:
                    if not entry:
                        continue
                    try:
                        # safe attempt to get a watch URL or id
                        video_id = entry.get('id')
                        video_url = entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
                        if not video_url:
                            # fallback to using the entry itself with ytdl
                            video_url = entry
                        player = await YTDLSource.from_url(video_url, loop=bot.loop)
                        music_queues[guild_id].append(player)
                        added += 1
                    except Exception as e:
                        print(f"Error adding playlist track: {e}")
                        continue

                await ctx.send(f"✅ Added **{added}** tracks from playlist to queue!")

            else:
                # generic youtube/search handling
                if not query.startswith('http'):
                    # treat as search
                    query = f"ytsearch:{query}"
                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            # If nothing currently playing, start playback
            if not ctx.voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            traceback.print_exc()

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
        await ctx.send("❌ Nothing is playing right now. 😤")

@bot.command(name='resume', aliases=['r'])
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed!")
    else:
        await ctx.send("❌ Nothing is paused right now. 😤")

@bot.command(name='stop')
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
    loop_mode[guild_id] = 'off'
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue!")
        await start_idle_timer(ctx)

@bot.command(name='leave', aliases=['disconnect', 'dc'])
async def leave(ctx):
    if ctx.voice_client:
        guild_id = ctx.guild.id
        if guild_id in music_queues:
            music_queues[guild_id].clear()
        loop_mode[guild_id] = 'off'
        try:
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Bye! 🥹")
        except Exception:
            await ctx.send("❌ Error disconnecting.")
    else:
        await ctx.send("❌ I'm not in a voice channel!")

@bot.command(name='queue', aliases=['q'])
async def queue_cmd(ctx):
    guild_id = ctx.guild.id
    q = music_queues.get(guild_id, deque())
    if not q and guild_id not in now_playing:
        await ctx.send("❌ Queue is empty!")
        return

    queue_text = "🎵 **Music Queue:**\n\n"
    if guild_id in now_playing:
        queue_text += f"**Now Playing:** {now_playing[guild_id].title}\n\n"

    for i, player in enumerate(list(q)[:10], 1):
        queue_text += f"{i}. {player.title}\n"

    if len(q) > 10:
        queue_text += f"\n...and {len(q) - 10} more tracks"

    if loop_mode.get(guild_id) == 'track':
        queue_text += "\n\n🔂 **Loop:** Current Track"
    elif loop_mode.get(guild_id) == 'queue':
        queue_text += "\n\n🔁 **Loop:** Entire Queue"

    await ctx.send(queue_text)

@bot.command(name='loop', aliases=['l'])
async def loop_command(ctx, mode: str = None):
    guild_id = ctx.guild.id
    if guild_id not in loop_mode:
        loop_mode[guild_id] = 'off'

    if mode is None:
        # cycle: off -> track -> queue -> off
        current = loop_mode[guild_id]
        if current == 'off':
            loop_mode[guild_id] = 'track'
            await ctx.send("🔂 **Loop:** Current track enabled!")
        elif current == 'track':
            loop_mode[guild_id] = 'queue'
            await ctx.send("🔁 **Loop:** Entire queue enabled!")
        else:
            loop_mode[guild_id] = 'off'
            await ctx.send("❌ **Loop:** Disabled")
    else:
        mode = mode.lower()
        if mode in ['track', 't', 'song', 'single']:
            loop_mode[guild_id] = 'track'
            await ctx.send("🔂 **Loop:** Current track enabled!")
        elif mode in ['queue', 'q', 'all']:
            loop_mode[guild_id] = 'queue'
            await ctx.send("🔁 **Loop:** Entire queue enabled!")
        elif mode in ['off', 'stop', 'disable']:
            loop_mode[guild_id] = 'off'
            await ctx.send("❌ **Loop:** Disabled")
        else:
            await ctx.send("❌ Invalid mode! Use: `!loop track`, `!loop queue`, or `!loop off`")

@bot.command(name='nowplaying', aliases=['np'])
async def nowplaying(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        player = now_playing[guild_id]
        loop_status = ""
        if loop_mode.get(guild_id) == 'track':
            loop_status = " 🔂"
        elif loop_mode.get(guild_id) == 'queue':
            loop_status = " 🔁"
        await ctx.send(f"🎵 **Now Playing:** {player.title}{loop_status}")
    else:
        await ctx.send("❌ Nothing is playing right now!")

@bot.command(name='commands', aliases=['help'])
async def commands_cmd(ctx):
    help_text = """
🎵 **Music Bot Commands:**

**!play <song/link>** or **!p** - Play a song (YouTube, Spotify, or search)
**!skip** or **!s** - Skip current song
**!pause** - Pause music
**!resume** or **!r** - Resume music
**!loop** or **!l** - Toggle loop (off → track → queue → off)
**!loop track** - Loop current track
**!loop queue** - Loop entire queue
**!loop off** - Disable loop
**!queue** or **!q** - Show queue
**!nowplaying** or **!np** - Show current song
**!stop** - Stop and clear queue
**!leave** or **!dc** - Disconnect bot

**Supports:**
✅ YouTube links & playlists
✅ Spotify links, playlists & albums (Spotipy optional)
✅ Search by song name
    """
    await ctx.send(help_text)

# ---- Start Flask and Bot ----
keep_alive()

token = os.getenv('DISCORD_TOKEN')
if not token:
    print("ERROR: DISCORD_TOKEN environment variable not set. Bot will not start.")
else:
    try:
        bot.run(token)
    except Exception as e:
        print("Bot crashed on run:", e)
        traceback.print_exc()
