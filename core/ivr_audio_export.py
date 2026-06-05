from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

DEFAULT_ASTERISK_SOUNDS_ROOT = "/var/lib/asterisk/sounds"
ASTERISK_SOUNDS_BUNDLE_PREFIX = "asterisk/sounds"
AUDIO_WAV_CONTENT_TYPE = "audio/wav"


@dataclass(frozen=True)
class AudioPromptPayloadSpec:
    prompt_name: str
    source_path: Path
    archive_path: str
    content_type: str = AUDIO_WAV_CONTENT_TYPE


class AudioPromptPayloadError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("IVR prompt audio files are not exportable.")


def audio_prompt_archive_files(
    prompts: Iterable[Any],
    *,
    sounds_root: str = DEFAULT_ASTERISK_SOUNDS_ROOT,
    bundle_prefix: str = ASTERISK_SOUNDS_BUNDLE_PREFIX,
) -> list[tuple[str, bytes, str]]:
    specs, errors = audio_prompt_payload_specs(
        prompts,
        sounds_root=sounds_root,
        bundle_prefix=bundle_prefix,
    )
    if errors:
        raise AudioPromptPayloadError(errors)
    return [
        (spec.archive_path, spec.source_path.read_bytes(), spec.content_type)
        for spec in specs
    ]


def audio_prompt_payload_specs(
    prompts: Iterable[Any],
    *,
    sounds_root: str = DEFAULT_ASTERISK_SOUNDS_ROOT,
    bundle_prefix: str = ASTERISK_SOUNDS_BUNDLE_PREFIX,
) -> tuple[list[AudioPromptPayloadSpec], list[dict[str, Any]]]:
    specs: list[AudioPromptPayloadSpec] = []
    errors: list[dict[str, Any]] = []
    seen_archive_paths: set[str] = set()

    for prompt in prompts:
        source_path = _converted_file_path(prompt)
        if not source_path or not source_path.is_file():
            errors.append(_missing_prompt_file_error(prompt, source_path))
            continue

        try:
            archive_path = asterisk_sound_archive_path(
                _prompt_asterisk_path(prompt),
                sounds_root=sounds_root,
                bundle_prefix=bundle_prefix,
            )
        except ValueError as exc:
            errors.append(_invalid_prompt_path_error(prompt, str(exc)))
            continue

        if archive_path in seen_archive_paths:
            continue
        seen_archive_paths.add(archive_path)
        specs.append(
            AudioPromptPayloadSpec(
                prompt_name=_prompt_name(prompt),
                source_path=source_path,
                archive_path=archive_path,
            )
        )

    return specs, errors


def audio_prompt_payload_errors(
    prompts: Iterable[Any],
    *,
    sounds_root: str = DEFAULT_ASTERISK_SOUNDS_ROOT,
    bundle_prefix: str = ASTERISK_SOUNDS_BUNDLE_PREFIX,
) -> list[dict[str, Any]]:
    _specs, errors = audio_prompt_payload_specs(
        prompts,
        sounds_root=sounds_root,
        bundle_prefix=bundle_prefix,
    )
    return errors


def asterisk_sound_archive_path(
    asterisk_path: str,
    *,
    sounds_root: str = DEFAULT_ASTERISK_SOUNDS_ROOT,
    bundle_prefix: str = ASTERISK_SOUNDS_BUNDLE_PREFIX,
) -> str:
    root = _clean_posix_path(sounds_root)
    prompt_path = _clean_posix_path(asterisk_path)
    root_prefix = f"{root}/"
    if not prompt_path.startswith(root_prefix):
        raise ValueError(f"prompt path must be under {root}")

    relative = prompt_path[len(root_prefix) :]
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise ValueError("prompt path must be a safe path below the Asterisk sounds root")
    return f"{bundle_prefix.strip('/')}/{relative_path.as_posix()}"


def _missing_prompt_file_error(prompt: Any, source_path: Path | None) -> dict[str, Any]:
    return {
        "code": "missing_ivr_prompt_file",
        "ivr": _prompt_ivr_name(prompt),
        "prompt": _prompt_name(prompt),
        "converted_file": _prompt_converted_file_name(prompt),
        "converted_file_path": str(source_path or ""),
        "message": f"IVR {_prompt_ivr_name(prompt)} references a converted prompt file that is missing.",
    }


def _invalid_prompt_path_error(prompt: Any, detail: str) -> dict[str, Any]:
    return {
        "code": "invalid_ivr_prompt_path",
        "ivr": _prompt_ivr_name(prompt),
        "prompt": _prompt_name(prompt),
        "asterisk_path": _prompt_asterisk_path(prompt),
        "message": f"IVR {_prompt_ivr_name(prompt)} prompt path is not exportable: {detail}.",
    }


def _converted_file_path(prompt: Any) -> Path | None:
    raw_path = _value(prompt, "converted_file_path")
    if not raw_path:
        return None
    return Path(str(raw_path))


def _prompt_name(prompt: Any) -> str:
    return str(_value(prompt, "prompt_name") or _value(prompt, "name") or "unknown prompt")


def _prompt_ivr_name(prompt: Any) -> str:
    return str(_value(prompt, "ivr") or _value(prompt, "ivr_name") or "unknown IVR")


def _prompt_converted_file_name(prompt: Any) -> str:
    return str(_value(prompt, "converted_file") or _value(prompt, "converted_file_name") or "")


def _prompt_asterisk_path(prompt: Any) -> str:
    return str(_value(prompt, "asterisk_path") or "")


def _clean_posix_path(path: str) -> str:
    cleaned = PurePosixPath(str(path or "").strip()).as_posix()
    return cleaned.rstrip("/")


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)
