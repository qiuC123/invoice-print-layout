"""Offline lodging headcount interpretation and review-only capacity estimates.

These functions never assign people to rooms, merge rooms, or update a stay.
The caller owns the dated headcount baseline and the source message identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

ChangeKind = Literal["total", "increase", "decrease", "unchanged"]
RoomType = Literal["king", "twin"]


def _validate_count(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


@dataclass(frozen=True)
class LodgingChange:
    kind: ChangeKind | None
    amount: int | None
    total_people: int | None
    clarification: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return self.clarification is not None


_NUMBER = r"(?:[0-9]+|[零一二两三四五六七八九十]+)"
_UNIT = r"(?:个)?人"
_PATTERNS: tuple[tuple[ChangeKind, str], ...] = (
    ("increase", rf"(?:住宿)?(?:增加|新增|加|加了|再加|多了|来了)({_NUMBER}){_UNIT}"),
    ("decrease", rf"(?:住宿)?(?:减少|减|减了|少了|走了|撤走|撤走了|撤了)({_NUMBER}){_UNIT}"),
    (
        "total",
        rf"(?:(?:现在|目前|今晚|今天)?(?:住宿)?(?:人数)?(?:是|有|共|一共|总共|总人数为|总人数|总数为|总数|剩下|剩)?)(?:[:：])?({_NUMBER}){_UNIT}",
    ),
)
_UNCHANGED = {
    "无变化", "没变", "没变化", "没有变化", "人数没变", "人员没变", "人数没有变化",
    "住宿人数没变", "住宿人员没变", "住宿人数没有变化", "住宿人员无变化",
    "住宿人员没有变化", "住宿没变化", "不变", "和昨天一样",
}
_DIGITS = {char: value for value, char in enumerate("零一二三四五六七八九")}
_DIGITS["两"] = 2


def _parse_number(text: str) -> int | None:
    if text.isascii() and text.isdigit():
        # Avoid unbounded integer parsing of malformed or adversarial messages.
        return int(text) if len(text) <= 6 else None
    if text in _DIGITS:
        return _DIGITS[text]
    if text.count("十") == 1:
        tens, units = text.split("十")
        if (not tens or tens in _DIGITS) and (not units or units in _DIGITS):
            tens_value = _DIGITS[tens] if tens else 1
            if tens_value > 0:
                return tens_value * 10 + (_DIGITS[units] if units else 0)
    return None


def parse_lodging_change(text: str, baseline_people: int | None = None) -> LodgingChange:
    """Understand only complete, unambiguous lodging replies in a lodging task.

    Unrecognized text, several numbers, and conflicting clauses require a human
    clarification. A definite total needs no baseline; deltas and no-change do.
    """
    if baseline_people is not None:
        _validate_count(baseline_people, "baseline_people")
    if re.search(r"[0-9零一二两三四五六七八九十]\s+[0-9零一二两三四五六七八九十]", text):
        return LodgingChange(None, None, None, "消息中有分开的数字，请明确住宿总人数或增减人数。")
    normalized = re.sub(r"\s+", "", text).rstrip("。！!，,")
    kind: ChangeKind | None = None
    amount: int | None = None
    if normalized in _UNCHANGED:
        kind, amount = "unchanged", 0
    else:
        for candidate, pattern in _PATTERNS:
            match = re.fullmatch(pattern, normalized)
            if match:
                amount = _parse_number(match.group(1))
                if amount is not None:
                    kind = candidate
                break
    if kind is None:
        return LodgingChange(None, None, None, "请说明住宿总人数，或明确增加/减少了几人。")
    if kind == "total":
        return LodgingChange(kind, amount, amount)
    if baseline_people is None:
        return LodgingChange(kind, amount, None, "还没有已确认的住宿人数，请补充当前总人数。")
    assert amount is not None
    total = baseline_people + (amount if kind == "increase" else -amount)
    if total < 0:
        return LodgingChange(kind, amount, None, "减少人数超过已确认人数，请核对当前总人数。")
    return LodgingChange(kind, amount, total)


@dataclass(frozen=True)
class RoomOccupancy:
    room_id: str
    room_type: RoomType | None = None
    occupants: int | None = None
    capacity_override: int | None = None
    exclusive: bool = False

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("room_id must not be empty")
        if self.room_type not in (None, "king", "twin"):
            raise ValueError("room_type must be king, twin, or None")
        if self.occupants is not None:
            _validate_count(self.occupants, "occupants")
        if self.capacity_override is not None:
            _validate_count(self.capacity_override, "capacity_override")
            if self.capacity_override == 0:
                raise ValueError("capacity_override must be positive")

    @property
    def effective_capacity(self) -> int | None:
        # An explicit single-occupancy arrangement overrides physical bed size.
        if self.exclusive:
            return 1
        if self.capacity_override is not None:
            return self.capacity_override
        return {"king": 2, "twin": 4}.get(self.room_type or "")


@dataclass(frozen=True)
class LodgingRoomPreview:
    target_people: int
    known_capacity: int
    known_occupants: int
    known_vacancies: int
    current_people: int | None
    arriving_people: int | None
    departing_people: int | None
    additional_twin_rooms: int | None
    clarification: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification)


def preview_lodging_rooms(
    target_people: int, rooms: Sequence[RoomOccupancy]
) -> LodgingRoomPreview:
    """Estimate extra four-person twin rooms after using confirmed vacancies.

    ``rooms`` must be the current room inventory, not historical stay/renewal
    rows. An empty inventory explicitly means no rooms. Unknown capacity or
    occupancy blocks the final estimate, while known subtotals remain visible.
    Fewer people produce a departure notice, never a room-release instruction.
    """
    _validate_count(target_people, "target_people")
    capacity = occupants = vacancies = 0
    issues: list[str] = []
    seen: set[str] = set()
    for room in rooms:
        if room.room_id in seen:
            issues.append(f"房间 {room.room_id} 重复，请先核实当前房间清单。")
            continue
        seen.add(room.room_id)
        effective = room.effective_capacity
        if effective is None:
            issues.append(f"房间 {room.room_id} 缺少房型或已确认的有效容量。")
        else:
            capacity += effective
        if room.occupants is None:
            issues.append(f"房间 {room.room_id} 缺少实际入住人数。")
        else:
            occupants += room.occupants
        if effective is not None and room.occupants is not None:
            if room.occupants > effective:
                issues.append(f"房间 {room.room_id} 实住人数超过有效容量，请核实。")
            else:
                vacancies += effective - room.occupants
    notes = ["仅为待核对预览，未分配人员、合并房间或办理退房。"]
    if issues:
        return LodgingRoomPreview(
            target_people, capacity, occupants, vacancies, None, None, None, None,
            tuple(issues), tuple(notes),
        )
    arriving = max(0, target_people - occupants)
    departing = max(0, occupants - target_people)
    additional = (max(0, arriving - vacancies) + 3) // 4
    if departing:
        notes.append(f"人数减少 {departing} 人；需核实离开人员所在房间及实际空位，保留现有房间。")
    if additional:
        notes.append(f"已有明确空位可供 {vacancies} 人入住，另参考新增 {additional} 间普通双床房，每间按 4 人计算。")
    return LodgingRoomPreview(
        target_people, capacity, occupants, vacancies, occupants, arriving, departing,
        additional, (), tuple(notes),
    )
