"""Weighted blends of interest vectors.

These ran on numpy until the Vercel deploy: 73 MB of numpy for three weighted
sums was most of the serverless size budget. The tests below were written
against the numpy implementation first, so they pin the behaviour that the pure
Python version has to reproduce — element order, weighting, and refusing to
blend vectors of different lengths.

Since issue #4 every blend is renormalised to unit length. OpenAI embeddings have
norm 1, and a weighted sum of two different unit vectors is shorter than 1, so
without this the stored interest vector shrank a little on every chat sync. The
expectations below are the weighted sums divided by their norm.
"""

from __future__ import annotations

import math

import pytest

from app.services.ai_service import ai_service


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values]


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(x * x for x in values))


def test_blend_vectors_uses_the_default_weights() -> None:
    out = ai_service.blend_vectors([1.0, 0.0], [0.0, 1.0])

    assert out == pytest.approx(_unit([0.8, 0.2]))


def test_blend_vectors_accepts_explicit_weights() -> None:
    out = ai_service.blend_vectors([1.0, 2.0], [3.0, 4.0], w_personal=0.5, w_social=0.5)

    assert out == pytest.approx(_unit([2.0, 3.0]))


def test_blend_vectors_keeps_element_order() -> None:
    out = ai_service.blend_vectors([1.0, 2.0, 3.0], [0.0, 0.0, 0.0], 1.0, 0.0)

    assert out == pytest.approx(_unit([1.0, 2.0, 3.0]))


def test_blend_interest_weighted_is_a_convex_blend() -> None:
    out = ai_service.blend_interest_weighted([10.0, 0.0], [0.0, 10.0], 0.9, 0.1)

    assert out == pytest.approx(_unit([9.0, 1.0]))


def test_update_interest_organically_uses_the_configured_weights() -> None:
    from app.core.config import settings

    wc, wn = settings.INTEREST_WEIGHT_CURRENT, settings.INTEREST_WEIGHT_NEW
    out = ai_service.update_interest_organically([1.0, 0.0], [0.0, 1.0])

    assert out == pytest.approx(_unit([wc, wn]))


@pytest.mark.parametrize(
    "method",
    ["blend_vectors", "blend_interest_weighted", "update_interest_organically"],
)
def test_blends_of_unit_vectors_stay_unit_length(method: str) -> None:
    # Repeated syncs used to shrink the vector; it must stay on the unit sphere.
    fn = getattr(ai_service, method)
    extra = (0.7, 0.3) if method != "update_interest_organically" else ()
    vec = _unit([1.0, 0.0, 0.0])
    for _ in range(20):
        vec = fn(vec, _unit([0.0, 1.0, 1.0]), *extra)

    assert _norm(vec) == pytest.approx(1.0)


def test_a_zero_blend_is_returned_as_is() -> None:
    # Nothing to normalise; dividing by zero would turn it into NaNs.
    out = ai_service.blend_vectors([1.0, 0.0], [-4.0, 0.0], 0.8, 0.2)

    assert out == pytest.approx([0.0, 0.0])


def test_blends_return_plain_floats() -> None:
    # The result is serialized straight into JSON for Supabase; a numpy scalar
    # would not survive that, so the element type is part of the contract.
    out = ai_service.blend_vectors([1.0], [2.0])

    assert isinstance(out, list)
    assert all(type(x) is float for x in out)


def test_full_length_embedding_round_trip() -> None:
    a = [0.5] * 1536
    b = [1.5] * 1536

    out = ai_service.blend_vectors(a, b, 0.5, 0.5)

    assert len(out) == 1536
    assert out == pytest.approx(_unit([1.0] * 1536))


@pytest.mark.parametrize(
    "method",
    ["blend_vectors", "blend_interest_weighted", "update_interest_organically"],
)
def test_mismatched_lengths_are_refused(method: str) -> None:
    # Silently truncating to the shorter vector would corrupt an embedding
    # without anyone noticing.
    fn = getattr(ai_service, method)
    args = ([1.0, 2.0, 3.0], [1.0, 2.0])
    extra = (0.5, 0.5) if method != "update_interest_organically" else ()

    with pytest.raises(ValueError):
        fn(*args, *extra)
