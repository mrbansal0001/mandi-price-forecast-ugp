"""Local glyph-template OCR for Agmarknet's plain six-character images.

Uses no remote solver. Rejects unsegmented images rather than guessing strings.
"""
from pathlib import Path
import io
import string
import numpy as np
from PIL import Image, ImageFont, ImageDraw

class OCRFailure(Exception):
    pass

class TemplateOCR:
    def __init__(self, font=None):
        if font is None:
            local = Path(__file__).resolve().parents[1] / 'assets' / 'DejaVuSans.ttf'
            if local.exists():
                font = local
            else:
                try:
                    import importlib.util
                    spec = importlib.util.find_spec('matplotlib')
                    font = Path(spec.origin).parent / 'mpl-data/fonts/ttf/DejaVuSans.ttf'
                except (AttributeError, TypeError):
                    raise OCRFailure('Pass --font pointing to DejaVuSans.ttf.')
        self.font = str(font)
        self.templates = []
        for size in [38, 39, 40, 41, 42]:
            f = ImageFont.truetype(self.font, size)
            for char in string.ascii_letters + string.digits:
                im = Image.new('L', (90, 90), 255)
                ImageDraw.Draw(im).text((10, 5), char, font=f, fill=0)
                mask = np.array(im) < 128
                ys, xs = np.where(mask)
                self.templates.append((char, mask[ys.min():ys.max()+1, xs.min():xs.max()+1]))

    def solve(self, image_bytes):
        im = Image.open(io.BytesIO(image_bytes)).convert('L')
        if im.width > 2000 or im.height > 1000:
            raise OCRFailure('Unexpected challenge dimensions.')
        pixels = np.array(im)
        # The current image is dark text on a light background. Normalise contrast.
        lo, hi = np.percentile(pixels, [2, 90])
        if hi - lo < 20:
            raise OCRFailure('Insufficient image contrast.')
        mask = pixels < (lo + hi) / 2
        occupied = mask.any(axis=0)
        runs = []; start = None
        for i, yes in enumerate(list(occupied) + [False]):
            if yes and start is None: start = i
            if not yes and start is not None:
                runs.append((start, i)); start = None
        if len(runs) != 6:
            raise OCRFailure(f'Expected six separate glyphs; found {len(runs)}.')
        answer = ''; confidence = []
        for start, end in runs:
            glyph = mask[:, start:end]; ys = np.where(glyph.any(axis=1))[0]
            glyph = glyph[ys.min():ys.max()+1]
            scores = []
            for char, template in self.templates:
                resized = np.array(Image.fromarray(template).resize((glyph.shape[1], glyph.shape[0]), Image.Resampling.NEAREST))
                mismatch = np.mean(resized != glyph)
                dimensions = .015 * (abs(template.shape[0]-glyph.shape[0]) + abs(template.shape[1]-glyph.shape[1]))
                scores.append((mismatch + dimensions, char))
            scores.sort(); score, char = scores[0]
            if score > .22: raise OCRFailure('Glyph does not match supported font; refusing low-confidence answer.')
            answer += char; confidence.append(round(1-score, 3))
        return answer, confidence
