"""Prep a manga panel for a new captions.py Template: find its speech bubble(s),
erase the text inside them, and print the Region.box coordinates to use.

Usage:
    python scripts/detect_bubble.py list <input.png>
        Lists bright (candidate bubble) connected components by area, largest
        first, with their bounding box and whether they touch the image edge.
        Pick the index/indices that are actually the speech bubbles (eye
        highlights, glare, etc. show up too, but are much smaller).

    python scripts/detect_bubble.py erase <input.png> <output.png> <index> [index ...]
        Erases the text inside the chosen bright component(s) — any dark blob
        fully surrounded by that component's own pixels (i.e. never touching
        anything else, so not the bubble's outline, which always borders the
        background on its far side) — and prints one Region(box=...) per index,
        in the order given.

Works whether the bubble sits fully inside the frame or is cropped by the
image border (a bright component's own edge pixels still correctly separate
its interior ink from its outline either way). Review the erased output before
wiring it into a Template — this is a detection aid, not a guarantee.
"""
import sys
from collections import deque

from PIL import Image

BRIGHT_THRESHOLD = 160  # grayscale at/above this = candidate bubble background
MIN_AREA = 200  # skip noise-sized bright components (specular highlights, dots, ...)
MOPUP_THRESHOLD = 170  # residual antialiasing ghosts lighter than this get flattened to white


def _label_components(mask, w, h):
    """4-connected components of pixels where mask[i] is truthy."""
    visited = bytearray(w * h)
    components = []
    for start in range(w * h):
        if not mask[start] or visited[start]:
            continue
        comp = [start]
        visited[start] = 1
        minx = maxx = start % w
        miny = maxy = start // w
        touches_border = False
        q = deque([start])
        while q:
            idx = q.popleft()
            y, x = divmod(idx, w)
            if x == 0 or y == 0 or x == w - 1 or y == h - 1:
                touches_border = True
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    nidx = ny * w + nx
                    if mask[nidx] and not visited[nidx]:
                        visited[nidx] = 1
                        comp.append(nidx)
                        q.append(nidx)
                        minx, maxx = min(minx, nx), max(maxx, nx)
                        miny, maxy = min(miny, ny), max(maxy, ny)
        components.append((len(comp), minx, miny, maxx, maxy, touches_border, comp))
    components.sort(key=lambda c: c[0], reverse=True)
    return components


def _bright_mask(img):
    w, h = img.size
    gpix = img.convert("L").load()
    mask = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if gpix[x, y] >= BRIGHT_THRESHOLD:
                mask[y * w + x] = 1
    return mask, w, h


def list_candidates(path):
    img = Image.open(path).convert("RGB")
    mask, w, h = _bright_mask(img)
    components = _label_components(mask, w, h)
    print(f"image size: {w}x{h}")
    shown = 0
    for area, minx, miny, maxx, maxy, touches_border, _ in components:
        if area < MIN_AREA:
            break
        edge = " (touches edge)" if touches_border else ""
        print(f"[{shown}] area={area} box=({minx},{miny},{maxx+1},{maxy+1}){edge}")
        shown += 1
        if shown >= 15:
            break


def erase_and_box(path, out_path, indices):
    img = Image.open(path).convert("RGB")
    mask, w, h = _bright_mask(img)
    components = _label_components(mask, w, h)
    candidates = [c for c in components if c[0] >= MIN_AREA]

    pix = img.load()
    boxes = []
    for i in indices:
        area, minx, miny, maxx, maxy, _, comp = candidates[i]

        # Anti-aliasing can fragment the bubble's interior into several
        # disconnected bright islands (gaps pinched off by nearby ink). Any
        # such island spatially nested inside this candidate's box is still
        # part of the same bubble, so fold it back into the background set —
        # otherwise ink touching only that stray island looks "external" and
        # survives erasure.
        bg = set()
        for c_area, c_minx, c_miny, c_maxx, c_maxy, _, c_pixels in components:
            if c_minx >= minx and c_miny >= miny and c_maxx <= maxx and c_maxy <= maxy:
                bg.update(c_pixels)

        # ink to erase: dark blobs fully surrounded by this bright component
        wall = bytearray(w * h)
        for idx in range(w * h):
            if not mask[idx]:
                wall[idx] = 1
        visited = bytearray(w * h)
        neigh8 = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
        to_erase = []
        for start in range(w * h):
            if not wall[start] or visited[start]:
                continue
            wcomp = [start]
            visited[start] = 1
            only_touches_bg = True
            touches_bg = False
            q = deque([start])
            while q:
                idx = q.popleft()
                y, x = divmod(idx, w)
                for dx, dy in neigh8:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if wall[nidx]:
                            if not visited[nidx]:
                                visited[nidx] = 1
                                wcomp.append(nidx)
                                q.append(nidx)
                        elif nidx in bg:
                            touches_bg = True
                        else:
                            only_touches_bg = False
            if touches_bg and only_touches_bg:
                to_erase.extend(wcomp)

        for idx in comp:
            y, x = divmod(idx, w)
            pix[x, y] = (255, 255, 255)
        for idx in to_erase:
            y, x = divmod(idx, w)
            pix[x, y] = (255, 255, 255)

        inset = 15
        left, top = minx + inset, miny + inset
        right, bottom = maxx + 1 - inset, maxy + 1 - inset
        if right > left and bottom > top:
            for y in range(top, bottom):
                for x in range(left, right):
                    r, g, b = pix[x, y]
                    if r > MOPUP_THRESHOLD and g > MOPUP_THRESHOLD and b > MOPUP_THRESHOLD:
                        pix[x, y] = (255, 255, 255)

        boxes.append((minx, miny, maxx + 1, maxy + 1))

    img.save(out_path)
    for box in boxes:
        print(f"Region(box={box})")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "list":
        list_candidates(sys.argv[2])
    elif len(sys.argv) >= 5 and sys.argv[1] == "erase":
        erase_and_box(sys.argv[2], sys.argv[3], [int(a) for a in sys.argv[4:]])
    else:
        print(__doc__)
        raise SystemExit(1)
