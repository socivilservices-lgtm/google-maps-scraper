"""Fixture locations shared by unit tests."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_PLACE = ROOT / "testdata" / "raw.json"
REVIEW_DIR = ROOT / "gmaps" / "testdata"
