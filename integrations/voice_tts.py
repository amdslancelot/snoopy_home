import asyncio
import os
import tempfile
from typing import Optional

import discord

from config import settings


async def speak_in_channel(
    bot: discord.Client,
    guild: Optional[discord.Guild],
    text: str,
    target_user_id: Optional[int],
) -> bool:
    try:
        import edge_tts
    except ImportError:
        print("[voice] edge-tts is not installed — run: pip install edge-tts")
        return False

    if not guild:
        return False

    # Prefer the target user's current voice channel; fall back to configured default.
    voice_channel: Optional[discord.VoiceChannel] = None
    if target_user_id:
        member = guild.get_member(target_user_id)
        if member and member.voice and member.voice.channel:
            voice_channel = member.voice.channel

    if voice_channel is None and settings.default_voice_channel_id:
        ch = bot.get_channel(settings.default_voice_channel_id)
        if isinstance(ch, discord.VoiceChannel):
            voice_channel = ch

    if voice_channel is None:
        print(f"[voice] no voice channel found in guild {guild.id}")
        return False

    # Synthesise speech to a temporary MP3 file.
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name

    try:
        communicate = edge_tts.Communicate(text, "en-US-JennyNeural")
        await communicate.save(tmp_path)
    except Exception as exc:
        print(f"[voice] TTS synthesis failed: {exc}")
        os.unlink(tmp_path)
        return False

    vc: Optional[discord.VoiceClient] = None
    try:
        vc = await voice_channel.connect(timeout=10.0)
        done = asyncio.Event()

        def _after(error):
            if error:
                print(f"[voice] playback error: {error}")
            done.set()

        vc.play(discord.FFmpegPCMAudio(tmp_path), after=_after)
        await done.wait()
        return True
    except Exception as exc:
        print(f"[voice] playback failed: {exc}")
        return False
    finally:
        if vc and vc.is_connected():
            await vc.disconnect()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
