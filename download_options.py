"""Shared download options for the CLI and Telegram command validation."""
import argparse
import math


def add_download_options(parser):
    for flag, kind, default in (
        ("max", int, 0), ("concurrency", int, 4), ("delay", float, 0.4),
        ("timeout", float, 30.0), ("max-width", int, 2048),
        ("split-zip-size-mb", int, 0),
    ):
        parser.add_argument("--" + flag, type=kind, default=default)
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--skip-video-thumbs", action="store_true")
    parser.add_argument("--zip-name", default="")


def validate_download_options(args):
    for field in ("max", "delay", "split_zip_size_mb", "concurrency", "timeout", "max_width"):
        value = getattr(args, field)
        minimum = 0 if field in ("max", "delay", "split_zip_size_mb") else 1
        if not math.isfinite(value) or (value < minimum if field != "timeout" else value <= 0):
            raise ValueError(f"Некорректное значение --{field.replace('_', '-')}: {value}")
    name = args.zip_name
    if name and (name in (".", "..") or any(c in name for c in ("/", "\\", "\x00"))):
        raise ValueError("--zip-name должен содержать только имя файла, без пути")


class _CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def validate_download_flags(tokens):
    parser = _CommandParser(add_help=False, allow_abbrev=False)
    add_download_options(parser)
    validate_download_options(parser.parse_args(list(tokens)))
    return list(tokens)
