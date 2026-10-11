"""Spoken replies: turn a short text into a WhatsApp voice note with Gemini's text-to-speech model.

Gemini TTS speaks 100+ languages and picks the language up from the text itself. It returns a
24 kHz mono WAV; WhatsApp only shows a voice-note bubble for OGG/Opus, so we re-encode it with
PyAV (bundles libopus, no ffmpeg install needed).

VOICE_REPLIES=off turns voice notes off. Needs GEMINI_API_KEY even when AI_PROVIDER=anthropic."""
import asyncio
import base64
import io
import os
import wave

import av

import ai

VOICE_REPLIES = os.getenv("VOICE_REPLIES", "on").lower() not in ("off", "0", "false", "no")
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-lite-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "Kore")


def enabled() -> bool:
    return VOICE_REPLIES and bool(os.getenv("GEMINI_API_KEY"))


def _wav_to_ogg(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    frame = av.AudioFrame(format="s16", layout="mono" if channels == 1 else "stereo", samples=len(pcm) // (2 * channels))
    frame.planes[0].update(pcm)
    frame.sample_rate = rate
    out = io.BytesIO()
    with av.open(out, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48000)  # encoder resamples the 24 kHz input
        stream.layout = "mono"
        stream.bit_rate = 32000  # plenty for speech, keeps a 15s note around 60 KB
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


async def synthesize(text: str) -> bytes:
    """Return OGG/Opus bytes of `text` spoken aloud. Raises on any API or encoding failure."""
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
    return _wav_to_ogg(base64.b64decode(interaction.output_audio.data))
