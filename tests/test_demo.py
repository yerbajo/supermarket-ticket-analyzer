"""Demo database test: the synthetic seed keeps the contract its screens rely on.

``scripts.demo.build`` invents every ticket, so this test is the one place that
pins what the demo actually contains: six tickets, three chains, and repeated
products whose price-change lists have something to show.
"""

import sqlite3

from scripts import demo


def _scalar(path, sql, *params):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchone()[0]
    finally:
        connection.close()


def test_build_writes_the_demo_database(tmp_path):
    path = demo.build(tmp_path / "demo.db")

    assert path.exists()
    assert _scalar(path, "SELECT COUNT(*) FROM tickets") == 6
    # 3+3+3+4+3+3 lines in scripts.demo.TICKETS.
    assert _scalar(path, "SELECT COUNT(*) FROM ticket_lines") == 19

    connection = sqlite3.connect(path)
    try:
        chains = {row[0] for row in connection.execute("SELECT DISTINCT chain FROM tickets")}
        observations = connection.execute(
            "SELECT COUNT(*) FROM price_observations o JOIN products p ON p.id = o.product_id "
            "WHERE p.chain = 'Mercadona' AND p.external_id = 'M-100'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert {"Mercadona", "Dia", "MAS"} <= chains
    assert observations == 2

    # Rebuilding over an existing file replaces it instead of adding to it.
    demo.build(path)
    assert _scalar(path, "SELECT COUNT(*) FROM tickets") == 6
