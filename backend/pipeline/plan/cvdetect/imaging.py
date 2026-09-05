"""Turning a sheet into the images the rest of the pipeline measures.

Two ways a plan reaches an image, and the difference matters:

*   **From the drawing's own paths.** Where the sheet stores its geometry as
    vector line work, the mask is *drawn* from the paths Step 1 kept - each one
    at the weight the office plotted it. Nothing that Step 1 set aside is ever
    drawn, so a dashed roof line and a light annotation line are not merely
    ignored downstream, they are not in the picture at all. This is what makes
    "filter the noise before rendering the mask" mean something: once a
    dimension line is black pixels, no morphology can tell it from a wall face.
*   **From the page as a picture.** A plan set can be published as embedded
    images - one of the sets in use is - and then there are no paths to draw.
    The page is rendered and thresholded instead, and every measurement taken
    from it records that it came from pixels rather than from the drawing.

**Resolution is a promise the machine has to keep.** Drawing sizes are not
bounded: an A0 sheet at 300 DPI is 140 megapixels, and on a small server that
render is what ends the run - and a run that ends part way through returns the
same empty result as a sheet with nothing on it. So the resolution is reduced,
never the sheet cropped, until the image fits ``max_megapixels``, and the
reduction is logged and carried on the scale so that every distance derived
from it stays correct.
"""

import math

import fitz
import numpy as np

from app.logging_setup import get_logger
from pipeline.plan.cvdetect.settings import POINTS_PER_INCH, number

logger = get_logger()

# Ink is dark on a drawing. Anything below this on a 0-255 grey page is a
# plotted line; above it is paper. A drawing is plotted in near-black on white,
# so this is not a value that needs tuning per sheet - and the alternative,
# Otsu over the whole page, is dragged about by a large photograph or a solid
# title block.
_INK_BELOW = 200


def choose_dpi(page, settings: dict) -> float:
    """The highest resolution this sheet can be rendered at without ending the run.

    Reduced, never cropped: cropping loses part of the drawing silently, while
    a lower resolution loses precision that the run can state.
    """
    wanted = number(settings, "render_dpi", 300.0)
    ceiling = number(settings, "max_megapixels", 40.0)
    try:
        width_pt, height_pt = page.rect.width, page.rect.height
    except Exception:
        return wanted
    if width_pt <= 0 or height_pt <= 0 or ceiling <= 0:
        return wanted

    megapixels = (width_pt * height_pt * (wanted / POINTS_PER_INCH) ** 2) / 1e6
    if megapixels <= ceiling:
        return wanted
    reduced = wanted * math.sqrt(ceiling / megapixels)
    logger.info(
        f"this sheet is {width_pt:.0f} x {height_pt:.0f} pt, which is {megapixels:.0f} "
        f"megapixels at {wanted:.0f} DPI; rendering at {reduced:.0f} DPI instead so the "
        "reading fits in memory"
    )
    return reduced


def image_size(page, scale) -> tuple:
    """The rendered page's size in pixels, as (height, width) for numpy."""
    zoom = scale.pixels_per_point
    return (
        max(1, int(round(page.rect.height * zoom))),
        max(1, int(round(page.rect.width * zoom))),
    )


def render_page(page, scale) -> np.ndarray:
    """The page as a greyscale image, at the calibrated resolution.

    Rendered through ``get_pixmap``, which draws the page *as it displays* -
    the same space Step 1's paths are turned into - so a sheet carrying its own
    90 degree rotation gives text, line work and pixels in one coordinate
    system and nothing downstream has to know the page was rotated.
    """
    zoom = scale.pixels_per_point
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    # OpenCV writes into some of these; it needs an array it owns rather than
    # a read-only view onto the pixmap's buffer.
    image = np.ascontiguousarray(image)

    # **One canonical size for every image of this page.** ``get_pixmap``
    # rounds the page rectangle its own way, and a mask drawn from the page's
    # own coordinates rounds it another - so the two can differ by a single
    # pixel. That single pixel is not cosmetic: combining them then raises
    # "sizes of input arguments do not match", the sheet is logged as
    # unreadable, and a plan set published as pictures reports no walls at all.
    # Padded with paper rather than cropped, so nothing drawn is ever lost.
    wanted = image_size(page, scale)
    if image.shape[:2] != wanted:
        fitted = np.full(wanted, 255, dtype=np.uint8)
        rows = min(wanted[0], image.shape[0])
        columns = min(wanted[1], image.shape[1])
        fitted[:rows, :columns] = image[:rows, :columns]
        image = fitted
    return image


