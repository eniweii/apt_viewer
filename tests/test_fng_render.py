"""Tests for the resolution scale of FNG/fng_render.py and the size table of FNG/fng_player.py (no game files).
Run: python tests/test_fng_render.py   (the player part needs tkinter and is skipped without it)"""
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("FNG", "common"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import fng_render as R  # noqa: E402


def obj(guid, pos, size, kind=R.TYPE_SIMPLE, parent=None, color=(255, 255, 255, 255)):
    return SimpleNamespace(guid=guid, parent=parent, type=kind, flags=0, scripts=[], resource=None, text={}, name="o%d" % guid,
                           name_hash=guid, props={"Position": pos, "Size": size, "Color": color})


def scene(*objects):
    return R.Scene(SimpleNamespace(objects=list(objects), name="T"))


def area(img):
    return sum(img.getchannel("R").histogram()[201:])


class Scale(unittest.TestCase):
    def test_default_scale_is_height_over_480(self):
        sc = scene(obj(1, (0, 0, 0), (200, 100, 1)))
        sc.render(640, 480)
        self.assertEqual(sc._scale, 1.0)
        sc.render(1280, 720)
        self.assertEqual(sc._scale, 1.5)
        sc.render(1920, 1080)
        self.assertEqual(sc._scale, 2.25)

    def test_polygon_scales_about_the_center(self):
        sc = scene(obj(1, (50, -20, 0), (200, 100, 1)))
        sc.render(640, 480)
        base = sc.polygon_of(next(iter(sc.nodes.values())))
        for w, h in ((960, 720), (1440, 1080), (1920, 1440)):
            sc.render(w, h)
            poly = sc.polygon_of(next(iter(sc.nodes.values())))
            k = h / 480
            for (bx, by), (x, y) in zip(base, poly):
                self.assertAlmostEqual((bx - 320) * k + w / 2, x, places=6)
                self.assertAlmostEqual((by - 240) * k + h / 2, y, places=6)

    def test_16_9_sizes_are_standard(self):
        sc = scene(obj(1, (0, 0, 0), (100, 100, 1)))
        for w, h in ((854, 480), (1280, 720), (1920, 1080), (2560, 1440)):
            img = sc.render(w, h)
            self.assertEqual(img.size, (w, h))

    def test_picture_is_not_reused_between_resolutions(self):
        sc = scene(obj(1, (0, 0, 0), (200, 100, 1)))
        a1 = area(sc.render(640, 480))
        a2 = area(sc.render(960, 720))
        a3 = area(sc.render(640, 480))
        self.assertAlmostEqual(a2 / a1, 2.25, delta=0.05)
        self.assertEqual(a1, a3)

    def test_hit_testing_follows_the_logical_position(self):
        sc = scene(obj(1, (100, 0, 0), (60, 60, 1)))
        for w, h in ((640, 480), (1280, 720), (1920, 1080)):
            sc.render(w, h)
            k = h / 480
            x, y = w / 2 + 100 * k, h / 2
            self.assertEqual(len(sc.objects_at(x, y)), 1)
            self.assertEqual(sc.objects_at(w / 2 - 100 * k, y), [])

    def test_fallback_bounds_scale(self):
        group = obj(1, (40, 30, 0), (1, 1, 1), kind=R.TYPE_GROUP)
        sc = scene(group)
        sc.render(1280, 720)
        x0, y0, x1, y1 = sc.node_bounds(sc.nodes[1])
        self.assertAlmostEqual(x0, 640 + 40 * 1.5)
        self.assertAlmostEqual(y0, 360 + 30 * 1.5)


class PlayerSizes(unittest.TestCase):
    def test_table(self):
        try:
            import fng_player as P
        except Exception as e:                     # no tkinter / display
            self.skipTest(str(e))
        for aspect, ratio in (("4:3", 4 / 3), ("16:9", 16 / 9)):
            for res in P.RESOLUTIONS:
                w, h = P.OUTPUT_SIZES[(aspect, res)]
                self.assertEqual(h, int(res[:-1]))
                self.assertLessEqual(abs(w / h - ratio), 0.002)
        self.assertEqual(P.OUTPUT_SIZES[("16:9", "720p")], (1280, 720))
        self.assertEqual(P.OUTPUT_SIZES[("16:9", "1080p")], (1920, 1080))


if __name__ == "__main__":
    unittest.main()
