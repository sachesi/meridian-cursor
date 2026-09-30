#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render svg/*.svg into the Meridian Xcursor themes under dist/.

    build.py            build every theme
    build.py install    build, then copy the themes to ~/.local/share/icons
    build.py preview    write preview.png
"""

import io
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SVG_DIR = ROOT / "svg"
DIST = ROOT / "dist"
ICON_DIR = Path.home() / ".local" / "share" / "icons"

CANVAS = 24
SIZES = (24, 32, 36, 48, 64, 72, 96)

INK = "#1b1c20"
EDGE = "#fff"
EVERY_CURSOR = frozenset(svg.stem for svg in SVG_DIR.glob("*.svg"))
WHITE_IN_CLASSIC = frozenset({"pointer", "grab", "grabbing", "cell"})
GREEN = "#1fa64a"
BLUE = "#1a73e8"
RED = "#e0352b"


@dataclass(frozen=True)
class Theme:
    comment: str
    preview_background: str
    ink: str = INK
    # Cursors where ink and edge trade places.
    swapped: frozenset[str] = frozenset()
    # Ink per cursor file. A badge cursor embeds the arrow from default.svg,
    # so its accent colours the badge only.
    accents: dict[str, str] = field(default_factory=dict)


THEMES = {
    "Meridian": Theme("Graphite cursors with a white edge", "#e9e9ec"),
    "Meridian-Light": Theme("White cursors with a graphite edge", "#202226", swapped=EVERY_CURSOR),
    "Meridian-Classic": Theme(
        "Black cursors with a white edge, white hands with a black edge",
        "#8a8d93",
        ink="#000",
        swapped=WHITE_IN_CLASSIC,
    ),
    "Meridian-Colour": Theme(
        "Black and white cursors with coloured badges and signs",
        "#d6cfc0",
        ink="#000",
        swapped=WHITE_IN_CLASSIC,
        accents={
            "copy": GREEN,
            "alias": BLUE,
            "help": BLUE,
            "progress": BLUE,
            "wait": BLUE,
            "no-drop": RED,
            "not-allowed": RED,
        },
    ),
}

XCURSOR_IMAGE = 0xFFFD0002
SPIN_MARKER = "rotate(0 "


def write_sources(directory: Path, theme: Theme) -> None:
    for svg in SVG_DIR.glob("*.svg"):
        source = svg.read_text()
        if svg.stem in theme.swapped:
            source = source.replace(EDGE, "\0").replace(INK, EDGE).replace("\0", INK)
        ink = theme.accents.get(svg.stem, theme.ink)
        (directory / svg.name).write_text(source.replace(INK, ink))


def render(svg: Path, pixels: int) -> Image.Image:
    png = subprocess.run(
        ["rsvg-convert", "--width", str(pixels), "--height", str(pixels), str(svg)],
        capture_output=True,
        check=True,
    ).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def render_frames(svg: Path, pixels: int, frames: int) -> list[Image.Image]:
    if frames == 1:
        return [render(svg, pixels)]
    source = svg.read_text()
    if SPIN_MARKER not in source:
        raise ValueError(f"{svg.name}: animated cursor has no {SPIN_MARKER!r} transform")
    images = []
    # Frames are written next to the sources because librsvg only resolves
    # <image href> relative to the file being rendered.
    with tempfile.NamedTemporaryFile("w", dir=svg.parent, suffix=".svg") as frame:
        for index in range(frames):
            angle = 360 * index / frames
            frame.seek(0)
            frame.truncate()
            frame.write(source.replace(SPIN_MARKER, f"rotate({angle:g} "))
            frame.flush()
            images.append(render(Path(frame.name), pixels))
    return images


def xcursor(images: list[tuple[int, Image.Image, int, int, int]]) -> bytes:
    toc = b""
    chunks = b""
    position = 16 + 12 * len(images)
    for nominal, image, xhot, yhot, delay in images:
        chunk = struct.pack(
            "<9I", 36, XCURSOR_IMAGE, nominal, 1, image.width, image.height, xhot, yhot, delay
        ) + image.convert("RGBa").tobytes("raw", "BGRa")
        toc += struct.pack("<3I", XCURSOR_IMAGE, nominal, position)
        position += len(chunk)
        chunks += chunk
    return struct.pack("<4s3I", b"Xcur", 16, 0x10000, len(images)) + toc + chunks


def write_cursor(cursor_dir: Path, sources: Path, name: str, spec: dict) -> None:
    images = []
    for size in SIZES:
        xhot, yhot = (int(h * size / CANVAS) for h in spec["hotspot"])
        for image in render_frames(sources / f"{name}.svg", size, spec.get("frames", 1)):
            images.append((size, image, xhot, yhot, spec.get("delay", 0)))
    (cursor_dir / name).write_bytes(xcursor(images))
    for alias in spec.get("aliases", []):
        (cursor_dir / alias).symlink_to(name)


def build() -> None:
    cursors = tomllib.loads((ROOT / "cursors.toml").read_text())
    for name, theme in THEMES.items():
        theme_dir = DIST / name
        shutil.rmtree(theme_dir, ignore_errors=True)
        cursor_dir = theme_dir / "cursors"
        cursor_dir.mkdir(parents=True)
        (theme_dir / "index.theme").write_text(
            f"[Icon Theme]\nName={name}\nComment={theme.comment}\n"
        )
        with tempfile.TemporaryDirectory() as sources, ThreadPoolExecutor() as pool:
            write_sources(Path(sources), theme)
            jobs = [
                pool.submit(write_cursor, cursor_dir, Path(sources), cursor, spec)
                for cursor, spec in cursors.items()
            ]
            for job in jobs:
                job.result()
        print(theme_dir)


def install() -> None:
    build()
    for theme in THEMES:
        target = ICON_DIR / theme
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(DIST / theme, target, symlinks=True)
        print(target)


def preview() -> None:
    names = list(tomllib.loads((ROOT / "cursors.toml").read_text()))
    tile = 48
    columns = 12
    rows = -(-len(names) // columns)
    sheet = Image.new("RGBA", (columns * tile, len(THEMES) * rows * tile))
    for band, theme in enumerate(THEMES.values()):
        top = band * rows * tile
        sheet.paste(theme.preview_background, (0, top, columns * tile, top + rows * tile))
        with tempfile.TemporaryDirectory() as sources:
            write_sources(Path(sources), theme)
            for index, name in enumerate(names):
                image = render(Path(sources) / f"{name}.svg", tile)
                sheet.alpha_composite(image, (index % columns * tile, top + index // columns * tile))
    sheet.save(ROOT / "preview.png")


COMMANDS = {"build": build, "install": install, "preview": preview}

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "build"
    if command not in COMMANDS:
        raise SystemExit(f"unknown command {command!r}, expected one of {', '.join(COMMANDS)}")
    COMMANDS[command]()
