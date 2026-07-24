# ABOUTME: Bounding-box read helper over geotagged experiences (#43).
# ABOUTME: Reads through brain.live_visible_experiences so viewer/visibility match search.
from openbrain.brain.db import brain_cursor, dictfetchall, parse_json

# Reads through the shared live+visible predicate (brain.live_visible_experiences,
# init/18) so this map read and match_brain_hybrid can never drift on who may see
# a row. The lat/lng partial btrees (init/18) serve the range scan; a null viewer
# (legacy operator / system caller) bypasses the owner/visibility filter, exactly
# as match_brain_hybrid does.
_BBOX_SQL = """
    select e.id::text as id,
           e.content,
           e.lat,
           e.lng,
           e.occurred_at,
           e.captured_at,
           e.metadata
      from brain.live_visible_experiences(%(viewer)s) e
     where e.lat is not null
       and e.lng is not null
       and e.lat between %(min_lat)s and %(max_lat)s
       and {lng_clause}
     order by e.occurred_at desc nulls last, e.captured_at desc
     limit %(limit)s
"""

# Normal viewport: a single contiguous longitude range.
_LNG_CONTIGUOUS = "e.lng between %(min_lng)s and %(max_lng)s"
# Antimeridian-crossing viewport (min_lng east of max_lng): the box wraps past
# +/-180, so it is the union of [min_lng, 180] and [-180, max_lng].
_LNG_WRAPPED = "(e.lng >= %(min_lng)s or e.lng <= %(max_lng)s)"


def crosses_antimeridian(min_lng: float, max_lng: float) -> bool:
    """True when the viewport wraps past the 180th meridian (min east of max)."""
    return min_lng > max_lng


def experiences_in_bbox(
    *,
    viewer: str | None,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    limit: int = 500,
) -> list[dict]:
    """Experiences whose lat/lng fall inside the box, newest occurrence first.

    Honors the same viewer/visibility rules as hybrid search (a member sees own
    + shared; a null viewer sees everything). Handles the antimeridian by
    splitting the longitude test into two ranges when the box crosses 180.
    """
    lng_clause = (
        _LNG_WRAPPED if crosses_antimeridian(min_lng, max_lng) else _LNG_CONTIGUOUS
    )
    sql = _BBOX_SQL.format(lng_clause=lng_clause)
    params = {
        "viewer": viewer,
        "min_lat": min_lat,
        "max_lat": max_lat,
        "min_lng": min_lng,
        "max_lng": max_lng,
        "limit": limit,
    }
    with brain_cursor() as cursor:
        cursor.execute(sql, params)
        rows = dictfetchall(cursor)
    for row in rows:
        row["metadata"] = parse_json(row["metadata"])
    return rows
