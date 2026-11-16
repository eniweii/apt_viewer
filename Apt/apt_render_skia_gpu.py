#!/usr/bin/env python3
"""
apt_render_skia_gpu.py

Skia on the GPU (OpenGL) with the same drawing rules as apt_render_skia.py: it is that renderer with a GPU surface, GPU
textures and a readback to a Pillow image at the end of each frame, so apt_view.py can use it like the others.
  Surface   a GPU render target (reused while the size stays the same), cleared with the movie background each frame
  Textures  every Pillow image becomes a GPU texture once and stays cached (a texture that cannot be made stays a raster image)
  Readback  the finished frame is copied back to a Pillow image for the Tk window (one copy per frame)
  Fast images is not used here: its tricks (premultiplied textures, tinted copies) are for the CPU raster path
Needs:  pip install skia-python glfw   and a working OpenGL driver. A hidden window gives the GL context; nothing is shown.
Choose it with --renderer skia-gpu (or "skia-gpu" in the launcher). "auto" never picks it.
"""
import numpy as np
import skia
from PIL import Image

import apt_render_skia
from apt_render_skia import Layer, RGBA, UNPREMUL

try:
    import glfw
except ImportError:                                     # the viewer tells the user what to install
    glfw = None

TEXTURE_LIMIT = 1500                                    # cached textures before the cache is emptied


def available():
    """True when glfw can be imported (the GL context itself is only known when the renderer is made)."""
    return glfw is not None


class SkiaGpuRenderer(apt_render_skia.SkiaRenderer):
    backend = "skia-gpu"

    def __init__(self, resources, boxes=False):
        if glfw is None:
            raise ImportError("glfw is not installed")
        super().__init__(resources, boxes)
        if not glfw.init():
            raise RuntimeError("glfw could not start (no display or no OpenGL support)")
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
        self._win = glfw.create_window(64, 64, "apt gpu", None, None)
        if not self._win:
            raise RuntimeError("could not create an OpenGL context (check the graphics driver)")
        glfw.make_context_current(self._win)
        self._ctx = skia.GrDirectContext.MakeGL()
        if self._ctx is None:
            raise RuntimeError("Skia could not use the OpenGL context")
        self._gpu_surface = None                        # reused while (w, h) stays the same
        self._gpu_size = None

    # Fast images belongs to the CPU renderer; here it stays off whatever the viewer sets.
    @property
    def fast_images(self):
        return False

    @fast_images.setter
    def fast_images(self, value):
        pass

    def _current(self):
        glfw.make_context_current(self._win)

    def _sk(self, pil):
        """The Pillow image as a GPU texture, made once."""
        hit = self._sk_images.get(id(pil))
        if hit is None:
            if len(self._sk_images) > TEXTURE_LIMIT:
                self._sk_images.clear()
                self._ctx.freeGpuResources()
            rgba = pil if pil.mode == "RGBA" else pil.convert("RGBA")
            raster = skia.Image.fromarray(np.asarray(rgba), RGBA, UNPREMUL)
            self._current()
            texture = raster.makeTextureImage(self._ctx)
            hit = self._sk_images[id(pil)] = (pil, texture if texture is not None else raster)
        return hit[1]

    def _surface(self, movie, scale, bg, size=None):
        import apt_render
        from apt_render import argb
        w, h, sx, sy = apt_render.output_geometry(movie, scale, size)
        self._current()
        if self._gpu_surface is None or self._gpu_size != (w, h):
            info = skia.ImageInfo.MakeN32Premul(w, h)
            self._gpu_surface = skia.Surface.MakeRenderTarget(self._ctx, skia.Budgeted.kNo, info)
            if self._gpu_surface is None:
                raise RuntimeError("could not make a %dx%d GPU surface" % (w, h))
            self._gpu_size = (w, h)
        r, g, b, _a = argb(bg) if bg is not None else (0.2, 0.2, 0.2, 1.0)
        surface = self._gpu_surface
        surface.getCanvas().clear(skia.ColorSetARGB(255, int(r * 255), int(g * 255), int(b * 255)))
        return surface, Layer(surface.getCanvas(), w, h), sx, sy

    def _to_pil(self, surface):
        self._current()
        self._ctx.flushAndSubmit()
        arr = surface.makeImageSnapshot().toarray(colorType=RGBA, alphaType=UNPREMUL)
        return Image.fromarray(arr, "RGBA")
