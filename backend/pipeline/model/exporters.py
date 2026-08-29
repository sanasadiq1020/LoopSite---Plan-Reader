"""Day 5 — the canonical model written out as 3D files.

Three formats, each for a different reader, all generated from the same
canonical model and never from each other (Critical Rule 2):

*   **GLB** — what the browser viewer loads. Each wall is its own named node,
    so clicking one in the viewer identifies exactly which wall it is.
*   **OBJ** — plain text, opens in anything, and can be read with a text
    editor when something looks wrong.
*   **IFC** — the format the construction industry actually exchanges. This is
    what makes the model a building rather than a picture of one: an
    ``IfcWallStandardCase`` on an ``IfcBuildingStorey`` in a project measured
    in millimetres.

**A wall is a box.** Its centreline runs from start to end on the floor; it is
half its thickness either side of that line, and it rises to the storey height.
That is the whole of Day 5's geometry — openings are cut on Day 6.

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


def _wall_corners(wall: dict):
    """The eight corners of one wall, in canonical millimetres."""
    start = wall["geometry"]["start_mm"]
    end = wall["geometry"]["end_mm"]
    thickness = wall["dimensions"]["thickness_mm"]
    height = wall["dimensions"]["height_mm"]
    base = wall["geometry"].get("base_elevation_mm", 0.0)

    dx, dy = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 0:
        return None
    # The wall's own sideways direction: at right angles to its centreline.
    nx, ny = -dy / length, dx / length
    half = thickness / 2.0

    footprint = [
        (start[0] + nx * half, start[1] + ny * half),
        (end[0] + nx * half, end[1] + ny * half),
        (end[0] - nx * half, end[1] - ny * half),
        (start[0] - nx * half, start[1] - ny * half),
    ]
    bottom = [(x, y, base) for x, y in footprint]
    top = [(x, y, base + height) for x, y in footprint]
    return bottom + top


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
        lines = [
            f"# LoopSite canonical model — {model['modelled_sheet']['sheet_id']}",
            f"# {model['source_file']}  run {model['run_id']}",
            "# Millimetres. Y is up; the building's north is -Z.",
            "",
        ]
        offset = 1  # OBJ vertex numbering starts at 1
        for wall in model["walls"]:
            corners = _wall_corners(wall)
            if corners is None:
                continue
            lines.append(f"g {wall['element_id']}")
            for corner in corners:
                x, y, z = _to_y_up(corner)
                lines.append(f"v {x:.1f} {y:.1f} {z:.1f}")
            for a, b, c in _BOX_TRIANGLES:
                lines.append(f"f {offset + a} {offset + b} {offset + c}")
            lines.append("")
            offset += 8
        path.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as e:
        logger.exception(f"could not write the OBJ model: {e}")
        return False


# --- GLB -----------------------------------------------------------------


def write_glb(model: dict, path) -> bool:
    """The model as a single binary glTF file.

    Written directly rather than through a library: glTF is a small, precisely
    specified format, and one wall as a box is the simplest thing it can hold.
    Each wall becomes its own node named with its ``element_id``, which is what
    lets the viewer say which wall was clicked.
    """
    try:
        vertex_bytes = bytearray()
        index_bytes = bytearray()
        accessors, meshes, nodes = [], [], []

        for wall in model["walls"]:
            corners = _wall_corners(wall)
            if corners is None:
                continue

            positions = [_to_y_up(corner) for corner in corners]
            vertex_offset = len(vertex_bytes)
            for x, y, z in positions:
                vertex_bytes += struct.pack("<fff", x, y, z)

            index_offset = len(index_bytes)
            for triangle in _BOX_TRIANGLES:
                index_bytes += struct.pack("<HHH", *triangle)

            position_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": 0,
                    "byteOffset": vertex_offset,
                    "componentType": 5126,  # float
                    "count": 8,
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
                    "count": len(_BOX_TRIANGLES) * 3,
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


def write_ifc(model: dict, path) -> bool:
    """The model as IFC4 — a building, not a picture of one.

    Kept deliberately plain: a project, a site, a building, one storey and a
    wall for each wall, each an extruded rectangle placed on its centreline.
    That is what Week 1 asks for, and it is what Week 2 will cut openings into.
    """
    try:
        import ifcopenshell
        import ifcopenshell.api.aggregate
        import ifcopenshell.api.context
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
        ifcopenshell.api.aggregate.assign_object(
            ifc, products=[site], relating_object=project
        )
        ifcopenshell.api.aggregate.assign_object(
            ifc, products=[building], relating_object=site
        )
        ifcopenshell.api.aggregate.assign_object(
            ifc, products=[storey], relating_object=building
        )

        written = 0
        for wall_record in model["walls"]:
            corners = _wall_corners(wall_record)
            if corners is None:
                continue
            wall = ifcopenshell.api.root.create_entity(
                ifc, ifc_class="IfcWallStandardCase", name=wall_record["element_id"]
            )
            footprint = [(round(x, 2), round(y, 2)) for x, y, _ in corners[:4]]
            profile = ifc.create_entity(
                "IfcArbitraryClosedProfileDef",
                ProfileType="AREA",
                OuterCurve=ifc.create_entity(
                    "IfcPolyline",
                    Points=[
                        ifc.create_entity("IfcCartesianPoint", Coordinates=point)
                        for point in footprint + [footprint[0]]
                    ],
                ),
            )
            solid = ifc.create_entity(
                "IfcExtrudedAreaSolid",
                SweptArea=profile,
                Position=ifc.create_entity(
                    "IfcAxis2Placement3D",
                    Location=ifc.create_entity(
                        "IfcCartesianPoint",
                        Coordinates=(0.0, 0.0, wall_record["geometry"].get("base_elevation_mm", 0.0)),
                    ),
                ),
                ExtrudedDirection=ifc.create_entity(
                    "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
                ),
                Depth=float(wall_record["dimensions"]["height_mm"]),
            )
            wall.Representation = ifc.create_entity(
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
            ifcopenshell.api.spatial.assign_container(
                ifc, products=[wall], relating_structure=storey
            )
            written += 1

        ifc.write(str(path))
        logger.info(f"IFC written with {written} walls")
        return written > 0
    except Exception as e:
        logger.exception(f"could not write the IFC model: {e}")
        return False
