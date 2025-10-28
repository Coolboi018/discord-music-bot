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

# Optional Spotify support
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    spotipy = None

# Flask web server (keep-alive)
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

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

# Music system
music_queues = {}
now_playing = {}
loop_mode = {}  # off / track / queue

# yt-dlp options
ytdl_opts = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
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
    'http_headers': {'User-Agent': 'Mozilla/5.0'}
}

ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.7):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.webpage_url = data.get('webpage_url')

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(loop.run_in_executor(
                None, lambda: ytdl.extract_info(url, download=False)), timeout=60.0)
            if 'entries' in data and data['entries']:
                data = data['entries'][0]
            return cls(discord.FFmpegPCMAudio(data['url'], **ffmpeg_opts), data=data)
        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout: YouTube took too long to respond.")
        except Exception as e:
            raise Exception(f"⚠️ Error: {str(e)[:150]}")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

def extract_spotify_title(spotify_url):
    """Extract song title from a Spotify link (fallback, no API)"""
    try:
        r = requests.get(spotify_url, timeout=10)
        html = r.text
        match = re.search(r'<title>(.*?)</title>', html)
        if match:
            title = match.group(1).replace("| Spotify", "").strip()
            return title
    except:
        return None
    return None

def get_spotify_track_queries(spotify_url):
    """Return track search queries from Spotify (requires API)"""
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not spotipy or not client_id or not client_secret:
        return queries

    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret))
        if "track" in spotify_url:
            track = sp.track(spotify_url)
            name = track.get('name')
            artist = track.get('artists', [{}])[0].get('name', '')
            queries.append(f"{name} {artist}")
        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            for item in results.get('items', []):
                track = item.get('track')
                if track:
                    name = track.get('name')
                    artist = track.get('artists', [{}])[0].get('name', '')
                    queries.append(f"{name} {artist}")
        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            for item in results.get('items', []):
                name = item.get('name')
                artist = item.get('artists', [{}])[0].get('name', '')
                queries.append(f"{name} {artist}")
    except Exception as e:
        print("Spotify error:", e)
    return queries

async def get_youtube_playlist(url):
    """Extract YouTube playlist"""
    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)), timeout=90.0)
        return data.get('entries', []) if 'entries' in data else []
    except Exception as e:
        print("Playlist error:", e)
        return []

@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first!")
        return

    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        await channel.connect()

    async with ctx.typing():
        guild_id = ctx.guild.id
        music_queues.setdefault(guild_id, deque())
        loop_mode.setdefault(guild_id, 'off')

        try:
            # Spotify link
            if "spotify.com" in query:
                queries = get_spotify_track_queries(query)
                if queries:
                    await ctx.send(f"🎧 Spotify playlist detected! Adding {len(queries)} songs...")
                    for q in queries:
                        player = await YTDLSource.from_url(f"ytsearch:{q}", loop=bot.loop)
                        music_queues[guild_id].append(player)
                    await ctx.send(f"✅ Added {len(queries)} songs to queue!")
                else:
                    title = extract_spotify_title(query)
                    if not title:
                        await ctx.send("❌ Couldn't extract Spotify title.")
                        return
                    player = await YTDLSource.from_url(f"ytsearch:{title}", loop=bot.loop)
                    music_queues[guild_id].append(player)
                    await ctx.send(f"✅ Added to queue: **{player.title}**")

            # YouTube playlist
            elif "playlist" in query:
                await ctx.send("📋 YouTube playlist detected! Extracting...")
                entries = await get_youtube_playlist(query)
                for entry in entries:
                    if entry:
                        url = f"https://www.youtube.com/watch?v={entry.get('id')}"
                        player = await YTDLSource.from_url(url, loop=bot.loop)
                        music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added {len(entries)} songs from playlist!")

            # YouTube video or search
            else:
                if not query.startswith("http"):
                    query = f"ytsearch:{query}"
                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            if not ctx.voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

async def play_next(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues or not music_queues[guild_id]:
        await start_idle_timer(ctx)
        return

    mode = loop_mode.get(guild_id, 'off')
    if mode == 'track' and guild_id in now_playing:
        player = now_playing[guild_id]
    else:
        player = music_queues[guild_id].popleft()
        if mode == 'queue':
            music_queues[guild_id].append(player)

    now_playing[guild_id] = player

    def after(error):
        if error:
            print("Playback error:", error)
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    ctx.voice_client.play(player, after=after)
    emoji = " 🔂" if mode == 'track' else " 🔁" if mode == 'queue' else ""
    await ctx.send(f"🎶 Now playing: **{player.title}**{emoji}")

async def start_idle_timer(ctx):
    await asyncio.sleep(120)
    if ctx.voice_client and not ctx.voice_client.is_playing():
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Leaving due to inactivity.")

@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing is playing.")

@bot.command(name='pause')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("❌ Nothing playing.")

@bot.command(name='resume', aliases=['r'])
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("❌ Nothing paused.")

@bot.command(name='stop')
async def stop(ctx):
    guild_id = ctx.guild.id
    music_queues.get(guild_id, deque()).clear()
    loop_mode[guild_id] = 'off'
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue.")
        await start_idle_timer(ctx)

@bot.command(name='leave', aliases=['dc', 'disconnect'])
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Bye!")
    else:
        await ctx.send("❌ I'm not in a voice channel.")

@bot.command(name='queue', aliases=['q'])
async def queue(ctx):
    guild_id = ctx.guild.id
    q = music_queues.get(guild_id, deque())
    if not q:
        if guild_id in now_playing:
            await ctx.send(f"🎵 Now playing: {now_playing[guild_id].title}\nQueue is empty.")
        else:
            await ctx.send("❌ Queue is empty.")
        return

    msg = f"🎵 **Now Playing:** {now_playing[guild_id].title}\n\n"
    for i, player in enumerate(list(q)[:10], 1):
        msg += f"{i}. {player.title}\n"
    await ctx.send(msg)

@bot.command(name='loop', aliases=['l'])
async def loop_cmd(ctx, mode=None):
    guild_id = ctx.guild.id
    cur = loop_mode.get(guild_id, 'off')
    if not mode:
        new = 'track' if cur == 'off' else 'queue' if cur == 'track' else 'off'
    else:
        mode = mode.lower()
        if mode in ['track', 't']: new = 'track'
        elif mode in ['queue', 'q']: new = 'queue'
        else: new = 'off'

    loop_mode[guild_id] = new
    msg = {'off': '❌ Loop off', 'track': '🔂 Loop track', 'queue': '🔁 Loop queue'}
    await ctx.send(msg[new])

@bot.command(name='nowplaying', aliases=['np'])
async def nowplaying(ctx):
    guild_id = ctx.guild.id
    if guild_id in now_playing:
        player = now_playing[guild_id]
        mode = loop_mode.get(guild_id, 'off')
        emoji = " 🔂" if mode == 'track' else " 🔁" if mode == 'queue' else ""
        await ctx.send(f"🎵 Now playing: **{player.title}**{emoji}")
    else:
        await ctx.send("❌ Nothing playing right now.")

@bot.command(name='help', aliases=['commands'])
async def help_cmd(ctx):
    await ctx.send("""
🎶 **Music Bot Commands**
!play <song/link> — Play a song or link
!skip — Skip song
!pause / !resume — Pause or resume
!queue — Show queue
!loop — Toggle loop
!stop — Stop playback
!leave — Disconnect
!nowplaying — Show current song

Supports: YouTube 🔗 | Spotify 🎧 | Search by name 🔍
    """)

# Start Flask (keep alive)
keep_alive()

# Run bot
token = os.getenv('DISCORD_TOKEN')
bot.run(token)
