"""CPF and Brazilian mobile numbers (decisions 2026-10-12): validated, normalised, masked."""

from __future__ import annotations

import pytest

from app.core.br_documents import mask_cpf, mask_phone, normalize_cpf, normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("529.982.247-25", "52998224725"),
        ("52998224725", "52998224725"),
        (" 111.444.777-35 ", "11144477735"),
    ],
)
def test_valid_cpfs_are_normalised_to_digits(raw: str, expected: str) -> None:
    assert normalize_cpf(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "529.982.247-24",  # wrong check digit
        "111.111.111-11",  # all the same digit: passes the checksum but is not a CPF
        "1234567890",  # 10 digits
        "529982247250",  # 12 digits
        "abc",
        "",
    ],
)
def test_invalid_cpfs_are_refused(raw: str) -> None:
    assert normalize_cpf(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(81) 91234-5678", "+5581912345678"),
        ("81912345678", "+5581912345678"),
        ("+55 81 91234-5678", "+5581912345678"),
        ("55 (11) 98765-4321", "+5511987654321"),
    ],
)
def test_valid_mobiles_become_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "(81) 3123-4567",  # landline
        "(01) 91234-5678",  # DDD cannot start with 0
        "(80) 91234-5678",  # DDD cannot end with 0
        "91234-5678",  # no DDD
        "+1 415 555 0100",  # not Brazil
        "",
    ],
)
def test_other_numbers_are_refused(raw: str) -> None:
    assert normalize_phone(raw) is None


def test_masks_show_only_part_of_the_number() -> None:
    assert mask_cpf("52998224725") == "***.982.247-**"
    assert mask_phone("+5581912345678") == "(81) 9****-5678"
    assert mask_cpf(None) is None
    assert mask_phone(None) is None
