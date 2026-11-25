"""Library quality policy: which titles are kept at archival quality and which are kept efficient.

Pure functions, no state. A title is ``archival`` when the owner's override says so, else when
it is one of their AniList favourites or their list score is at least 9.0; every other title
is ``efficient``. An override of ``efficient`` beats the automatic rules. The reasons that led
to the decision are returned so a report can show them.

Efficient band (per episode, MB = 10^6 bytes): target 250, accepted 120 to 450. Archival has no
size cap. The band is a judgement about disk use, never about whether a release is good.
"""

POLICIES = ("archival", "efficient")
EFFICIENT = {"target_mb": 250, "min_mb": 120, "max_mb": 450}
ARCHIVAL = {"cap": None}
SCORE_ARCHIVAL = 9.0


def classify(anilist_id, *, favourites=(), score=None, overrides=None) -> tuple[str, list[str]]:
    """``(policy, reasons)`` for one title.

    ``favourites`` is a collection of AniList IDs, ``score`` the owner's list score (0..10) or
    None, ``overrides`` a mapping of AniList ID to ``"archival"`` or ``"efficient"``.
    A title without an AniList ID can only be ``efficient`` (unless a caller pins it elsewhere).
    """
    if anilist_id is None:
        return "efficient", ["default"]
    tag = (overrides or {}).get(anilist_id)
    if tag in POLICIES:
        return tag, ["override_" + tag]
    reasons = []
    if anilist_id in favourites:
        reasons.append("favourite")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= SCORE_ARCHIVAL:
        reasons.append("score_9_plus")
    return ("archival", reasons) if reasons else ("efficient", ["default"])


def band_position(mb) -> str | None:
    """``below``, ``within`` or ``above`` the efficient band for a size in MB; None if unknown."""
    if not isinstance(mb, (int, float)) or isinstance(mb, bool):
        return None
    if mb < EFFICIENT["min_mb"]:
        return "below"
    return "above" if mb > EFFICIENT["max_mb"] else "within"
