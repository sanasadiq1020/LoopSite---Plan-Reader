"""The canonical model written out as 3D files.

Three formats, each for a different reader, all generated from the same
canonical model and never from each other (Critical Rule 2):

*   **GLB** — what the browser viewer loads. Each wall is its own named node,
    so clicking one in the viewer identifies exactly which wall it is.
*   **OBJ** — plain text, opens in anything, and can be read with a text
    editor when something looks wrong.
*   **IFC** — the format the construction industry actually exchanges. This is
    what makes the model a building rather than a picture of one: an
    ``IfcWallStandardCase`` on an ``IfcBuildingStorey`` in a project measured
    in millimetres, with its doors and windows as real ``IfcOpeningElement``
    voids rather than as holes drawn into the wall's own shape.

**A wall is a box with its doors and windows taken out of it.** Its centreline
runs from start to end on the floor; it is half its thickness either side of
that line, and it rises to the storey height. Each opening cut into it removes
a rectangle from the sill to the head, over the width the schedule gives it, at
the place along the wall the plan puts it. Everything left over is solid wall,
and it is built as the pieces that remain: the full-height stretches between
openings, the sill piece under each window, and the lintel piece over each
opening. Nothing is subtracted approximately and nothing is drawn twice.

**Only what the drawings establish is cut.** An opening whose height, width,
wall or position could not be established is carried on the model with the wall
it belongs to and is not cut — the canonical model records one sentence saying
which of the four is missing. A hole in the wrong place is worse than a wall
that still says what it does not know.

**Axes.** The canonical model is X east, Y north, Z up, which is how a building
is described and what IFC expects. GLB and OBJ are conventionally Y-up, so for
those two the axes are turned — ``(x, y, z)`` becomes ``(x, z, -y)``, which
keeps the handedness. Mapping to ``(x, z, y)`` instead would mirror the whole
building, and a mirrored plan looks completely convincing.
"""

import json
import struct

from app.logging_setup import get_logger

logger = get_logger()


# --- the pieces a wall is left in once its openings are taken out ---------


def _wall_axes(wall: dict):
    """The wall's own direction, its sideways direction, and its length."""
    start = wall["geometry"]["start_mm"]
    end = wall["geometry"]["end_mm"]
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 0:
        return None
    return start, (dx / length, dy / length), (-dy / length, dx / length), length


def _piece(wall: dict, start_fraction: float, end_fraction: float, low: float, high: float):
    """The eight corners of one solid piece of a wall, in canonical millimetres."""
    axes = _wall_axes(wall)
    if axes is None:
        return None
    start, along, across, length = axes
    half = wall["dimensions"]["thickness_mm"] / 2.0

    first = (
        start[0] + along[0] * length * start_fraction,
        start[1] + along[1] * length * start_fraction,
    )
    last = (
        start[0] + along[0] * length * end_fraction,
        start[1] + along[1] * length * end_fraction,
    )
    footprint = [
        (first[0] + across[0] * half, first[1] + across[1] * half),
        (last[0] + across[0] * half, last[1] + across[1] * half),
        (last[0] - across[0] * half, last[1] - across[1] * half),
        (first[0] - across[0] * half, first[1] - across[1] * half),
    ]
    return [(x, y, low) for x, y in footprint] + [(x, y, high) for x, y in footprint]


def voids_in(wall: dict, openings: list) -> list:
    """The openings actually cut into this wall, in order along it.

    Two openings that overlap along the wall are joined into one hole. That is
    rare — it means two marks were placed at the same place — and one hole
    spanning both is a truthful picture of an uncertainty, where two
    overlapping cuts would leave a sliver of wall that exists in neither
    drawing nor schedule.
    """
    found = []
    for opening in openings:
        geometry = opening.get("geometry") or {}
        if not geometry.get("cut_as_void"):
            continue
        if opening.get("in_wall") != wall["element_id"]:
            continue
        start = geometry.get("start_fraction")
        end = geometry.get("end_fraction")
        sill = geometry.get("sill_height_mm")
        head = geometry.get("head_height_mm")
        if start is None or end is None or sill is None or head is None:
            continue
        found.append(
            {
                "start": max(0.0, min(float(start), 1.0)),
                "end": max(0.0, min(float(end), 1.0)),
                "sill": float(sill),
                "head": float(head),
            }
        )

    found.sort(key=lambda v: v["start"])
    joined = []
    for void in found:
        if joined and void["start"] <= joined[-1]["end"]:
            joined[-1]["end"] = max(joined[-1]["end"], void["end"])
            joined[-1]["sill"] = min(joined[-1]["sill"], void["sill"])
            joined[-1]["head"] = max(joined[-1]["head"], void["head"])
        else:
            joined.append(dict(void))
    return joined


