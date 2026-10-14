"""ccmonitor's icon as image files, drawn in code (#71): the same soft-edged dot the tray
draws (tray.dot_icon, #67), in gui.TRAY_COLORS. build_exe embeds all three colours in
ccmonitor.exe as icon resources (res()) and the window uses the PNG, so no image lives in git.
"""
import struct
import zlib

ICO_SIZES = (16, 20, 24, 32, 48, 256)


def rgba(rgb: str, size: int) -> bytes:
    """Top-down RGBA rows of a filled circle in `rgb` ("#rrggbb") on transparency."""
    r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
    centre, radius = (size - 1) / 2, size / 2 - size / 16  # tray.dot_icon at 16 px: radius 7
    out = bytearray()
    for y in range(size):
        for x in range(size):
            d = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
            alpha = max(0.0, min(1.0, radius + 0.5 - d))  # soft edge
            out += bytes((r, g, b, round(alpha * 255)))
    return bytes(out)


def png(rgb: str, size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels, stride = rgba(rgb, size), size * 4
    raw = b"".join(b"\0" + pixels[y * stride:(y + 1) * stride] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


RT_ICON, RT_GROUP_ICON = 3, 14


def _res_entry(kind: int, name: int, data: bytes, flags: int) -> bytes:
    """One entry of a .res file: the 32-byte header (ordinal type and name), the data,
    padded to a DWORD."""
    header = struct.pack("<IIHHHHIHHII", len(data), 32, 0xFFFF, kind, 0xFFFF, name,
                         0, flags, 0, 0, 0)
    return header + data + b"\0" * (-len(data) % 4)


def res(colours, sizes=ICO_SIZES) -> bytes:
    """A compiled resource file (csc /win32res) holding one icon group per colour, IDs
    1, 2, 3, ... in order: Explorer shows group 1 as the exe's icon. Each image is a PNG
    (Windows Vista and later read PNG icon images at any size)."""
    out = _res_entry(0, 0, b"", 0)  # a .res file starts with an empty entry
    image_id = 1
    for group_id, rgb in enumerate(colours, start=1):
        directory = struct.pack("<HHH", 0, 1, len(sizes))
        for s in sizes:
            data = png(rgb, s)
            out += _res_entry(RT_ICON, image_id, data, 0x1010)  # MOVEABLE | DISCARDABLE
            directory += struct.pack("<BBBBHHIH", s % 256, s % 256, 0, 0, 1, 32, len(data), image_id)
            image_id += 1
        out += _res_entry(RT_GROUP_ICON, group_id, directory, 0x1030)  # | PURE
    return out
