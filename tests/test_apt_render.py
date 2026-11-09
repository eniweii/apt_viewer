"""Tests for the Resolution (output size) of Apt/apt_render.py and apt_render_skia.py, using oscar.big when it is
next to this file or given in APT_TEST_BIG; skipped without it. Run: python tests/test_apt_render.py"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("Apt", "common"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import apt_movie  # noqa: E402
import apt_render  # noqa: E402

BIG = os.environ.get("APT_TEST_BIG") or next((p for p in (os.path.join(os.path.dirname(__file__), "oscar.big"),
                                                           "/mnt/user-data/uploads/oscar.big") if os.path.exists(p)), None)


class NoResources:
    """Every image and font is missing, so only the geometry is drawn."""
    def image(self, movie, i):
        return {"status": "missing", "png": "", "texture": "", "image": None, "pack": "", "note": ""}

    def font_for(self, name):
        return None, None


class Fake:
    def __init__(self, w, h):
        self.width, self.height = w, h


class Geometry(unittest.TestCase):
    def test_output_geometry(self):
        m = Fake(800, 600)
        self.assertEqual(apt_render.output_geometry(m, 0.75), (600, 450, 0.75, 0.75))
        self.assertEqual(apt_render.output_geometry(m, 1.0, (1280, 720)), (1280, 720, 1.6, 1.2))
        self.assertEqual(apt_render.output_geometry(m, 1.0, (400, 300)), (400, 300, 0.5, 0.5))


@unittest.skipUnless(BIG, "oscar.big not found")
class Render(unittest.TestCase):
    def backends(self):
        out = [apt_render.Renderer(NoResources())]
        try:
            import apt_render_skia
            out.append(apt_render_skia.SkiaRenderer(NoResources()))
        except ImportError:
            pass
        return out

    def test_sizes_and_hits(self):
        movie = apt_movie.load(BIG)
        for r in self.backends():
            img0, hits0, _ = r.render(movie, 0, 1.0, None)
            self.assertEqual(img0.size, (movie.width, movie.height), r)
            for size in ((1280, 720), (640, 480), (1920, 1080)):
                img, hits, _ = r.render(movie, 0, 1.0, size)
                self.assertEqual(img.size, size, (r, size))
                self.assertEqual(len(hits), len(hits0), (r, size))
                sx, sy = size[0] / movie.width, size[1] / movie.height
                for (b0, _i0), (b1, _i1) in zip(hits0, hits):
                    # centers: markers have a fixed pixel margin, so the edges of a small box do not scale exactly
                    self.assertAlmostEqual((b0[0] + b0[2]) / 2 * sx, (b1[0] + b1[2]) / 2, delta=2)
                    self.assertAlmostEqual((b0[1] + b0[3]) / 2 * sy, (b1[1] + b1[3]) / 2, delta=2)

    def test_scale_still_works_without_size(self):
        movie = apt_movie.load(BIG)
        for r in self.backends():
            img, _h, _l = r.render(movie, 0, 0.5)
            self.assertEqual(img.size, (int(movie.width * 0.5), int(movie.height * 0.5)))

    def test_grid_with_size(self):
        movie = apt_movie.load(BIG)
        for r in self.backends():
            r.grid = True
            img, _h, _l = r.render(movie, 0, 1.0, (1280, 720))
            r.grid = False
            self.assertEqual(img.size, (1280, 720))


if __name__ == "__main__":
    unittest.main()
