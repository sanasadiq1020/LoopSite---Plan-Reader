"""Day 3 — room labels.

Two independent detection methods run, and every room records which one (or
both) found it. That matters for the "works on an unseen PDF" requirement: a
detector built only on a keyword list can only ever find rooms whose names
someone already wrote down, and the supplied second plan set proved the point
by naming a room 'MULTI-FUNCTION' — a real room that no residential room-name
list would contain.

*   **Vocabulary** — the printed label matches a known Australian residential
    room name. High confidence, because the drawing used a word that means a
    room.
*   **Geometry** — an Australian residential plan prints a room's size
    directly beneath its name, as a paired dimension: 'MULTI-FUNCTION' with
    '(3,325 x 5,720)' on the next line, in the same column. That layout *is*
    the room callout convention, so a short label with a paired dimension
    tucked under it is a room whatever it is called. This method also yields
    the room's width, length and floor area for free, which the vocabulary
    method cannot.

Instance identity is preserved. 'BED 2', 'BED 3' and 'MASTER BED' are three
different rooms, so each keeps its printed name and its own instance number
alongside the normalised type. Collapsing all three to 'BED', as the earlier
version did, destroys exactly the distinction that Day 5's canonical model
needs to give each room a stable ID.
"""

import re

from pipeline.plan.textmodel import (
    bbox_center,
    bbox_height,
    horizontal_overlap,
    is_placeholder,
)

# A paired dimension printed as a room-size callout: '(3,325 x 5,720)',
# '3325 x 5720', '3.5 x 4.2m'. The brackets are optional because both forms
# appear on the supplied plans.
_NUMBER = r"\d{1,3}(?:,\d{3})+|\d{2,6}(?:\.\d{1,3})?"
_PAIRED_RE = re.compile(
    r"^\(?\s*(" + _NUMBER + r")\s*(?:MM|M)?\s*[xX×*]\s*(" + _NUMBER + r")\s*(?:MM|M)?\s*\)?$"
)

# A trailing instance on a room name: 'BED 2', 'BEDROOM No. 3', 'ROBE 1'.
_INSTANCE_RE = re.compile(r"\b(?:NO\.?\s*)?(\d{1,2})\s*$", re.IGNORECASE)


def _to_mm(number_text: str, unit_hint: str) -> float:
    value = float(number_text.replace(",", ""))
    if unit_hint == "m" or (value < 100 and "." in number_text):
        return value * 1000.0
    return value


def parse_paired_dimension(text: str):
    """Returns (width_mm, height_mm) for a room-size callout, else None."""
    match = _PAIRED_RE.match(text.strip().upper())
    if not match:
        return None
    unit = "m" if re.search(r"\bM\b", text.upper()) and "MM" not in text.upper() else "mm"
    try:
        return _to_mm(match.group(1), unit), _to_mm(match.group(2), unit)
    except ValueError:
        return None


_WORD_RE = re.compile(r"[A-Z][A-Z'-]*")


def _words_of(upper_text: str) -> set:
    return set(_WORD_RE.findall(upper_text))


def _extra_word_count(upper_text: str, keyword: str) -> int:
    """Words in the label that the matched room name does not account for.

    An instance marker is not an extra word: 'BEDROOM No. 2' is the name of a
    bedroom, not a bedroom plus a note.
    """
    label_words = _WORD_RE.findall(upper_text)
    name_words = set(_WORD_RE.findall(keyword.upper()))
    ignore = name_words | {"NO", "NR", "N"}
    return sum(1 for word in label_words if word not in ignore)


def _match_keyword(text: str, keywords: list):
    """Longest keyword first, so 'MASTER BEDROOM' is not reduced to 'BEDROOM'
    and 'WALK IN ROBE' is not reduced to 'ROBE'."""
    upper = text.upper()
    for keyword in sorted(keywords, key=len, reverse=True):
        if re.search(r"(?<![A-Z])" + re.escape(keyword.upper()) + r"(?![A-Z])", upper):
            return keyword.upper()
    return None


# Room names are set in capitals, but an abbreviation inside one is not
# ('BEDROOM No. 2'). The threshold sits between that and a genuine sentence
# note: measured on the supplied plans, 'BEDROOM No. 2' is 89% upper-case
# while 'Gas HP' is 60% and 'Skillion roof to carport' is 5%.
_MIN_UPPER_CASE_RATIO = 0.75


