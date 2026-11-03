"""Shared pytest fixtures and network-test deselection."""

import pathlib
import sys

import pytest

# Make the repo root importable regardless of where pytest is invoked from.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tickets import store  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="also run tests marked as network (they hit the live Mercadona API)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test hits the live network")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    # Deselect (not merely skip) network tests so offline runs stay clean.
    items[:] = [item for item in items if "network" not in item.keywords]


@pytest.fixture
def conn(tmp_path):
    connection = store.init_db(tmp_path / "tickets-test.db")
    yield connection
    connection.close()
