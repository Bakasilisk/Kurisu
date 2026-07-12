"""Prep a manga panel for a new captions.py Template: erase the text inside its
speech bubble and print the bubble's Region.box coordinates.

Usage: python scripts/detect_bubble.py <input.png> <output.png>

Detection works by flood-filling from the image border through dark ("wall")
pixels to find the largest fully-enclosed light region (the bubble interior),
then erasing any dark blob inside it that never touches the reachable-from-
border background (i.e. the text, as opposed to the bubble's own outline).
Assumes a single bubble that is the largest enclosed light area in the image;
review the output before wiring it into a Template.
"""
import sys
from collections import deque

from PIL import Image

WALL_THRESHOLD = 160  # grayscale below this = ink (outline or text)
MOPUP_THRESHOLD = 170  # residual antialiasing ghosts lighter than this get flattened to white


def _find_enclosed_region(wall, reachable, w, h):
    visited = bytearray(w * h)
    best = None
    for start in range(w * h):
        if wall[start] or reachable[start] or visited[start]:
            continue
        comp = [start]
        visited[start] = 1
        minx = maxx = start % w
        miny = maxy = start // w
        q = deque([start])
        while q:
            idx = q.popleft()
            y, x = divmod(idx, w)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    nidx = ny * w + nx
                    if not wall[nidx] and not reachable[nidx] and not visited[nidx]:
                        visited[nidx] = 1
                        comp.append(nidx)
                        q.append(nidx)
                        minx, maxx = min(minx, nx), max(maxx, nx)
                        miny, maxy = min(miny, ny), max(maxy, ny)
        area = len(comp)
        if best is None or area > best[0]:
            best = (area, minx, miny, maxx, maxy, comp)
    return best


def _find_ink_to_erase(wall, reachable, interior_bg, w, h):
    visited = bytearray(w * h)
    neigh8 = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
    to_erase = []
    for start in range(w * h):
        if not wall[start] or visited[start]:
            continue
        comp = [start]
        visited[start] = 1
        touches_outside = touches_interior = False
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
                            comp.append(nidx)
                            q.append(nidx)
                    elif reachable[nidx]:
                        touches_outside = True
                    elif nidx in interior_bg:
                        touches_interior = True
        if touches_interior and not touches_outside:
            to_erase.extend(comp)
    return to_erase


def detect_and_whiteout(in_path, out_path):
    img = Image.open(in_path).convert("RGB")
    w, h = img.size
    gpix = img.convert("L").load()

    wall = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if gpix[x, y] < WALL_THRESHOLD:
                wall[y * w + x] = 1

    reachable = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            idx = y * w + x
            if not wall[idx]:
                reachable[idx] = 1
                q.append(idx)
    for y in range(h):
        for x in (0, w - 1):
            idx = y * w + x
            if not wall[idx] and not reachable[idx]:
                reachable[idx] = 1
                q.append(idx)
    while q:
        idx = q.popleft()
        y, x = divmod(idx, w)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                nidx = ny * w + nx
                if not wall[nidx] and not reachable[nidx]:
                    reachable[nidx] = 1
                    q.append(nidx)

    area, minx, miny, maxx, maxy, comp = _find_enclosed_region(wall, reachable, w, h)
    interior_bg = set(comp)
    to_erase = _find_ink_to_erase(wall, reachable, interior_bg, w, h)

    pix = img.load()
    for idx in comp:
        y, x = divmod(idx, w)
        pix[x, y] = (255, 255, 255)
    for idx in to_erase:
        y, x = divmod(idx, w)
        pix[x, y] = (255, 255, 255)

    # Mop up faint antialiasing remnants left by the erased text, staying well
    # clear of the bubble's own outline near the box edges.
    inset = 15
    left, top = minx + inset, miny + inset
    right, bottom = maxx + 1 - inset, maxy + 1 - inset
    if right > left and bottom > top:
        for y in range(top, bottom):
            for x in range(left, right):
                r, g, b = pix[x, y]
                if r > MOPUP_THRESHOLD and g > MOPUP_THRESHOLD and b > MOPUP_THRESHOLD:
                    pix[x, y] = (255, 255, 255)

    img.save(out_path)
    return w, h, (minx, miny, maxx + 1, maxy + 1)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} <input.png> <output.png>")
        raise SystemExit(1)
    w, h, box = detect_and_whiteout(sys.argv[1], sys.argv[2])
    print(f"image size: {w}x{h}")
    print(f"Region(box={box})")