def wall_pieces(wall: dict, openings: list) -> list:
    """A wall as the solid boxes left once its openings are taken out.

    With no openings this is one box, exactly as before. With openings it is
    the full-height stretches between them, plus the piece under each window
    and the lintel piece over each opening.
    """
    base = float(wall["geometry"].get("base_elevation_mm", 0.0))
    top = base + float(wall["dimensions"]["height_mm"])
    boxes = []

    cursor = 0.0
    for void in voids_in(wall, openings):
        if void["start"] > cursor:
            boxes.append(_piece(wall, cursor, void["start"], base, top))
        if void["sill"] > base:
            boxes.append(_piece(wall, void["start"], void["end"], base, void["sill"]))
        if void["head"] < top:
            boxes.append(_piece(wall, void["start"], void["end"], void["head"], top))
        cursor = max(cursor, void["end"])
    if cursor < 1.0:
        boxes.append(_piece(wall, cursor, 1.0, base, top))

    return [box for box in boxes if box is not None]


# The twelve triangles of a box, as indices into the eight corners above:
# 0-3 are the bottom face in order, 4-7 the top face directly above them.
_BOX_TRIANGLES = [
    (0, 1, 2), (0, 2, 3),          # floor
    (4, 6, 5), (4, 7, 6),          # ceiling
    (0, 4, 5), (0, 5, 1),          # side
    (1, 5, 6), (1, 6, 2),          # end
    (2, 6, 7), (2, 7, 3),          # other side
    (3, 7, 4), (3, 4, 0),          # other end
]


def _to_y_up(point):
    """Canonical (east, north, up) as the Y-up axes GLB and OBJ expect."""
    x, y, z = point
    return (x, z, -y)


# --- OBJ -----------------------------------------------------------------


def write_obj(model: dict, path) -> bool:
    """The model as Wavefront OBJ. Every wall is a named group."""
    try:
        openings = model.get("openings", [])
        lines = [
            f"# LoopSite canonical model — {model['modelled_sheet']['sheet_id']}",
            f"# {model['source_file']}  run {model['run_id']}",
            "# Millimetres. Y is up; the building's north is -Z.",
            "# Each wall is the solid pieces left once its openings are taken out.",
            "",
        ]
        offset = 1  # OBJ vertex numbering starts at 1
        for wall in model["walls"]:
            boxes = wall_pieces(wall, openings)
            if not boxes:
                continue
            lines.append(f"g {wall['element_id']}")
            for corners in boxes:
                for corner in corners:
                    x, y, z = _to_y_up(corner)
                    lines.append(f"v {x:.1f} {y:.1f} {z:.1f}")
                for a, b, c in _BOX_TRIANGLES:
                    lines.append(f"f {offset + a} {offset + b} {offset + c}")
                offset += 8
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as e:
        logger.exception(f"could not write the OBJ model: {e}")
        return False


# --- GLB -----------------------------------------------------------------


