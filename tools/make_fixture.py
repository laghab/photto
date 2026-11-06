#!/usr/bin/env python3
"""Generate a repeatable large-folder fixture for photto.

The fixture is intentionally cheap to create: thousands of tiny, valid PNGs
with a handful of realistic mixed file types. Use it for Desktop tests and for
pushing a known-size folder to the Android emulator.

Usage:
    python3 tools/make_fixture.py /tmp/photto-large
    python3 tools/make_fixture.py /tmp/photto-large --images 5000 --seed 7
    python3 tools/make_fixture.py /tmp/photto-large --json

The generator refuses to write into a non-empty directory unless --force is
given, so a test run cannot silently mix a new fixture with old files.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import struct
import sys
import zipfile
import zlib
from pathlib import Path

DEFAULT_IMAGES = 3000
SUBDIRS = ("Camera", "Screenshots", "WhatsApp", "Downloads", "Pictures/Trips")


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A small valid RGB PNG, built without third-party libraries."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def pdf(text: str) -> bytes:
    """A minimal one-page PDF with a line of text."""
    stream = f"BT /F1 18 Tf 36 340 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def wav(seconds: float = 0.25, rate: int = 8000) -> bytes:
    """A tiny valid mono WAV tone."""
    frames = int(seconds * rate)
    samples = b"".join(struct.pack("<h", int(12000 * ((i * 2) % 8 - 4) / 4)) for i in range(frames))
    data = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(samples), b"WAVE", b"fmt ",
                       16, 1, 1, rate, rate * 2, 2, 16, b"data", len(samples)) + samples
    return data


def write_mixed(root: Path, rng: random.Random) -> list[Path]:
    """Create a small mix of non-image files; returns the paths written."""
    made: list[Path] = []
    downloads = root / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    (downloads / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(rng.randrange(256) for _ in range(4096)))
    (downloads / "clip-two.webm").write_bytes(b"\x1aE\xdf\xa3" + bytes(rng.randrange(256) for _ in range(2048)))
    (downloads / "tone.wav").write_bytes(wav())
    (downloads / "tone.mp3").write_bytes(b"ID3\x04\x00\x00" + bytes(rng.randrange(256) for _ in range(2048)))
    (downloads / "readme.txt").write_text("photto fixture\n" + ("line\n" * 40), encoding="utf-8")
    (downloads / "notes.md").write_text("# Fixture notes\n\nA small text file.\n", encoding="utf-8")
    made += [downloads / "clip.mp4", downloads / "clip-two.webm", downloads / "tone.wav",
             downloads / "tone.mp3", downloads / "readme.txt", downloads / "notes.md"]

    for name in ("document.pdf", "scanned.pdf"):
        (downloads / name).write_bytes(pdf(name.rsplit(".", 1)[0]))
        made.append(downloads / name)

    with zipfile.ZipFile(downloads / "archive.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("hello.txt", "hello from a fixture archive\n")
        archive.writestr("folder/item.txt", "nested\n")
    made.append(downloads / "archive.zip")

    (downloads / "letter.docx").write_bytes(b"PK\x03\x04" + bytes(rng.randrange(256) for _ in range(512)))
    (downloads / "data.csv").write_text("name,size\none,1\ntwo,2\n", encoding="utf-8")
    made += [downloads / "letter.docx", downloads / "data.csv"]

    (root / "empty-folder").mkdir(exist_ok=True)
    return made


def generate(root: Path, images: int = DEFAULT_IMAGES, seed: int = 1, force: bool = False) -> dict:
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        if not force:
            raise SystemExit(f"refusing to fill non-empty directory: {root} (use --force)")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    color_pool = [(230, 120, 60), (60, 180, 160), (90, 120, 220), (220, 190, 70),
                  (170, 90, 200), (100, 200, 110), (240, 100, 120)]
    for index in range(images):
        sub = SUBDIRS[index % len(SUBDIRS)]
        folder = root / sub
        folder.mkdir(parents=True, exist_ok=True)
        color = color_pool[index % len(color_pool)]
        # 32x24 keeps 3,000 images comfortably small while still being a valid PNG.
        name = f"IMG_{index + 1:05d}.png"
        (folder / name).write_bytes(png(32, 24, color))

    mixed = write_mixed(root, rng)
    files = [p for p in root.rglob("*") if p.is_file()]
    total_bytes = sum(p.stat().st_size for p in files)
    return {
        "root": str(root),
        "images": images,
        "mixed": len(mixed),
        "files": len(files),
        "bytes": total_bytes,
        "bytesH": human(total_bytes),
        "seed": seed,
    }


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="destination folder")
    parser.add_argument("--images", type=int, default=DEFAULT_IMAGES, help="number of PNG images (default: 3000)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default: 1)")
    parser.add_argument("--force", action="store_true", help="replace the destination if it is not empty")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    if args.images < 1:
        parser.error("--images must be at least 1")
    summary = generate(args.root, args.images, args.seed, args.force)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"fixture: {summary['files']} files, {summary['bytesH']} ({summary['bytes']} bytes) in {summary['root']}")
        print(f"         {summary['images']} images + {summary['mixed']} mixed files, seed {summary['seed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