def ink_from_page(page, scale) -> np.ndarray:
    """A binary image of a rendered sheet: 255 where there is ink."""
    import cv2

    grey = render_page(page, scale)
    _threshold, binary = cv2.threshold(grey, _INK_BELOW, 255, cv2.THRESH_BINARY_INV)
    return binary


def ink_from_paths(page, scale, segments: list, fills: list = None) -> np.ndarray:
    """A binary image drawn from the paths Step 1 kept, and only those.

    Each segment is drawn at the weight the office plotted it - at least one
    pixel, because a line plotted at 0.17 pt is a quarter of a pixel at 300 DPI
    and a line nobody can see is a line nobody can measure.

    Solid fills are drawn as well, because an Australian plan very often blacks
    in its walls rather than outlining them, and a wall drawn as a filled
    rectangle has no faces to pair at all.
    """
    import cv2

    height, width = image_size(page, scale)
    canvas = np.zeros((height, width), dtype=np.uint8)
    zoom = scale.pixels_per_point

    for segment in segments:
        try:
            x0, y0 = scale.point_to_pixel(segment.x0, segment.y0)
            x1, y1 = scale.point_to_pixel(segment.x1, segment.y1)
            thickness = max(1, int(round(segment.width * zoom)))
            cv2.line(
                canvas,
                (int(round(x0)), int(round(y0))),
                (int(round(x1)), int(round(y1))),
                255,
                thickness,
                lineType=cv2.LINE_8,
            )
        except Exception:
            # One unplottable segment must not cost the sheet.
            continue

    for fill in fills or []:
        try:
            x0, y0 = scale.point_to_pixel(fill["bbox"][0], fill["bbox"][1])
            x1, y1 = scale.point_to_pixel(fill["bbox"][2], fill["bbox"][3])
            cv2.rectangle(
                canvas,
                (int(round(x0)), int(round(y0))),
                (int(round(x1)), int(round(y1))),
                255,
                -1,
            )
        except Exception:
            continue

    return canvas


def page_is_a_picture(page, min_share: float = 0.1) -> bool:
    """Whether this sheet's drawing is an embedded image rather than line work.

    A real Australian plan set can be published this way - one of the sets in
    use covers about a third of each sheet with embedded images and holds only
    its frame and title block as drawn paths. Asked directly, the page says so,
    and it costs one scan rather than a render.

    ``get_image_info`` returns every picture's position from one pass;
    ``get_image_rects`` scans the whole page again for each image it is asked
    about, which is how a sheet carrying ten pictures came to be scanned ten
    times.
    """
    try:
        sheet_area = max(page.rect.width * page.rect.height, 1.0)
        covered = 0.0
        for image in page.get_image_info() or []:
            box = image.get("bbox")
            if not box:
                continue
            covered += abs(box[2] - box[0]) * abs(box[3] - box[1])
        return covered / sheet_area >= min_share
    except Exception as e:
        logger.exception(f"page_is_a_picture: could not be answered for this sheet: {e}")
        return False


def draw_mask(page, scale, boxes: list, padding_px: float = 0.0) -> np.ndarray:
    """A mask with the given page-space boxes filled in.

    Used for the openings mask, which is what Step 3 hands to Step 4 so that a
    door gap is not bridged shut before anything has looked at it.
    """
    import cv2

    height, width = image_size(page, scale)
    mask = np.zeros((height, width), dtype=np.uint8)
    pad = int(round(max(padding_px, 0.0)))
    for box in boxes:
        try:
            x0, y0 = scale.point_to_pixel(box[0], box[1])
            x1, y1 = scale.point_to_pixel(box[2], box[3])
            cv2.rectangle(
                mask,
                (int(round(min(x0, x1))) - pad, int(round(min(y0, y1))) - pad),
                (int(round(max(x0, x1))) + pad, int(round(max(y0, y1))) + pad),
                255,
                -1,
            )
        except Exception:
            continue
    return mask
