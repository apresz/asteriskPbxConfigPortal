from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import uuid

from django.conf import settings
from django.core.files import File
from django.utils.text import slugify

from .models import AudioPrompt


SUPPORTED_AUDIO_FORMATS = {
    "wav": "WAV",
    "mp3": "MP3",
    "m4a": "M4A",
}
ASTERISK_SAMPLE_RATE_HZ = 8000
ASTERISK_CHANNELS = 1
ASTERISK_CODEC = "pcm_s16le"


class AudioPromptError(Exception):
    pass


class AudioPromptValidationError(AudioPromptError):
    pass


class AudioPromptConversionError(AudioPromptError):
    pass


def validate_audio_prompt_upload(uploaded_file) -> str:
    filename = getattr(uploaded_file, "name", "")
    source_format = Path(filename).suffix.lower().lstrip(".")
    if source_format not in SUPPORTED_AUDIO_FORMATS:
        raise AudioPromptValidationError("Upload a WAV, MP3, or M4A audio prompt.")
    return source_format


def create_audio_prompt_from_upload(*, location, uploaded_file, name: str = "", runner=None) -> AudioPrompt:
    source_format = validate_audio_prompt_upload(uploaded_file)
    runner = runner or subprocess.run
    prompt_name = _unique_prompt_name(location, name or Path(uploaded_file.name).stem)
    prompt_slug = slugify(prompt_name) or "prompt"
    prompt_token = uuid.uuid4().hex[:12]
    prompt_stem = f"{prompt_slug}-{prompt_token}"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        source_path = temp_path / f"source.{source_format}"
        output_path = temp_path / "converted.wav"
        with source_path.open("wb") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        _convert_to_asterisk_wav(source_path, output_path, runner=runner)

        prompt = AudioPrompt(
            location=location,
            name=prompt_name,
            original_filename=Path(uploaded_file.name).name,
            source_format=source_format,
            content_type=getattr(uploaded_file, "content_type", "") or "",
            size_bytes=getattr(uploaded_file, "size", source_path.stat().st_size) or source_path.stat().st_size,
            converted_format="wav",
            sample_rate_hz=ASTERISK_SAMPLE_RATE_HZ,
            channels=ASTERISK_CHANNELS,
            asterisk_path=_asterisk_path(location.slug, prompt_stem),
        )

        with source_path.open("rb") as original:
            prompt.original_file.save(
                f"audio_prompts/original/{location.slug}/{prompt_stem}.{source_format}",
                File(original),
                save=False,
            )
        with output_path.open("rb") as converted:
            prompt.converted_file.save(
                f"audio_prompts/converted/{location.slug}/{prompt_stem}.wav",
                File(converted),
                save=False,
            )
        prompt.save()
    return prompt


def _convert_to_asterisk_wav(source_path: Path, output_path: Path, *, runner) -> None:
    command = [
        _ffmpeg_binary(),
        "-y",
        "-i",
        str(source_path),
        "-acodec",
        ASTERISK_CODEC,
        "-ar",
        str(ASTERISK_SAMPLE_RATE_HZ),
        "-ac",
        str(ASTERISK_CHANNELS),
        str(output_path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AudioPromptConversionError(
            "Audio conversion requires ffmpeg to be installed and available on PATH."
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        message = "Could not convert audio prompt."
        if detail:
            message = f"{message} ffmpeg reported: {detail[-500:]}"
        raise AudioPromptConversionError(message)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise AudioPromptConversionError("Could not convert audio prompt. ffmpeg did not produce a WAV file.")


def _unique_prompt_name(location, raw_name: str) -> str:
    base_name = " ".join((raw_name or "Audio prompt").replace("_", " ").replace("-", " ").split())
    base_name = base_name[:120] or "Audio prompt"
    candidate = base_name
    suffix = 2
    while AudioPrompt.objects.filter(location=location, name=candidate).exists():
        suffix_text = f" {suffix}"
        candidate = f"{base_name[:120 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _asterisk_path(location_slug: str, prompt_stem: str) -> str:
    sounds_root = getattr(settings, "ASTERISK_SOUNDS_ROOT", "/var/lib/asterisk/sounds").rstrip("/")
    prompt_dir = getattr(settings, "ASTERISK_PROMPT_DIRECTORY", "custom/ivr").strip("/")
    return f"{sounds_root}/{prompt_dir}/{location_slug}/{prompt_stem}.wav"


def _ffmpeg_binary() -> str:
    return getattr(settings, "AUDIO_CONVERSION_FFMPEG", "ffmpeg")
