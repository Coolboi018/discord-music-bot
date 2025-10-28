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

# Flask web server to keep bot alive
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

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 🔧 Fix for alias conflict (help)
bot.remove_command('help')  # Disable default help command

# Queue system
music_queues = {}
now_playing = {}
loop_mode = {}  # 'off', 'track', 'queue'
loop_queue_backup = {}  # Store original queue for loop

# yt-dlp options
ytdl_opts = {
    'format': 'bestaudio/best',
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
    'noplaylist': False,  # Allow playlists
}

ffmpeg_opts = {
    'before_options':
    '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, volume=0.5):
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
                None, lambda: ytdl.extract_info(url, download=False)),
                                          timeout=60.0)

            if 'entries' in data:
                if not data['entries']:
                    raise Exception("❌ No results found.")
                data = data['entries'][0]

            return cls(discord.FFmpegPCMAudio(data['url'], **ffmpeg_opts),
                       data=data)

        except asyncio.TimeoutError:
            raise Exception(
                "⏱️ Timeout: YouTube took too long to respond. Try again!")
        except Exception as e:
            raise Exception(f"⚠️ Error: {str(e)[:150]}")


@bot.event
async def on_ready():
    print(f'✅ {bot.user} is online!')


def extract_spotify_title(spotify_url):
    """Try to extract song title from a Spotify link (no API needed)"""
    try:
        response = requests.get(spotify_url, timeout=10)
        html = response.text
        match = re.search(r'<title>(.*?)</title>', html)
        if match:
            title_text = match.group(1)
            clean_title = title_text.replace('| Spotify', '').strip()
            return clean_title
    except Exception:
        return None
    return None


def get_spotify_track_queries(spotify_url):
    """Returns list of "Song Artist" search queries from Spotify"""
    queries = []

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not spotipy or not client_id or not client_secret:
        return queries

    try:
        auth_manager = SpotifyClientCredentials(client_id=client_id,
                                                client_secret=client_secret)
        sp = spotipy.Spotify(auth_manager=auth_manager)

        if "track" in spotify_url and "playlist" not in spotify_url:
            track = sp.track(spotify_url)
            name = track.get('name')
            artist = track.get('artists')[0].get('name') if track.get(
                'artists') else ''
            queries.append(f"{name} {artist}")

        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            items = results.get('items', [])
            while True:
                for item in items:
                    track = item.get('track')
                    if not track:
                        continue
                    name = track.get('name')
                    artist = track.get('artists')[0].get('name') if track.get(
                        'artists') else ''
                    queries.append(f"{name} {artist}")
                if results and results.get('next'):
                    results = sp.next(results)
                    items = results.get('items', [])
                else:
                    break

        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            items = results.get('items', [])
            while True:
                for item in items:
                    name = item.get('name')
                    artist = item.get('artists')[0].get('name') if item.get(
                        'artists') else ''
                    queries.append(f"{name} {artist}")
                if results and results.get('next'):
                    results = sp.next(results)
                    items = results.get('items', [])
                else:
                    break

    except Exception as e:
        print("Spotify API error:", e)
    return queries


async def get_youtube_playlist(url):
    """Extract all videos from a YouTube playlist"""
    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)),
                                      timeout=90.0)

        if 'entries' in data:
            return data['entries']
        return []
    except Exception as e:
        print(f"Playlist extraction error: {e}")
        return []


