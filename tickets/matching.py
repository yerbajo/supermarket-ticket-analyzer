"""Match a ticket line to a catalogue product.

Resolution order:

1. A human-confirmed alias wins outright (``score = 1.0``, ``matched_by =
   "alias"``).
2. Otherwise the line is fuzzy-matched against the catalogue of the *same
   chain only*. Both sides are compared through :func:`normalize_text` with a
   documented 0-100 blend: ``0.6 * token_set_ratio + 0.4 * partial_ratio``.
3. A size guard rejects a candidate whose size conflicts with the line: both
   sides must expose a size (via ``units.extract_size``) and either the base
   unit differs or the amounts differ by more than :data:`SIZE_TOLERANCE`
   relative. A ``1,5 l`` line therefore never matches a ``1 l`` product. When
   only one side exposes a size the candidate is neither rejected nor rewarded.
4. Anything below ``threshold`` is dropped: the engine never invents a match.
"""

from __future__ import annotations

import unicodedata

from rapidfuzz import fuzz

from tickets import store
from tickets.units import extract_size

# Relative difference above which two same-unit sizes count as different.
SIZE_TOLERANCE = 0.10


def normalize_text(text) -> str:
    """Upper-case, strip accents and punctuation, drop stray tokens.

    ``"Leche (entera) 1,5L"`` -> ``"LECHE ENTERA 1 5L"``. Deterministic and
    idempotent; empty or ``None`` input returns ``""``.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Every remaining non-alphanumeric character separates tokens; ``split()``
    # then drops punctuation-only and empty fragments ("stray tokens").
    cleaned = "".join(ch if ch.isalnum() else " " for ch in without_accents)
    return " ".join(cleaned.split()).upper()


def _size_text(row) -> str:
    """Name plus format, the two product fields that may carry a size."""
    return " ".join(part for part in (row["name"], row["format"]) if part)


def blended_score(query: str, choice: str) -> float:
    """Documented 0-100 blend: 60% token-set ratio, 40% partial ratio."""
    return 0.6 * fuzz.token_set_ratio(query, choice) + 0.4 * fuzz.partial_ratio(query, choice)


#: Kept so older callers keep working; new code should use ``blended_score``.
_blended_score = blended_score


def _sizes_conflict(line_size, candidate_size) -> bool:
    """True when both sides expose a size and they are materially different."""
    line_value, line_unit = line_size
    candidate_value, candidate_unit = candidate_size
    if line_value is None or candidate_value is None:
        return False  # only one side has a size: neither reject nor reward
    if line_unit != candidate_unit:
        return True
    reference = max(line_value, candidate_value)
    if reference <= 0:
        return True
    return abs(line_value - candidate_value) / reference > SIZE_TOLERANCE


class _Catalogue:
    """Per-(conn, chain) cache of normalised candidates for a matching run.

    Reading and normalising the catalogue happens once; every line in the run
    is then scored against the same list.
    """

    def __init__(self, conn, chain):
        self.conn = conn
        self.chain = chain
        self._candidates = None

    def candidates(self):
        if self._candidates is None:
            self._candidates = [
                {
                    "product_id": row["id"],
                    "name": row["name"],
                    "norm": normalize_text(row["name"]),
                    "size": extract_size(_size_text(row)),
                    "unit_price": row["unit_price"],
                    "unit_label": row["unit_label"],
                }
                for row in store.list_products(self.conn, chain=self.chain)
            ]
        return self._candidates


def _match_result(product_id, name, unit_price, unit_label, score, matched_by) -> dict:
    return {
        "product_id": product_id,
        "score": score,
        "matched_by": matched_by,
        "name": name,
        "unit_price": unit_price,
        "unit_label": unit_label,
    }


def _resolve(conn, chain, raw_text, threshold, limit, catalogue):
    """Best match for one line, or ``None``. See module docstring for the rules."""
    alias_id = store.resolve_alias(conn, chain, raw_text)
    if alias_id is not None:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (alias_id,)).fetchone()
        if row is not None:
            return _match_result(
                row["id"], row["name"], row["unit_price"], row["unit_label"], 1.0, "alias"
            )

    query = normalize_text(raw_text)
    if not query:
        return None
    line_size = extract_size(raw_text)
    scored = sorted(
        ((_blended_score(query, cand["norm"]), cand) for cand in catalogue.candidates()),
        key=lambda item: item[0],
        reverse=True,
    )
    if limit is not None:
        scored = scored[:limit]

    for score, cand in scored:
        if score < threshold:
            break
        if _sizes_conflict(line_size, cand["size"]):
            continue
        return _match_result(
            cand["product_id"], cand["name"], cand["unit_price"], cand["unit_label"],
            round(score, 2), "fuzzy",
        )
    return None


def match_line(conn, chain, raw_text, threshold=82, limit=5):
    """Return the best catalogue match for ``raw_text``, or ``None``.

    ``threshold`` is the 0-100 minimum blended score; ``limit`` caps how many
    of the highest-scoring candidates are considered (a size conflict skips to
    the next one). ``limit=None`` scores the whole catalogue.
    """
    catalogue = _Catalogue(conn, chain)
    return _resolve(conn, chain, raw_text, threshold, limit, catalogue)


def match_ticket_lines(conn, chain, ticket_id=None, threshold=82) -> dict:
    """Resolve the still-unmatched ticket lines, writing the match back.

    Walks the ``ticket_lines`` rows of ``chain`` (and ``ticket_id`` when given)
    whose ``product_id`` is NULL, resolves each one and writes ``product_id``,
    ``match_confidence`` and ``matched_by``. Rows below ``threshold`` keep
    ``product_id`` NULL. Returns ``{"lines", "matched", "unmatched",
    "from_alias"}``.
    """
    catalogue = _Catalogue(conn, chain)
    # A line that carries the shop's own product code is never guessed by name: the
    # code is exact, so if it did not resolve, the product is not in our catalogue
    # and any name match would be a *different* product (a 16-pack read as a 4-pack,
    # measured on a real ticket). Those lines stay unmatched on purpose.
    sql = (
        "SELECT tl.id AS line_id, tl.raw_text AS raw_text "
        "FROM ticket_lines tl JOIN tickets t ON t.id = tl.ticket_id "
        "WHERE t.chain = ? AND tl.product_id IS NULL "
        "AND (tl.shop_code IS NULL OR tl.shop_code = '')"
    )
    params = [chain]
    if ticket_id is not None:
        sql += " AND tl.ticket_id = ?"
        params.append(ticket_id)
    sql += " ORDER BY tl.id"
    rows = conn.execute(sql, params).fetchall()

    matched = 0
    from_alias = 0
    for row in rows:
        result = _resolve(conn, chain, row["raw_text"], threshold, None, catalogue)
        if result is None:
            continue
        conn.execute(
            "UPDATE ticket_lines SET product_id = ?, match_confidence = ?, matched_by = ? "
            "WHERE id = ?",
            (result["product_id"], result["score"], result["matched_by"], row["line_id"]),
        )
        matched += 1
        if result["matched_by"] == "alias":
            from_alias += 1
    conn.commit()

    lines = len(rows)
    return {
        "lines": lines,
        "matched": matched,
        "unmatched": lines - matched,
        "from_alias": from_alias,
    }


def confirm_alias(conn, chain, raw_text, product_id, confirmed_by="human") -> int:
    """Persist a human-confirmed match so the same line resolves instantly."""
    return store.add_alias(conn, chain, raw_text, product_id, confirmed_by=confirmed_by)
