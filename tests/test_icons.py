import struct
import unittest
import zlib

from ccmonitor import icons

RUNNING, DISABLED, ERROR = "#2da44e", "#8c959f", "#cf222e"


def entries(res: bytes):
    """(type, name, data) of each entry in a .res file with ordinal types and names."""
    out, pos = [], 0
    while pos < len(res):
        size, header, _, kind, _, name = struct.unpack_from("<IIHHHH", res, pos)
        out.append((kind, name, res[pos + header:pos + header + size]))
        pos += header + size + (-size % 4)
    return out


def centre(png: bytes) -> tuple[int, int, int, int]:
    width = struct.unpack_from(">I", png, 16)[0]
    idat = png.index(b"IDAT")
    size = struct.unpack_from(">I", png, idat - 4)[0]
    raw = zlib.decompress(png[idat + 4:idat + 4 + size])
    row = width // 2 * (1 + 4 * width)
    x = 1 + width // 2 * 4
    return tuple(raw[row + x:row + x + 4])


class ExeIconsTest(unittest.TestCase):
    """build_exe embeds the three state colours (#63, #71) as icon groups 1, 2, 3."""

    def test_one_icon_group_per_colour_in_order(self):
        found = entries(icons.res([RUNNING, DISABLED, ERROR]))
        images = {name: data for kind, name, data in found if kind == icons.RT_ICON}
        groups = {name: data for kind, name, data in found if kind == icons.RT_GROUP_ICON}
        self.assertEqual(sorted(groups), [1, 2, 3])
        for group, rgb in zip((1, 2, 3), (RUNNING, DISABLED, ERROR)):
            with self.subTest(group=group):
                directory = groups[group]
                _, kind, count = struct.unpack_from("<HHH", directory)
                self.assertEqual((kind, count), (1, len(icons.ICO_SIZES)))
                for i, size in enumerate(icons.ICO_SIZES):
                    w, *_, length, image = struct.unpack_from("<BBBBHHIH", directory, 6 + 14 * i)
                    self.assertEqual(w, size % 256)
                    self.assertEqual(len(images[image]), length)
                    r, g, b, a = centre(images[image])
                    self.assertEqual(f"#{r:02x}{g:02x}{b:02x}", rgb)
                    self.assertEqual(a, 255)


if __name__ == "__main__":
    unittest.main()
