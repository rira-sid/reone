"""Spoken replies: turn a short text into an MP3 voice note with Gemini's text-to-speech model.

Gemini TTS speaks 100+ languages and picks the language up from the text itself. It returns a
24 kHz mono WAV; WhatsApp doesn't accept WAV, so we re-encode it as MP3 (lameenc, no ffmpeg needed).

VOICE_REPLIES=off turns voice notes off. Needs GEMINI_API_KEY even when AI_PROVIDER=anthropic."""
import asyncio
import base64
import io
import os
import wave

import lameenc

import ai

VOICE_REPLIES = os.getenv("VOICE_REPLIES", "on").lower() not in ("off", "0", "false", "no")
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-lite-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "Kore")


def enabled() -> bool:
    return VOICE_REPLIES and bool(os.getenv("GEMINI_API_KEY"))


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(48)  # plenty for speech, keeps a 15s note around 90 KB
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(channels)
    encoder.set_quality(5)
    return bytes(encoder.encode(pcm) + encoder.flush())


async def synthesize(text: str) -> bytes:
    """Return MP3 bytes of `text` spoken aloud. Raises on any API or encoding failure."""
    try:
        return await _synthesize_once(text)
    except Exception as e:  # free-tier 503/429 usually clears within seconds
        print("TTS RETRY after:", repr(e))
        await asyncio.sleep(3)
        return await _synthesize_once(text)


async def _synthesize_once(text: str) -> bytes:
    client = ai._gemini_client()
    interaction = await asyncio.to_thread(
        client.interactions.create,
        model=TTS_MODEL,
        input=[{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{"type": "speech_metadata", "style": "warm, friendly shop assistant"}],
            }],
        }],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": TTS_VOICE}]},
    )
    return _wav_to_mp3(base64.b64decode(interaction.output_audio.data))
