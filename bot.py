import discord
from discord.ext import commands
import yt_dlp
import asyncio
from collections import deque
import os
import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# Bot setup with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Remove default help command
bot.remove_command('help')

# Queue and state management
music_queues = {}
now_playing = {}
loop_mode = {}  # 'off', 'track', 'queue'
idle_timers = {}

# Enhanced yt-dlp options for better audio quality
ytdl_opts = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
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
    'noplaylist': False,
    'age_limit': None,
    'geo_bypass': True,
}

# Enhanced FFmpeg options for better audio quality
ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -filter:a "volume=0.8"'
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

# Initialize Spotify client
def get_spotify_client():
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return None
    
    try:
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        return spotipy.Spotify(auth_manager=auth_manager)
    except Exception as e:
        print(f"Spotify authentication error: {e}")
        return None


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.8):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.webpage_url = data.get('webpage_url')
        self.duration = data.get('duration', 0)

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()

        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False)),
                timeout=60.0
            )

            if 'entries' in data:
                if not data['entries']:
                    raise Exception("❌ No results found.")
                data = data['entries'][0]

            return cls(
                discord.FFmpegPCMAudio(data['url'], **ffmpeg_opts),
                data=data
            )

        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout: YouTube took too long to respond. Try again!")
        except Exception as e:
            raise Exception(f"⚠️ Error: {str(e)[:200]}")


def get_spotify_tracks(spotify_url):
    """Extract track information from Spotify links"""
    sp = get_spotify_client()
    if not sp:
        return []

    tracks = []
    
    try:
        if "track" in spotify_url:
            track = sp.track(spotify_url)
            name = track.get('name')
            artists = ', '.join([artist['name'] for artist in track.get('artists', [])])
            tracks.append(f"{name} {artists}")

        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            while results:
                for item in results.get('items', []):
                    track = item.get('track')
                    if track:
                        name = track.get('name')
                        artists = ', '.join([artist['name'] for artist in track.get('artists', [])])
                        tracks.append(f"{name} {artists}")
                
                results = sp.next(results) if results.get('next') else None

        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            album_info = sp.album(spotify_url)
            album_artists = ', '.join([artist['name'] for artist in album_info.get('artists', [])])
            
            while results:
                for item in results.get('items', []):
                    name = item.get('name')
                    tracks.append(f"{name} {album_artists}")
                
                results = sp.next(results) if results.get('next') else None

    except Exception as e:
        print(f"Spotify API error: {e}")
    
    return tracks


async def get_youtube_playlist(url):
    """Extract all videos from a YouTube playlist"""
    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False)),
            timeout=120.0
        )

        if 'entries' in data:
            return [entry for entry in data['entries'] if entry]
        return []
    except Exception as e:
        print(f"Playlist extraction error: {e}")
        return []


async def cancel_idle_timer(guild_id):
    """Cancel existing idle timer"""
    if guild_id in idle_timers:
        idle_timers[guild_id].cancel()
        idle_timers.pop(guild_id, None)


async def start_idle_timer(ctx):
    """Start idle timer - disconnect after 3 minutes of inactivity"""
    guild_id = ctx.guild.id
    await cancel_idle_timer(guild_id)
    
    async def idle_disconnect():
        await asyncio.sleep(180)  # 3 minutes
        if ctx.voice_client and not ctx.voice_client.is_playing():
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Left voice channel due to inactivity.")
    
    idle_timers[guild_id] = asyncio.create_task(idle_disconnect())


@bot.event
async def on_ready():
    print(f'✅ {bot.user} is online and ready!')
    print(f'Connected to {len(bot.guilds)} server(s)')


