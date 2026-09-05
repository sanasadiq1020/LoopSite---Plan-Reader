"""Structural walls, doors, windows and scale, read from an Australian residential plan.

A self-contained computer-vision reader for AS 1100.301 drawings, built on
PyMuPDF, OpenCV, NumPy and Shapely and nothing else - no external service, no
paid API, no trained model. The five steps run in this order and the order is
the design:

1.  ``vectorpaths``  - the drawing's own paths, with dashed and lightly plotted
    line work set aside **before** any image is made of it.
2.  ``calibration``  - what one point of this sheet measures, taken off the
    sheet's own printed dimension figures rather than off its title block.
3.  ``openings``     - doors and windows found first, and painted out of the
    plan image so that closing a wall's gaps cannot seal a door shut.
4.  ``wallgeometry`` - wall bands, their outlines by contour and their
    centrelines by skeletonisation, with the thickness measured by distance
    transform.
5.  ``crosslink``    - which wall each opening is in, or why that could not be
    said.

**No distance is written into this code.** Every threshold is stated in
millimetres of building or points of paper in ``config/cv_detection.json``, and
turned into pixels through the scale measured off each individual sheet - so
the same reader works on a 1:50 detail and a 1:200 site plan without being
retuned (Critical Rule 1).

Typical use::

    from pipeline.plan.cvdetect import read_document, read_sheet

    result = read_document("input/plan.pdf")
    for sheet in result["sheets"]:
        print(sheet["sheet_name"], sheet["summary"])
"""

from pipeline.plan.cvdetect.calibration import calibrate_scale
from pipeline.plan.cvdetect.crosslink import link_openings_to_walls
from pipeline.plan.cvdetect.detector import SheetReading, read_document, read_sheet
from pipeline.plan.cvdetect.openings import Opening, detect_openings
from pipeline.plan.cvdetect.settings import Scale, load_settings
from pipeline.plan.cvdetect.vectorpaths import VectorPaths, parse_paths
from pipeline.plan.cvdetect.wallgeometry import Wall, detect_walls

__all__ = [
    "read_document",
    "read_sheet",
    "SheetReading",
    "calibrate_scale",
    "Scale",
    "load_settings",
    "parse_paths",
    "VectorPaths",
    "detect_openings",
    "Opening",
    "detect_walls",
    "Wall",
    "link_openings_to_walls",
]
