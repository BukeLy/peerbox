# Markup for a word in relief, made of the panels' frosted glass, from an outlined SVG (see outline.py).
# The page keeps each word twice: the SVG file masks the frost, and this inlined copy draws the shadow, rim and light,
# so after changing a word's outline, rerun this to keep the two in step. Run from the repo root: python3 tools/glassword.py
# The cast shadow tucks under the word by blurring it and cutting at a raised alpha rather than with feMorphology,
# whose erode cost Safari ~270 ms a frame; the cut needs an opaque source, so the page sets the shadow's strength
# with opacity, which applies after the filter.
# Every id carries the prefix, so several words can live on one page.
import re


def glass_word(svg_file, p, indent):
    svg = open(svg_file).read()
    vb = [int(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
    d = re.search(r' d="([^"]+)"', svg).group(1)
    v = " ".join(map(str, vb))
    M = f'maskUnits="userSpaceOnUse" x="{vb[0] - 200}" y="{vb[1] - 300}" width="{vb[2] + 400}" height="{vb[3] + 600}"'
    lines = f'''<svg viewBox="{v}">
  <defs>
    <path id="{p}-p" d="{d}"/>
    <filter id="{p}-lift" x="-10%" y="-20%" width="120%" height="160%"><feGaussianBlur stdDeviation="50"/><feComponentTransfer><feFuncA type="linear" slope="3" intercept="-1.9"/></feComponentTransfer><feGaussianBlur stdDeviation="120"/><feOffset dy="120"/></filter>
    <filter id="{p}-near" x="-5%" y="-10%" width="110%" height="130%"><feGaussianBlur stdDeviation="9"/><feOffset dy="9"/></filter>
  </defs>
  <use href="#{p}-p" class="gw-cast" filter="url(#{p}-lift)"/>
  <use href="#{p}-p" class="gw-contact" filter="url(#{p}-near)"/>
</svg>
<span class="gw-frost"></span>
<svg class="gw-light" viewBox="{v}">
  <defs>
    <clipPath id="{p}-in"><use href="#{p}-p"/></clipPath>
    <linearGradient id="{p}-rim" gradientUnits="userSpaceOnUse" x1="0" y1="-1118" x2="0" y2="0">
      <stop offset="0" style="stop-color: var(--wm-rim-1)"/><stop offset="0.32" style="stop-color: var(--wm-rim-2)"/>
      <stop offset="0.62" style="stop-color: var(--wm-rim-3)"/><stop offset="1" style="stop-color: var(--wm-rim-4)"/>
    </linearGradient>
    <filter id="{p}-soft" x="-5%" y="-30%" width="110%" height="160%"><feGaussianBlur stdDeviation="40"/></filter>
    <mask id="{p}-top-line" {M}><use href="#{p}-p" fill="#fff"/><use href="#{p}-p" fill="#000" y="12"/></mask>
    <mask id="{p}-top-glow" {M}><use href="#{p}-p" fill="#fff"/><use href="#{p}-p" fill="#000" y="110"/></mask>
    <mask id="{p}-foot-line" {M}><use href="#{p}-p" fill="#fff"/><use href="#{p}-p" fill="#000" y="-12"/></mask>
    <mask id="{p}-foot-glow" {M}><use href="#{p}-p" fill="#fff"/><use href="#{p}-p" fill="#000" y="-130"/></mask>
  </defs>
  <g clip-path="url(#{p}-in)">
    <g filter="url(#{p}-soft)"><use href="#{p}-p" class="gw-glow" mask="url(#{p}-top-glow)"/></g>
    <g filter="url(#{p}-soft)"><use href="#{p}-p" class="gw-foot" mask="url(#{p}-foot-glow)"/></g>
    <use href="#{p}-p" class="gw-rim" stroke="url(#{p}-rim)" stroke-width="3" vector-effect="non-scaling-stroke"/>
    <use href="#{p}-p" class="gw-hi" mask="url(#{p}-top-line)"/>
    <use href="#{p}-p" class="gw-lo" mask="url(#{p}-foot-line)"/>
  </g>
</svg>'''
    return ("\n" + indent).join(lines.split("\n"))


if __name__ == "__main__":
    s = open("index.html").read()
    for opening, svg_file, p, indent in [
        (
            '<div class="mast-word glass-word" aria-hidden="true">',
            "wordmark.svg",
            "mw",
            "        ",
        ),
        (
            """<span class="glass-word" aria-hidden="true" style="--word-mask: url('leave-a-note.svg')">""",
            "leave-a-note.svg",
            "lan",
            "      ",
        ),
    ]:
        a = s.index(opening) + len(opening)
        b = s.index("</svg>", s.index('<svg class="gw-light"', a)) + len("</svg>")
        s = s[:a] + "\n" + indent + glass_word(svg_file, p, indent) + s[b:]
    open("index.html", "w").write(s)
    print("index.html: both words redrawn")