@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first! 🤡")
        return

    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        await channel.connect()

    async with ctx.typing():
        try:
            guild_id = ctx.guild.id
            if guild_id not in music_queues:
                music_queues[guild_id] = deque()
            if guild_id not in loop_mode:
                loop_mode[guild_id] = 'off'

            if "spotify.com" in query:
                queries = get_spotify_track_queries(query)

                if queries:
                    await ctx.send(
                        f"🎧 Spotify link detected! Adding {len(queries)} tracks to queue..."
                    )
                    added = 0
                    for q in queries:
                        search_q = f"ytsearch:{q} audio"
                        try:
                            player = await YTDLSource.from_url(search_q,
                                                               loop=bot.loop)
                            music_queues[guild_id].append(player)
                            added += 1
                        except Exception as e:
                            print("YT search error for:", q, e)
                            continue

                    if added == 0:
                        await ctx.send(
                            "❌ Couldn't find any tracks on YouTube for that Spotify link."
                        )
                        return
                    await ctx.send(f"✅ Added **{added}** tracks to the queue!")
                else:
                    title = extract_spotify_title(query)
                    if not title:
                        await ctx.send(
                            "❌ Couldn't extract song name from Spotify link. Try giving the song name instead."
                        )
                        return
                    search_q = f"ytsearch:{title} audio"
                    player = await YTDLSource.from_url(search_q, loop=bot.loop)
                    music_queues[guild_id].append(player)
                    await ctx.send(f"✅ Added to queue: **{player.title}**")

            elif "youtube.com/playlist" in query or "youtu.be/playlist" in query or "&list=" in query:
                await ctx.send(
                    "📋 YouTube playlist detected! Extracting tracks...")
                entries = await get_youtube_playlist(query)

                if not entries:
                    await ctx.send("❌ Couldn't extract playlist tracks.")
                    return

                added = 0
                for entry in entries:
                    if entry:
                        try:
                            video_url = entry.get(
                                'url'
                            ) or f"https://www.youtube.com/watch?v={entry.get('id')}"
                            player = await YTDLSource.from_url(video_url,
                                                               loop=bot.loop)
                            music_queues[guild_id].append(player)
                            added += 1
                        except Exception as e:
                            print(f"Error adding playlist track: {e}")
                            continue

                await ctx.send(
                    f"✅ Added **{added}** tracks from playlist to queue!")

            else:
                if not query.startswith('http'):
                    query = f"ytsearch:{query}"

                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            if not ctx.voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()


async def play_next(ctx):
    guild_id = ctx.guild.id

    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        if loop_mode.get(guild_id) == 'track' and guild_id in now_playing:
            player = now_playing[guild_id]
            try:
                player = await YTDLSource.from_url(player.webpage_url
                                                   or player.title,
                                                   loop=bot.loop)
            except:
                pass
        else:
            player = music_queues[guild_id].popleft()
            if loop_mode.get(guild_id) == 'queue':
                music_queues[guild_id].append(player)

        now_playing[guild_id] = player

        def after(error):
            if error:
                print(f"Error: {error}")
            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

        ctx.voice_client.play(player, after=after)

        loop_emoji = ""
        if loop_mode.get(guild_id) == 'track':
            loop_emoji = " 🔂"
        elif loop_mode.get(guild_id) == 'queue':
            loop_emoji = " 🔁"

        await ctx.send(f"🎵 Now playing: **{player.title}**{loop_emoji}")

    else:
        now_playing.pop(guild_id, None)
        await start_idle_timer(ctx)


async def start_idle_timer(ctx):
    await asyncio.sleep(120)
    if ctx.voice_client and not ctx.voice_client.is_playing():
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Leaving due to inactivity. 💢")


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
    if guild_id in loop_mode:
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
        if guild_id in loop_mode:
            loop_mode[guild_id] = 'off'
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Bye! 🥹")
    else:
        await ctx.send("❌ I'm not in a voice channel!")


@bot.command(name='queue', aliases=['q'])
async def queue(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
        if guild_id in now_playing:
            player = now_playing[guild_id]
            await ctx.send(
                f"🎵 **Now Playing:** {player.title}\n\n❌ Queue is empty.")
        else:
            await ctx.send("❌ Queue is empty!")
        return

    queue_text = "🎵 **Music Queue:**\n\n"

    if guild_id in now_playing:
        queue_text += f"**Now Playing:** {now_playing[guild_id].title}\n\n"

    for i, player in enumerate(list(music_queues[guild_id])[:10], 1):
        queue_text += f"{i}. {player.title}\n"

    if len(music_queues[guild_id]) > 10:
        queue_text += f"\n...and {len(music_queues[guild_id]) - 10} more tracks"

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
            await ctx.send(
                "❌ Invalid mode! Use: `!loop track`, `!loop queue`, or `!loop off`"
            )


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
async def commands(ctx):
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
✅ Spotify links, playlists & albums
✅ Search by song name
    """
    await ctx.send(help_text)


# Start Flask server in background
keep_alive()

# Get token and run bot
token = os.getenv('DISCORD_TOKEN')
bot.run(token)