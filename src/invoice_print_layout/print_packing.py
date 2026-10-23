"""Deterministic, unrotated packing of proofs at their existing printed size."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    index: int
    page: int
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class _Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def _subtract(free: list[_Rect], occupied: _Rect) -> list[_Rect]:
    fragments: list[_Rect] = []
    for rect in free:
        if (occupied.x >= rect.right or occupied.right <= rect.x or
                occupied.y >= rect.bottom or occupied.bottom <= rect.y):
            fragments.append(rect)
            continue
        if occupied.x > rect.x:
            fragments.append(_Rect(rect.x, rect.y, occupied.x - rect.x, rect.height))
        if occupied.right < rect.right:
            fragments.append(_Rect(occupied.right, rect.y, rect.right - occupied.right, rect.height))
        if occupied.y > rect.y:
            fragments.append(_Rect(rect.x, rect.y, rect.width, occupied.y - rect.y))
        if occupied.bottom < rect.bottom:
            fragments.append(_Rect(rect.x, occupied.bottom, rect.width, rect.bottom - occupied.bottom))
    # Maximal free rectangles may overlap each other, but never occupied space.
    unique = list(dict.fromkeys(rect for rect in fragments if rect.width > .001 and rect.height > .001))
    return [rect for rect in unique if not any(
        other != rect and other.x <= rect.x and other.y <= rect.y and
        other.right >= rect.right and other.bottom >= rect.bottom for other in unique)]


def pack_fixed_sizes(sizes: list[tuple[float, float]], width: float, height: float,
                     *, gap: float = 12.0) -> list[Placement] | None:
    """Try a bounded set of stable orders. Never scale, crop, or rotate a piece.

    Coordinates are relative to the usable page. Inflate right/bottom by one
    gutter, including the bin, so a proof may reach the actual page boundary.
    Equal page counts keep the first ordering; callers retain their old layout
    unless this candidate strictly reduces the baseline number of pages.
    """
    if not sizes or width <= 0 or height <= 0 or gap < 0:
        return None
    if any(w <= 0 or h <= 0 or w > width + .001 or h > height + .001 for w, h in sizes):
        return None
    original = list(range(len(sizes)))
    orders = [original,
              sorted(original, key=lambda i: -(sizes[i][0] * sizes[i][1])),
              sorted(original, key=lambda i: -sizes[i][1]),
              sorted(original, key=lambda i: -sizes[i][0])]
    best: list[Placement] | None = None
    best_count = len(sizes) + 1
    for order in orders:
        pages: list[list[_Rect]] = []
        result = []
        for index in order:
            w, h = sizes[index]
            candidates = [(number, rect.y, rect.x, min(rect.width - w - gap, rect.height - h - gap), rect)
                          for number, free in enumerate(pages) for rect in free
                          if w + gap <= rect.width + .001 and h + gap <= rect.height + .001]
            if candidates:
                number, _, _, _, target = min(candidates, key=lambda entry: entry[:4])
            else:
                number = len(pages)
                target = _Rect(0, 0, width + gap, height + gap)
                pages.append([target])
            result.append(Placement(index, number, target.x, target.y, w, h))
            pages[number] = _subtract(pages[number], _Rect(target.x, target.y, w + gap, h + gap))
        if len(pages) < best_count:
            best, best_count = result, len(pages)
    return best
