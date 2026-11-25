import pytest

from ergplan.regreader.voltage import band_key, disjoint, parse_band


@pytest.mark.parametrize(
    "text,band",
    [
        ("For supply up to 11 kV", {"max": 11}),
        ("For supply at 11kV", {"min": 11, "max": 11}),
        ("For supply above 11 kV and up to 66 kV", {"min": 11, "min_exclusive": True, "max": 66}),
        ("For supply above 132 kV", {"min": 132, "min_exclusive": True}),
        ("11 kV & above", {"min": 11}),
        ("33 kV", {"min": 33, "max": 33}),
        ("no voltage here", None),
        ("For supply at 11kV", {"min": 11, "max": 11}),
        ("HV-2 Supply at 11 kV up to 66 kV", {"min": 11, "max": 66}),  # NPCL's CSS table; see disjoint
        ("from 33 kV to 132 kV", {"min": 33, "max": 132}),
        ("Contracted load above 100 kVA", None),  # kVA is not a voltage
        ("Rs. 7.10 / kVAh", None),
        ("Distribution Loss above 33 kV (132 kV, 66 kV)", {"min": 33, "min_exclusive": True}),
    ],
)
def test_parse_band(text: str, band: dict | None) -> None:
    assert parse_band(text) == band


def test_band_key() -> None:
    assert band_key({"min": 11, "max": 11}) == "11"
    assert band_key({"min": 132, "min_exclusive": True}) == "132"
    assert band_key(None) is None


def test_bands_of_one_table_do_not_share_an_edge() -> None:
    # NPCL CSS Table 9-14: 'at 11 kV', 'at 11 kV up to 66 kV', 'at 66 kV up to 132 kV', 'at 132 kV'
    printed = [
        "Supply at 11 kV",
        "Supply at 11 kV up to 66 kV",
        "Supply at 66 kV up to 132 kV",
        "Supply at 132 kV#",
    ]
    assert disjoint([parse_band(t) for t in printed]) == [
        {"min": 11, "max": 11},
        {"min": 11, "max": 66, "min_exclusive": True},
        {"min": 66, "max": 132, "min_exclusive": True, "max_exclusive": True},
        {"min": 132, "max": 132},
    ]
    # bands that already exclude their edge, and rows without a voltage, are left as they are
    clean = [{"max": 11}, {"min": 11, "min_exclusive": True, "max": 66}, None]
    assert disjoint(clean) == clean
    # two ranges meeting at a voltage: 'up to' keeps it
    assert disjoint([{"max": 66}, {"min": 66, "max": 132}]) == [
        {"max": 66},
        {"min": 66, "max": 132, "min_exclusive": True},
    ]
