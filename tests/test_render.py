"""Exercise the production Pillow/font pipeline after image and ABI upgrades."""
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from PIL import Image


class RenderTests(unittest.TestCase):
    def test_kindle_png_remains_8bit_and_16_grays(self):
        png = app.render_dashboard({'quotas': {}, 'generated_at': '2026-11-02T14:00:00+08:00',
                                    'kindle': {'kindle_battery': '87'}})
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.mode, 'L')
            self.assertEqual(image.size, (app.WIDTH, app.HEIGHT))
            self.assertLessEqual(len(image.getcolors()), 16)

    @unittest.skipUnless(Path('/usr/share/fonts/noto/NotoSansCJK-Regular.ttc').exists(),
                         'Alpine production CJK font is not installed on this test host')
    def test_alpine_uses_cjk_font_before_latin_fallback(self):
        self.assertIn('NotoSansCJK', app.find_font(20).path)
        self.assertIn('NotoSansCJK', app.find_font(20, bold=True).path)
