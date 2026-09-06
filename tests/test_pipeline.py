"""
Tests for the decisions that are easy to break silently.

Note what is tested: not "does it run" but "does it make the RIGHT choice".
Every one of these corresponds to a bug that actually occurred during
development, which is the only good reason to write a test.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.files import load_config
from src.pii.masker import Masker
from src.transformations.cleaning import Cleaner
from src.transformations.validation import Validator


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "schema.yaml")


# --- the float-key bug -------------------------------------------------
def test_integer_key_does_not_become_float(cfg):
    """1029 must stay '1029'. '1029.0' silently breaks every join."""
    c = Cleaner(cfg)
    assert c._norm_number("1029", integer=True) == "1029"
    assert c._norm_number("1029.5", integer=True) is None   # not a whole number
    assert c._norm_number("1029.50", integer=False) == "1029.5"


# --- date ambiguity ----------------------------------------------------
def test_unambiguous_dates_are_not_flagged_ambiguous(cfg):
    c = Cleaner(cfg)
    assert c._norm_date("14/05/1990") == ("1990-05-14", False)   # 14 > 12: forced
    assert c._norm_date("05/14/1990") == ("1990-05-14", False)   # 14 > 12: forced
    assert c._norm_date("1990-05-14") == ("1990-05-14", False)


def test_genuinely_ambiguous_date_is_flagged(cfg):
    c = Cleaner(cfg)
    iso, ambiguous = c._norm_date("05/06/1990")
    assert ambiguous is True, "a coin-flip date must be reported, never silent"
    assert iso == "1990-06-05"      # day_first policy


# --- null-in-disguise --------------------------------------------------
def test_na_string_counts_as_missing(cfg):
    df = pd.DataFrame({c: [""] for c in cfg["expected_columns"]})
    df.loc[0, "address"] = "N/A"
    cleaned, res = Cleaner(cfg).clean(df)
    assert any("null-in-disguise" in ch.reason for ch in res.changes)


# --- phone normalisation ----------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("(373) 474-5298", "373-474-5298"),
    ("956.981.5699", "956-981-5699"),
    ("+18664437557", "866-443-7557"),
    ("8664437557", "866-443-7557"),
    ("555-CALL-NOW", None),          # unrepairable: must NOT be guessed
])
def test_phone_normalisation(cfg, raw, expected):
    assert Cleaner(cfg)._norm_phone(raw) == expected


# --- severity is stage-dependent --------------------------------------
def test_format_failure_warns_pre_clean_but_enforces_post_clean(cfg):
    v = Validator(cfg)
    v.stage = "pre-clean"
    assert v._sev("format_invalid") == "warn"
    v.stage = "post-clean"
    assert v._sev("format_invalid") == "quarantine"


# --- masking -----------------------------------------------------------
def test_pseudonym_is_deterministic_and_salted(cfg):
    m = Masker(cfg)
    a, b = m._pseudonymize("1029"), m._pseudonymize("1029")
    assert a == b, "joins across extracts depend on determinism"
    assert "1029" not in a


def test_masking_never_leaks_the_original(cfg):
    m = Masker(cfg)
    assert m._partial_email("john.doe@gmail.com") == "j***@gmail.com"
    assert m._partial_tail("555-123-4567") == "***-***-4567"
    assert m._year_only("1985-03-15") == "1985-**-**"
    assert m._initial_only("John") == "J***"
