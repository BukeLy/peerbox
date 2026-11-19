# Set a word in Allo at the given axes, shaped with HarfBuzz (kerning included), and write its outlines as one SVG path.
# The landing page's relief words come from this: wordmark.svg is "peerbox" and leave-a-note.svg is "Leave a note.", both at
# weight 800 and ROND 100. The font is https://cdn.jsdelivr.net/npm/allo-font@1.0.3/fonts/webfonts/AlloVariable.woff2
# usage, from docs/: uv run --with fonttools --with uharfbuzz --with brotli tools/outline.py AlloVariable.woff2 "peerbox" 800 100 wordmark.svg
# then run tools/glassword.py, which redraws the words in index.html from the new outlines
import io
import sys

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

src, text, wght, rond, out = (
    sys.argv[1],
    sys.argv[2],
    float(sys.argv[3]),
    float(sys.argv[4]),
    sys.argv[5],
)
f = instantiateVariableFont(TTFont(src), {"opsz": 32, "wght": wght, "ROND": rond})
f.flavor = None
buf = io.BytesIO()
f.save(buf)
data = buf.getvalue()
font = hb.Font(hb.Face(data))
b = hb.Buffer()
b.add_str(text)
b.guess_segment_properties()
hb.shape(font, b, {"kern": True, "liga": True})
gs = f.getGlyphSet()
order = f.getGlyphOrder()
upm = f["head"].unitsPerEm
pen = SVGPathPen(gs)
bounds = BoundsPen(gs)
x = 0
for info, pos in zip(b.glyph_infos, b.glyph_positions, strict=True):
    name = order[info.codepoint]
    t = (1, 0, 0, -1, x + pos.x_offset, 0)  # flip y: font units are y-up
    gs[name].draw(TransformPen(pen, t))
    gs[name].draw(TransformPen(bounds, t))
    x += pos.x_advance
x0, y0, x1, y1 = bounds.bounds  # y is flipped: y0 is the top (negative), baseline at 0
pad = round(upm * 0.02)
vb = (x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(str(round(v)) for v in vb)}"><path d="{pen.getCommands()}"/></svg>\n'
open(out, "w").write(svg)
print(
    out,
    "viewBox",
    [round(v) for v in vb],
    "baseline-from-top/h=%.4f" % ((0 - vb[1]) / vb[3]),
    "w/h=%.4f" % (vb[2] / vb[3]),
    "bytes",
    len(svg),
)