def _is_upper_case_label(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= _MIN_UPPER_CASE_RATIO


def _instance_of(text: str):
    match = _INSTANCE_RE.search(text.strip())
    return match.group(1) if match else None


def _dimension_below(label_line: dict, lines: list, max_heights: float):
    """A paired dimension printed directly beneath a label, in its column."""
    lx0, ly0, lx1, ly1 = label_line["bbox"]
    label_h = bbox_height(label_line["bbox"]) or 8.0
    best = None
    best_gap = None
    for other in lines:
        if other is label_line:
            continue
        gap = other["bbox"][1] - ly1
        if gap < -label_h * 0.4 or gap > label_h * max_heights:
            continue
        overlap = horizontal_overlap(label_line["bbox"], other["bbox"])
        narrowest = min(lx1 - lx0, other["bbox"][2] - other["bbox"][0]) or 1.0
        if overlap / narrowest < 0.35:
            continue
        parsed = parse_paired_dimension(other["text"])
        if parsed is None:
            continue
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = (other, parsed)
    return best


def detect_rooms(lines: list, config: dict, sheet_id: str) -> list:
    rooms_config = config["rooms"]
    thresholds = config["confidence_thresholds"]
    keywords = rooms_config["keywords"]
    exclusions = [e.upper() for e in rooms_config.get("exclusion_keywords", [])]
    max_length = rooms_config.get("max_label_length", 28)
    search_heights = rooms_config.get("paired_dimension_search_heights", 2.2)
    min_area_m2 = float(rooms_config.get("min_room_area_m2", 1.0))
    min_side_mm = float(rooms_config.get("min_room_side_mm", 900))
    element_words = {w.upper() for w in rooms_config.get("element_words", [])}
    max_extra_words = int(rooms_config.get("max_extra_words", 1))

    found: list = []
    for line in lines:
        text = line["text"].strip()
        if not text or is_placeholder(text):
            continue
        if len(text) > max_length:
            continue
        if not any(c.isalpha() for c in text):
            continue
        upper = text.upper()
        if any(exclusion in upper for exclusion in exclusions):
            continue
        # A line that is itself a dimension is never a room name.
        if parse_paired_dimension(text) is not None:
            continue

        # Australian residential plans set room names in capitals; a
        # mixed-case phrase on a plan is a note ('Skillion roof to carport',
        # 'Gas HP'). Requiring capitals is what separates the two, and it is a
        # drawing convention rather than a guess about any particular plan.
        if not _is_upper_case_label(text):
            continue

        # A label that names a building element is a note, not a room, even
        # when a room word sits inside it. An elevation prints "PROPOSED DECK
        # ROOF SHEETING" and a specification sheet "PLATE TO VERANDAH BEAM";
        # both contain a real room word and neither is a room.
        if any(word in _words_of(upper) for word in element_words):
            continue

        keyword = _match_keyword(text, keywords)
        # A room label is the room's name, not a sentence mentioning it.
        # "REAR DECK" and "MASTER BEDROOM" are labels; a name buried in three
        # more words is an annotation.
        if keyword is not None and _extra_word_count(upper, keyword) > max_extra_words:
            keyword = None
        size = _dimension_below(line, lines, search_heights)

        if keyword is None and size is None:
            continue

        if keyword is not None and size is not None:
            method = "vocabulary_and_paired_dimension"
            multiplier = 1.0
        elif keyword is not None:
            method = "vocabulary"
            multiplier = 1.0 if upper == keyword else 0.9
        else:
            # Geometry only. Require the label to read like a name rather than
            # a note: mostly letters, no sentence punctuation.
            letters = sum(1 for c in text if c.isalpha())
            if letters < 3 or letters / max(len(text), 1) < 0.6:
                continue
            if any(ch in text for ch in ".,;:"):
                continue
            # And require the size beneath it to be a room-sized one. A plan
            # prints plenty of paired figures that are not rooms: a timber
            # section (90 x 32) and a ceiling access hatch (600 x 600) were
            # both reported as rooms before this check existed.
            width_candidate, height_candidate = size[1]
            if min(width_candidate, height_candidate) < min_side_mm:
                continue
            if (width_candidate / 1000.0) * (height_candidate / 1000.0) < min_area_m2:
                continue
            method = "paired_dimension_below_label"
            multiplier = 0.75

        width_mm = height_mm = None
        area_m2 = None
        dimension_bbox = None
        if size is not None:
            dimension_line, (width_mm, height_mm) = size
            dimension_bbox = dimension_line["bbox"]
            area_m2 = round((width_mm / 1000.0) * (height_mm / 1000.0), 2)

        confidence = round(min(line["confidence"] * multiplier, 1.0), 3)
        band = (
            "high"
            if confidence >= thresholds["review"]
            else ("review" if confidence >= thresholds["low"] else "low")
        )
        found.append(
            {
                "room_id": "",  # assigned below, once ordering is settled
                "name": text,
                "normalized_name": keyword,
                "instance": _instance_of(text),
                "detection_method": method,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "floor_area_m2": area_m2,
                "bbox": line["bbox"],
                "dimension_bbox": dimension_bbox,
                "confidence": confidence,
                "confidence_band": band,
                "extraction_method": line["extraction_method"],
                "review_status": "confirmed" if band == "high" else "needs_review",
            }
        )

    found = _merge_stacked_labels(found)
    found = _resolve_shared_size_callouts(found)

    # Deterministic reading order, then stable IDs derived from it.
    found.sort(key=lambda r: (round(r["bbox"][1], 1), round(r["bbox"][0], 1)))
    for position, room in enumerate(found, start=1):
        room["room_id"] = f"{sheet_id}-R{position:02d}"
    return found


def _merge_stacked_labels(rooms: list) -> list:
    """Joins a room name printed across two stacked lines.

    'MASTER' above 'BEDROOM' is one room, not two. They are recognised by
    sitting in the same column, one directly under the other, with no other
    text between them — the same wrapping the title block does with long
    titles.
    """
    rooms = sorted(rooms, key=lambda r: (round(r["bbox"][0], 1), r["bbox"][1]))
    merged: list = []
    consumed = set()
    for index, room in enumerate(rooms):
        if index in consumed:
            continue
        current = room
        for other_index in range(index + 1, len(rooms)):
            if other_index in consumed:
                continue
            other = rooms[other_index]
            gap = other["bbox"][1] - current["bbox"][3]
            height = bbox_height(current["bbox"]) or 8.0
            if gap < -height * 0.4 or gap > height * 0.8:
                continue
            overlap = horizontal_overlap(current["bbox"], other["bbox"])
            narrowest = min(
                current["bbox"][2] - current["bbox"][0],
                other["bbox"][2] - other["bbox"][0],
            ) or 1.0
            if overlap / narrowest < 0.5:
                continue
            current = {
                **current,
                "name": f"{current['name']} {other['name']}".strip(),
                "normalized_name": current["normalized_name"] or other["normalized_name"],
                "instance": current["instance"] or other["instance"],
                "width_mm": current["width_mm"] or other["width_mm"],
                "height_mm": current["height_mm"] or other["height_mm"],
                "floor_area_m2": current["floor_area_m2"] or other["floor_area_m2"],
                "dimension_bbox": current["dimension_bbox"] or other["dimension_bbox"],
                "bbox": [
                    min(current["bbox"][0], other["bbox"][0]),
                    min(current["bbox"][1], other["bbox"][1]),
                    max(current["bbox"][2], other["bbox"][2]),
                    max(current["bbox"][3], other["bbox"][3]),
                ],
            }
            consumed.add(other_index)
        merged.append(current)
    return merged


def _resolve_shared_size_callouts(rooms: list) -> list:
    """One printed size callout belongs to one room.

    Where several labels sit above the same '(3,905 x 3,515)' — a room name and
    an appliance note beside it, say — the nearest label keeps the size and the
    others lose it. Reporting the same floor area twice would double-count it
    in any later take-off.
    """
    by_callout: dict = {}
    for room in rooms:
        if room.get("dimension_bbox"):
            by_callout.setdefault(tuple(room["dimension_bbox"]), []).append(room)

    for callout, claimants in by_callout.items():
        if len(claimants) < 2:
            continue
        target_x, target_y = bbox_center(list(callout))

        def distance(room):
            cx, cy = bbox_center(room["bbox"])
            return ((cx - target_x) ** 2 + (cy - target_y) ** 2) ** 0.5

        claimants.sort(key=lambda r: (r["normalized_name"] is None, distance(r)))
        for loser in claimants[1:]:
            loser["width_mm"] = None
            loser["height_mm"] = None
            loser["floor_area_m2"] = None
            loser["dimension_bbox"] = None
            if loser["detection_method"] == "paired_dimension_below_label":
                loser["detection_method"] = "dropped"
            elif loser["detection_method"] == "vocabulary_and_paired_dimension":
                loser["detection_method"] = "vocabulary"

    return [r for r in rooms if r["detection_method"] != "dropped"]


def nearest_room(rooms: list, bbox, max_distance: float):
    """The room label nearest a point, but only when it is unambiguously the
    nearest one.

    A dimension sitting between two equally close room labels genuinely cannot
    be attributed to either from the text alone. Returning the first one found
    would put a wrong room ID on a real measurement, and every later stage
    would trust it. So the nearest candidate is returned only when it is
    clearly closer than the runner-up; otherwise this returns None and the
    caller records the dimension as unlinked, which is the honest answer.
    """
    if not rooms:
        return None, None
    cx, cy = bbox_center(bbox)
    scored = []
    for room in rooms:
        rx, ry = bbox_center(room["bbox"])
        scored.append((((cx - rx) ** 2 + (cy - ry) ** 2) ** 0.5, room))
    scored.sort(key=lambda item: item[0])
    best_distance, best_room = scored[0]
    if best_distance > max_distance:
        return None, "No room label nearby."
    if len(scored) > 1 and scored[1][0] < best_distance * 1.6:
        return None, "Two room labels are equally close, so no room was assigned."
    return best_room, None