@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query):
    """Play a song from YouTube, Spotify, or search query"""
    if not ctx.author.voice:
        await ctx.send("❌ You need to join a voice channel first!")
        return

    channel = ctx.author.voice.channel
    
    # Connect to voice channel if not already connected
    if not ctx.voice_client:
        await channel.connect()
    elif ctx.voice_client.channel != channel:
        await ctx.voice_client.move_to(channel)

    await cancel_idle_timer(ctx.guild.id)

    async with ctx.typing():
        try:
            guild_id = ctx.guild.id
            
            # Initialize queue structures
            if guild_id not in music_queues:
                music_queues[guild_id] = deque()
            if guild_id not in loop_mode:
                loop_mode[guild_id] = 'off'

            # Handle Spotify links
            if "spotify.com" in query:
                tracks = get_spotify_tracks(query)
                
                if not tracks:
                    await ctx.send("❌ Couldn't extract tracks from Spotify link. Make sure your Spotify API credentials are set!")
                    return
                
                if len(tracks) > 1:
                    await ctx.send(f"🎧 Loading {len(tracks)} tracks from Spotify...")
                
                added = 0
                for track_query in tracks:
                    try:
                        search_query = f"ytsearch:{track_query}"
                        player = await YTDLSource.from_url(search_query, loop=bot.loop)
                        music_queues[guild_id].append(player)
                        added += 1
                    except Exception as e:
                        print(f"Error adding track '{track_query}': {e}")
                        continue
                
                if added == 0:
                    await ctx.send("❌ Couldn't find any matching tracks on YouTube.")
                    return
                
                await ctx.send(f"✅ Added **{added}** track(s) to queue!")

            # Handle YouTube playlists
            elif "youtube.com/playlist" in query or "youtu.be/playlist" in query or "&list=" in query:
                await ctx.send("📋 Loading YouTube playlist...")
                entries = await get_youtube_playlist(query)

                if not entries:
                    await ctx.send("❌ Couldn't extract playlist tracks.")
                    return

                added = 0
                for entry in entries[:50]:  # Limit to 50 tracks for performance
                    try:
                        video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                        player = await YTDLSource.from_url(video_url, loop=bot.loop)
                        music_queues[guild_id].append(player)
                        added += 1
                    except Exception as e:
                        print(f"Error adding playlist track: {e}")
                        continue

                await ctx.send(f"✅ Added **{added}** track(s) from playlist!")

            # Handle single YouTube link or search query
            else:
                if not query.startswith('http'):
                    query = f"ytsearch:{query}"

                player = await YTDLSource.from_url(query, loop=bot.loop)
                music_queues[guild_id].append(player)
                await ctx.send(f"✅ Added to queue: **{player.title}**")

            # Start playing if nothing is currently playing
            if not ctx.voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")


async def play_next(ctx):
    """Play the next song in queue"""
    guild_id = ctx.guild.id

    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        # Handle track loop
        if loop_mode.get(guild_id) == 'track' and guild_id in now_playing:
            player = now_playing[guild_id]
            try:
                player = await YTDLSource.from_url(
                    player.webpage_url or f"ytsearch:{player.title}",
                    loop=bot.loop
                )
            except Exception as e:
                print(f"Error reloading track: {e}")
                player = music_queues[guild_id].popleft()
        else:
            player = music_queues[guild_id].popleft()
            
            # Handle queue loop
            if loop_mode.get(guild_id) == 'queue':
                music_queues[guild_id].append(player)

        now_playing[guild_id] = player

        def after(error):
            if error:
                print(f"Playback error: {error}")
            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

        ctx.voice_client.play(player, after=after)

        # Show loop status
        loop_emoji = ""
        if loop_mode.get(guild_id) == 'track':
            loop_emoji = " 🔂"
        elif loop_mode.get(guild_id) == 'queue':
            loop_emoji = " 🔁"

        await ctx.send(f"🎵 Now playing: **{player.title}**{loop_emoji}")

    else:
        now_playing.pop(guild_id, None)
        await start_idle_timer(ctx)


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
    """Stop playing and clear the queue"""
    guild_id = ctx.guild.id
    
    if guild_id in music_queues:
        music_queues[guild_id].clear()
    if guild_id in loop_mode:
        loop_mode[guild_id] = 'off'
    
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue!")
        await start_idle_timer(ctx)
    else:
        await ctx.send("❌ Not connected to a voice channel.")


@bot.command(name='leave', aliases=['disconnect', 'dc'])
async def leave(ctx):
    """Disconnect the bot from voice channel"""
    if ctx.voice_client:
        guild_id = ctx.guild.id
        
        # Clear queue and reset state
        if guild_id in music_queues:
            music_queues[guild_id].clear()
        if guild_id in loop_mode:
            loop_mode[guild_id] = 'off'
        
        await cancel_idle_timer(guild_id)
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Disconnected!")
    else:
        await ctx.send("❌ I'm not in a voice channel!")