def write_glb(model: dict, path) -> bool:
    """The model as a single binary glTF file.

    Written directly rather than through a library: glTF is a small, precisely
    specified format, and a wall is a handful of boxes. Each wall becomes its
    own node named with its ``element_id``, which is what lets the viewer say
    which wall was clicked — a wall with three windows in it is still one node.
    """
    try:
        openings = model.get("openings", [])
        vertex_bytes = bytearray()
        index_bytes = bytearray()
        accessors, meshes, nodes = [], [], []

        for wall in model["walls"]:
            boxes = wall_pieces(wall, openings)
            if not boxes:
                continue

            positions = [_to_y_up(corner) for box in boxes for corner in box]
            vertex_offset = len(vertex_bytes)
            for x, y, z in positions:
                vertex_bytes += struct.pack("<fff", x, y, z)

            index_offset = len(index_bytes)
            for box_number in range(len(boxes)):
                base = box_number * 8
                for triangle in _BOX_TRIANGLES:
                    index_bytes += struct.pack(
                        "<HHH", *(base + corner for corner in triangle)
                    )

            position_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": 0,
                    "byteOffset": vertex_offset,
                    "componentType": 5126,  # float
                    "count": len(positions),
                    "type": "VEC3",
                    "min": [min(p[i] for p in positions) for i in range(3)],
                    "max": [max(p[i] for p in positions) for i in range(3)],
                }
            )
            index_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": 1,
                    "byteOffset": index_offset,
                    "componentType": 5123,  # unsigned short
                    "count": len(boxes) * len(_BOX_TRIANGLES) * 3,
                    "type": "SCALAR",
                }
            )

            meshes.append(
                {
                    "name": wall["element_id"],
                    "primitives": [
                        {
                            "attributes": {"POSITION": position_accessor},
                            "indices": index_accessor,
                            "material": 0,
                        }
                    ],
                }
            )
            nodes.append({"name": wall["element_id"], "mesh": len(meshes) - 1})

        if not meshes:
            logger.warning("no wall had usable geometry, so no GLB was written")
            return False

        # Each buffer view must start on a four-byte boundary.
        while len(vertex_bytes) % 4:
            vertex_bytes.append(0)
        binary = bytes(vertex_bytes) + bytes(index_bytes)
        while len(binary) % 4:
            binary += b"\x00"

        gltf = {
            "asset": {
                "version": "2.0",
                "generator": (
                    f"LoopSite canonical model — {model['modelled_sheet']['sheet_id']} "
                    f"from {model['source_file']}"
                ),
            },
            "scene": 0,
            "scenes": [{"nodes": list(range(len(nodes)))}],
            "nodes": nodes,
            "meshes": meshes,
            "materials": [
                {
                    "name": "wall",
                    "pbrMetallicRoughness": {
                        "baseColorFactor": [0.82, 0.84, 0.88, 1.0],
                        "metallicFactor": 0.0,
                        "roughnessFactor": 0.85,
                    },
                    "doubleSided": True,
                }
            ],
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": len(vertex_bytes), "target": 34962},
                {
                    "buffer": 0,
                    "byteOffset": len(vertex_bytes),
                    "byteLength": len(index_bytes),
                    "target": 34963,
                },
            ],
            "accessors": accessors,
        }

        json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        while len(json_bytes) % 4:
            json_bytes += b" "  # the JSON chunk is padded with spaces, not zeros

        total = 12 + 8 + len(json_bytes) + 8 + len(binary)
        with path.open("wb") as handle:
            handle.write(b"glTF")
            handle.write(struct.pack("<II", 2, total))
            handle.write(struct.pack("<I", len(json_bytes)))
            handle.write(b"JSON")
            handle.write(json_bytes)
            handle.write(struct.pack("<I", len(binary)))
            handle.write(b"BIN\x00")
            handle.write(binary)
        return True
    except Exception as e:
        logger.exception(f"could not write the GLB model: {e}")
        return False


# --- IFC -----------------------------------------------------------------


def _polyline(ifc, points):
    return ifc.create_entity(
        "IfcPolyline",
        Points=[
            ifc.create_entity("IfcCartesianPoint", Coordinates=(round(x, 2), round(y, 2)))
            for x, y in list(points) + [points[0]]
        ],
    )


def _extrusion(ifc, footprint, base: float, height: float):
    return ifc.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=ifc.create_entity(
            "IfcArbitraryClosedProfileDef",
            ProfileType="AREA",
            OuterCurve=_polyline(ifc, footprint),
        ),
        Position=ifc.create_entity(
            "IfcAxis2Placement3D",
            Location=ifc.create_entity(
                "IfcCartesianPoint", Coordinates=(0.0, 0.0, round(base, 2))
            ),
        ),
        ExtrudedDirection=ifc.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=round(float(height), 2),
    )


def _shape(ifc, body, solid):
    return ifc.create_entity(
        "IfcProductDefinitionShape",
        Representations=[
            ifc.create_entity(
                "IfcShapeRepresentation",
                ContextOfItems=body,
                RepresentationIdentifier="Body",
                RepresentationType="SweptSolid",
                Items=[solid],
            )
        ],
    )


