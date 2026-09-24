from __future__ import annotations

import pytest

from invoice_print_layout.lodging_capacity import (
    RoomOccupancy,
    parse_lodging_change,
    preview_lodging_rooms,
)


@pytest.mark.parametrize("text", ["21人", "现在住宿21人", "住宿人数：21人", "一共21人。", "今晚住宿二十一个人", "总人数21人", " 住宿 21 人 "])
def test_explicit_total_needs_no_baseline(text: str) -> None:
    result = parse_lodging_change(text)
    assert (result.kind, result.total_people, result.needs_clarification) == ("total", 21, False)


@pytest.mark.parametrize(("text", "kind", "expected"), [
    ("增加2人", "increase", 23), ("再加两个人", "increase", 23),
    ("走了3人", "decrease", 18), ("减少21人", "decrease", 0),
    ("没有变化", "unchanged", 21), ("和昨天一样", "unchanged", 21),
    ("没变", "unchanged", 21), ("加2人", "increase", 23), ("减2人", "decrease", 19),
])
def test_delta_and_no_change_use_confirmed_baseline(text: str, kind: str, expected: int) -> None:
    result = parse_lodging_change(text, 21)
    assert (result.kind, result.total_people) == (kind, expected)
    assert not result.needs_clarification


@pytest.mark.parametrize("text", ["增加2人", "减少2人", "没有变化"])
def test_missing_baseline_is_not_zero(text: str) -> None:
    result = parse_lodging_change(text)
    assert result.kind is not None
    assert result.total_people is None
    assert result.needs_clarification


@pytest.mark.parametrize("text", [
    "21", "收到", "稍后回复", "21份", "21人或23人", "增加2人，减少1人", "21人，5间房",
    "住宿-2人", "增加-2人", "住宿2.5人", "没有变化，增加2人", "可能21人", "二三人",
    "21人？", "不要增加2人", "明天21人", "晚饭21人", "住宿9999999999999人",
    "2 1人", "2\n1人", "二 三人",
])
def test_ambiguous_or_invalid_message_requires_clarification(text: str) -> None:
    result = parse_lodging_change(text, 10)
    assert result.needs_clarification
    assert result.total_people is None


def test_explicit_zero_and_overlarge_departure() -> None:
    assert parse_lodging_change("住宿0人").total_people == 0
    assert parse_lodging_change("减少3人", 2).needs_clarification


def test_mixed_capacities_preserve_per_room_exception() -> None:
    rooms = [
        RoomOccupancy("a", "twin", 3),
        RoomOccupancy("b", "twin", 2, capacity_override=3),
        RoomOccupancy("c", "king", 1),
        RoomOccupancy("d", "twin", 1, exclusive=True),
    ]
    assert [room.effective_capacity for room in rooms] == [4, 3, 2, 1]
    result = preview_lodging_rooms(14, rooms)
    assert (result.known_capacity, result.current_people, result.known_vacancies) == (10, 7, 3)
    assert result.arriving_people == 7
    assert result.additional_twin_rooms == 1
    assert not result.needs_clarification
    assert [room.occupants for room in rooms] == [3, 2, 1, 1]


def test_exclusive_occupancy_overrides_explicit_physical_capacity() -> None:
    room = RoomOccupancy("a", "twin", 1, capacity_override=4, exclusive=True)
    result = preview_lodging_rooms(2, [room])
    assert result.known_vacancies == 0
    assert result.additional_twin_rooms == 1


@pytest.mark.parametrize("target, expected", [(0, 0), (1, 1), (4, 1), (5, 2), (9, 3)])
def test_new_inventory_rounds_up(target: int, expected: int) -> None:
    assert preview_lodging_rooms(target, []).additional_twin_rooms == expected


@pytest.mark.parametrize("room", [
    RoomOccupancy("unknown_type", occupants=2),
    RoomOccupancy("unknown_people", "twin"),
    RoomOccupancy("over_capacity", "king", 3),
    RoomOccupancy("exclusive_conflict", "twin", 2, exclusive=True),
])
def test_missing_or_conflicting_room_facts_block_estimate(room: RoomOccupancy) -> None:
    result = preview_lodging_rooms(8, [room])
    assert result.additional_twin_rooms is None
    assert result.current_people is None
    assert result.needs_clarification


def test_explicit_capacity_is_evidence_without_room_type() -> None:
    result = preview_lodging_rooms(3, [RoomOccupancy("a", occupants=2, capacity_override=3)])
    assert result.additional_twin_rooms == 0
    assert result.known_vacancies == 1


def test_multiple_departures_do_not_merge_or_release_rooms() -> None:
    rooms = [RoomOccupancy(str(index), "twin", 4) for index in range(3)]
    result = preview_lodging_rooms(3, rooms)
    assert result.departing_people == 9
    assert result.additional_twin_rooms == 0
    assert result.known_vacancies == 0  # No guess about which room people leave.
    assert any("保留现有房间" in note for note in result.notes)
    assert len(rooms) == 3 and all(room.occupants == 4 for room in rooms)


def test_repeated_room_or_renewal_rows_are_not_double_counted() -> None:
    room = RoomOccupancy("a", "twin", 3)
    result = preview_lodging_rooms(8, [room, room])
    assert result.needs_clarification
    assert result.known_capacity == 4
    assert result.additional_twin_rooms is None


@pytest.mark.parametrize("value", [-1, True])
def test_invalid_baseline_is_programming_error(value: int) -> None:
    with pytest.raises(ValueError):
        parse_lodging_change("21人", value)
    with pytest.raises(ValueError):
        preview_lodging_rooms(value, [])


def test_invalid_room_inputs() -> None:
    with pytest.raises(ValueError):
        RoomOccupancy(" ", "twin", 0)
    with pytest.raises(ValueError):
        RoomOccupancy("a", "twin", -1)
    with pytest.raises(ValueError):
        RoomOccupancy("a", "twin", 0, capacity_override=0)