@bot.command(name='queue', aliases=['q'])
async def queue(ctx):
    """Show the current queue"""
    guild_id = ctx.guild.id
    
    if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
        if guild_id in now_playing:
            player = now_playing[guild_id]
            await ctx.send(f"🎵 **Now Playing:** {player.title}\n\n📭 Queue is empty.")
        else:
            await ctx.send("📭 Queue is empty!")
        return

    queue_text = "🎵 **Music Queue**\n\n"

    if guild_id in now_playing:
        queue_text += f"**🎧 Now Playing:** {now_playing[guild_id].title}\n\n"
        queue_text += "**📋 Up Next:**\n"

    for i, player in enumerate(list(music_queues[guild_id])[:15], 1):
        queue_text += f"`{i}.` {player.title}\n"

    if len(music_queues[guild_id]) > 15:
        queue_text += f"\n*...and {len(music_queues[guild_id]) - 15} more track(s)*"

    if loop_mode.get(guild_id) == 'track':
        queue_text += "\n\n🔂 **Loop:** Current Track"
    elif loop_mode.get(guild_id) == 'queue':
        queue_text += "\n\n🔁 **Loop:** Entire Queue"

    await ctx.send(queue_text)


@bot.command(name='loop', aliases=['l'])
async def loop_command(ctx, mode: str = None):
    """Toggle loop mode or set specific mode (track/queue/off)"""
    guild_id = ctx.guild.id

    if guild_id not in loop_mode:
        loop_mode[guild_id] = 'off'

    if mode is None:
        # Cycle through modes
        current = loop_mode[guild_id]
        if current == 'off':
            loop_mode[guild_id] = 'track'
            await ctx.send("🔂 **Loop Mode:** Current track")
        elif current == 'track':
            loop_mode[guild_id] = 'queue'
            await ctx.send("🔁 **Loop Mode:** Entire queue")
        else:
            loop_mode[guild_id] = 'off'
            await ctx.send("❌ **Loop Mode:** Disabled")
    else:
        mode = mode.lower()
        if mode in ['track', 't', 'song', 'single', '1']:
            loop_mode[guild_id] = 'track'
            await ctx.send("🔂 **Loop Mode:** Current track")
        elif mode in ['queue', 'q', 'all', 'playlist']:
            loop_mode[guild_id] = 'queue'
            await ctx.send("🔁 **Loop Mode:** Entire queue")
        elif mode in ['off', 'stop', 'disable', '0']:
            loop_mode[guild_id] = 'off'
            await ctx.send("❌ **Loop Mode:** Disabled")
        else:
            await ctx.send("❌ Invalid mode! Use: `!loop track`, `!loop queue`, or `!loop off`")


@bot.command(name='nowplaying', aliases=['np', 'current'])
async def nowplaying(ctx):
    """Show currently playing song"""
    guild_id = ctx.guild.id
    
    if guild_id in now_playing:
        player = now_playing[guild_id]
        loop_status = ""
        
        if loop_mode.get(guild_id) == 'track':
            loop_status = " 🔂"
        elif loop_mode.get(guild_id) == 'queue':
            loop_status = " 🔁"
        
        duration = f" ({player.duration // 60}:{player.duration % 60:02d})" if player.duration else ""
        await ctx.send(f"🎵 **Now Playing:** {player.title}{duration}{loop_status}")
    else:
        await ctx.send("❌ Nothing is playing right now!")


@bot.command(name='clear', aliases=['clearqueue'])
async def clear(ctx):
    """Clear the queue without stopping current song"""
    guild_id = ctx.guild.id
    
    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        count = len(music_queues[guild_id])
        music_queues[guild_id].clear()
        await ctx.send(f"🗑️ Cleared **{count}** track(s) from queue!")
    else:
        await ctx.send("📭 Queue is already empty!")


@bot.command(name='help', aliases=['commands', 'h'])
async def help_command(ctx):
    """Show all available commands"""
    help_text = """
🎵 **Discord Music Bot Commands**

**Playback:**
`!play <song/link>` or `!p` - Play a song
`!pause` - Pause playback
`!resume` or `!r` - Resume playback
`!skip` or `!s` - Skip current song
`!stop` - Stop and clear queue
`!leave` or `!dc` - Disconnect bot

**Queue:**
`!queue` or `!q` - Show queue
`!clear` - Clear queue
`!nowplaying` or `!np` - Show current song

**Loop:**
`!loop` or `!l` - Toggle loop mode
`!loop track` - Loop current track
`!loop queue` - Loop entire queue
`!loop off` - Disable loop

**Supported Sources:**
✅ YouTube links & playlists
✅ Spotify tracks, playlists & albums
✅ Direct song name search

**Features:**
🎧 High-quality audio (192kbps)
🔄 Loop individual tracks or entire queue
⏱️ Auto-disconnect after 3 min inactivity
    """
    await ctx.send(help_text)


# Run the bot
if __name__ == "__main__":
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("❌ Error: DISCORD_TOKEN not found in environment variables!")
    else:
        bot.run(token)