def write_ifc(model: dict, path) -> bool:
    """The model as IFC4 — a building, not a picture of one.

    A project, a site, a building, one storey, a wall for each wall, and for
    each opening a real ``IfcOpeningElement`` related to its wall by
    ``IfcRelVoidsElement``. That is how the industry exchanges a door: the wall
    keeps its own full shape and the opening states what it removes, so any
    receiving application can see both the wall that was measured and the hole
    that was cut in it.
    """
    try:
        import ifcopenshell
        import ifcopenshell.api.aggregate
        import ifcopenshell.api.context
        import ifcopenshell.api.feature
        import ifcopenshell.api.root
        import ifcopenshell.api.spatial
        import ifcopenshell.api.unit
    except ImportError as e:
        logger.warning(f"IfcOpenShell is not available, so no IFC was written: {e}")
        return False

    try:
        ifc = ifcopenshell.file(schema="IFC4")
        project = ifcopenshell.api.root.create_entity(
            ifc, ifc_class="IfcProject", name=model["source_file"] or "LoopSite project"
        )
        # Millimetres, because the canonical model is in millimetres and a
        # silent unit change is the failure Week 1 names first.
        ifcopenshell.api.unit.assign_unit(
            ifc, length={"is_metric": True, "raw": "MILLIMETERS"}
        )
        ifcopenshell.api.context.add_context(ifc, context_type="Model")
        body = ifcopenshell.api.context.add_context(
            ifc,
            context_type="Model",
            context_identifier="Body",
            target_view="MODEL_VIEW",
            parent=ifc.by_type("IfcGeometricRepresentationContext")[0],
        )

        site = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcSite", name="Site")
        building = ifcopenshell.api.root.create_entity(
            ifc, ifc_class="IfcBuilding", name=model["modelled_sheet"]["sheet_title"] or "Building"
        )
        storey_record = model["storeys"][0]
        storey = ifcopenshell.api.root.create_entity(
            ifc, ifc_class="IfcBuildingStorey", name=storey_record["name"]
        )
        # Project contains site contains building contains storey — the
        # spatial chain every IFC reader expects to walk down.
        ifcopenshell.api.aggregate.assign_object(ifc, products=[site], relating_object=project)
        ifcopenshell.api.aggregate.assign_object(ifc, products=[building], relating_object=site)
        ifcopenshell.api.aggregate.assign_object(ifc, products=[storey], relating_object=building)

        openings = model.get("openings", [])
        written = cut = 0
        for wall_record in model["walls"]:
            axes = _wall_axes(wall_record)
            if axes is None:
                continue
            base = float(wall_record["geometry"].get("base_elevation_mm", 0.0))
            footprint = [(x, y) for x, y, _ in (_piece(wall_record, 0.0, 1.0, 0.0, 1.0) or [])[:4]]
            if not footprint:
                continue

            wall = ifcopenshell.api.root.create_entity(
                ifc, ifc_class="IfcWallStandardCase", name=wall_record["element_id"]
            )
            wall.Representation = _shape(
                ifc,
                body,
                _extrusion(ifc, footprint, base, wall_record["dimensions"]["height_mm"]),
            )
            ifcopenshell.api.spatial.assign_container(
                ifc, products=[wall], relating_structure=storey
            )
            written += 1

            for opening_record in openings:
                geometry = opening_record.get("geometry") or {}
                if not geometry.get("cut_as_void"):
                    continue
                if opening_record.get("in_wall") != wall_record["element_id"]:
                    continue
                # The void runs right through both faces of the wall, so its
                # footprint is stretched a little past each of them: a solid
                # that stops exactly on a face leaves a paper-thin skin behind
                # in some receiving applications.
                corners = _piece(
                    wall_record,
                    geometry["start_fraction"],
                    geometry["end_fraction"],
                    0.0,
                    1.0,
                )
                if corners is None:
                    continue
                void_footprint = _widen(
                    [(x, y) for x, y, _ in corners[:4]], axes[2], wall_record
                )
                sill = float(geometry["sill_height_mm"])
                head = float(geometry["head_height_mm"])
                opening = ifcopenshell.api.root.create_entity(
                    ifc,
                    ifc_class="IfcOpeningElement",
                    name=opening_record.get("mark") or opening_record["element_id"],
                )
                opening.Representation = _shape(
                    ifc, body, _extrusion(ifc, void_footprint, base + sill, head - sill)
                )
                ifcopenshell.api.feature.add_feature(ifc, feature=opening, element=wall)
                cut += 1

        ifc.write(str(path))
        logger.info(f"IFC written with {written} walls and {cut} openings cut into them")
        return written > 0
    except Exception as e:
        logger.exception(f"could not write the IFC model: {e}")
        return False


def _widen(footprint, across, wall_record: dict, margin_mm: float = 10.0):
    """A void's footprint pushed a little past both faces of its wall."""
    centre_x = sum(x for x, _ in footprint) / len(footprint)
    centre_y = sum(y for _, y in footprint) / len(footprint)
    widened = []
    for x, y in footprint:
        side = 1.0 if ((x - centre_x) * across[0] + (y - centre_y) * across[1]) >= 0 else -1.0
        widened.append((x + across[0] * margin_mm * side, y + across[1] * margin_mm * side))
    return widened
