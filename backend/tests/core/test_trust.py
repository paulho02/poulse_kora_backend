"""app.core.trust: the Reviewer Trust formula.

Two of these tests are not really tests of behaviour, they are tests of *claims* -
the two properties app/core/trust.py says hold structurally rather than by tuning:

- anomalous behaviour alone can never demote a reader into the low band, so dropping a
  long run of bad posts stays allowed;
- volume alone can never promote one into the high band, so reach cannot be ground for.

Both are one careless coefficient change away from quietly becoming false, and neither
would show up as a failure anywhere else - the score would simply start meaning
something different. Everything below the invariants is a shape check on the curve, not
an assertion about specific numbers, which are settings and expected to move.
"""

import pytest

from app.core import trust
from app.core.config import settings
from app.core.trust import TrustInputs

# A population that forwards a quarter of what it sees - the shape the feed expects,
# where most posts are supposed to die.
POPULATION = 0.25


def score(**kwargs) -> int:
    kwargs.setdefault("population_forward_rate", POPULATION)
    return trust.compute_score(TrustInputs(**kwargs), settings)


def band(**kwargs) -> str:
    return trust.band_for(score(**kwargs), settings)


def clean_reviews(count: int, forward_rate: float = POPULATION) -> dict:
    """A reader with `count` ordinary, well-paced reviews at a normal forward rate."""
    return {
        "reviews": count,
        "forwards": round(count * forward_rate),
        "paced_reviews": max(0, count - 1),
        "hasty_reviews": 0,
    }


class TestNeutrality:
    def test_no_evidence_scores_exactly_neutral(self):
        """A brand-new account, and equally an account that has been away for longer
        than the window, has no evidence in either direction. Anything but neutral here
        would mean the feature either punishes newcomers or hands them a bonus."""
        assert score() == 50
        assert band() == trust.BAND_NORMAL

    def test_inactivity_needs_no_separate_rule(self):
        """Every input is windowed, so "came back after two months" and "just signed
        up" are the same empty set of rows. This is the whole decay mechanism."""
        assert score() == score(**clean_reviews(0))

    def test_a_single_probe_barely_moves_the_score(self):
        """Confidence exists so that one answer is not a verdict. A reader who gets
        their first probe wrong should not lose reach over it."""
        assert band(probes_answered=1, probes_correct=0, **clean_reviews(25)) == (
            trust.BAND_NORMAL
        )


class TestInvariants:
    """The two properties the formula's shape is supposed to guarantee."""

    @pytest.mark.parametrize("reviews", [20, 100, 1000])
    @pytest.mark.parametrize("forward_rate", [0.0, 1.0])
    def test_anomaly_alone_can_never_demote(self, reviews, forward_rate):
        """Maximally anomalous behaviour - every review faster than anyone can read,
        and a forward rate at one extreme or the other - with no probe evidence either
        way. The floor is neutral, so such a reader keeps normal reach.

        This is the "must not forbid dropping a series of bad posts" requirement. It
        holds because anomaly multiplies only the above-neutral half of the score (see
        trust.compute_score), not because any tolerance happens to be generous enough.
        """
        result = score(
            reviews=reviews,
            forwards=round(reviews * forward_rate),
            paced_reviews=reviews,
            hasty_reviews=reviews,
        )
        assert result >= 50
        assert trust.band_for(result, settings) is not trust.BAND_LOW

    def test_anomaly_caps_a_perfect_probe_record_at_neutral(self):
        """The other half of the same property: anomaly is a real ceiling, not a
        rounding error. A reader who aces every probe but never forwards anything -
        the strategy available to someone who has learned to spot the marker - is held
        at normal reach rather than rewarded with the bonus band."""
        assert (
            band(
                probes_answered=20,
                probes_correct=20,
                reviews=300,
                forwards=0,
                paced_reviews=299,
                hasty_reviews=0,
            )
            == trust.BAND_NORMAL
        )

    def test_volume_alone_can_never_promote(self):
        """Reviewing everything in sight, perfectly paced, at exactly the population's
        forward rate, with no probes answered at all. Grinding is not what buys reach.
        """
        result = score(**clean_reviews(10_000))
        assert result < settings.TRUST_BAND_HIGH_MIN
        assert trust.band_for(result, settings) is not trust.BAND_HIGH

    def test_only_failed_probes_reach_the_low_band(self):
        """The contrapositive of the first invariant, stated the way it matters: if a
        reader is in the low band, they got test posts wrong. Nothing else can put them
        there."""
        assert (
            band(
                probes_answered=12,
                probes_correct=6,  # chance level - answering without reading
                reviews=300,
                forwards=0,
                paced_reviews=299,
                hasty_reviews=250,
            )
            == trust.BAND_LOW
        )


class TestProbeComponent:
    def test_chance_level_earns_no_credit(self):
        """Half the variants ask to be forwarded and half to be dropped, so blind
        answering converges on 50%. If that scored neutral rather than zero, ignoring
        probes would cost nothing at all."""
        inputs = TrustInputs(probes_answered=10, probes_correct=5)
        assert trust.probe_component(inputs, settings) == 0.0

    def test_perfect_accuracy_at_full_confidence_is_the_maximum(self):
        inputs = TrustInputs(
            probes_answered=settings.TRUST_PROBE_CONFIDENCE_N,
            probes_correct=settings.TRUST_PROBE_CONFIDENCE_N,
        )
        assert trust.probe_component(inputs, settings) == 1.0

    def test_thin_evidence_is_pulled_toward_neutral(self):
        thin = trust.probe_component(
            TrustInputs(probes_answered=1, probes_correct=1), settings
        )
        thick = trust.probe_component(
            TrustInputs(
                probes_answered=settings.TRUST_PROBE_CONFIDENCE_N,
                probes_correct=settings.TRUST_PROBE_CONFIDENCE_N,
            ),
            settings,
        )
        assert trust.NEUTRAL < thin < thick

    def test_one_slip_does_not_cost_the_high_band(self):
        """Probes are meant to catch people who are not reading, not to punish a
        mis-tap. A single wrong answer in an otherwise perfect record has to survive."""
        assert (
            band(probes_answered=7, probes_correct=6, **clean_reviews(150))
            == trust.BAND_HIGH
        )


class TestVolumeComponent:
    def test_reviewing_little_is_never_worse_than_neutral(self):
        """A casual reader is quiet, not untrustworthy."""
        for reviews in (0, 1, 5, settings.TRUST_VOLUME_NEUTRAL_REVIEWS):
            value = trust.volume_component(TrustInputs(reviews=reviews), settings)
            assert value == trust.NEUTRAL

    def test_it_climbs_and_then_saturates(self):
        full = trust.volume_component(
            TrustInputs(reviews=settings.TRUST_VOLUME_FULL_REVIEWS), settings
        )
        beyond = trust.volume_component(
            TrustInputs(reviews=settings.TRUST_VOLUME_FULL_REVIEWS * 10), settings
        )
        assert full == 1.0
        assert beyond == full


class TestAnomalySignals:
    def test_no_population_rate_means_no_skew_signal(self):
        """A deployment too young to have a consensus has nothing to be anomalous
        against, and guessing one would manufacture the signal."""
        inputs = TrustInputs(
            reviews=100, forwards=0, population_forward_rate=None
        )
        assert trust.skew_penalty(inputs, settings) == 0.0

    def test_heavy_dropping_within_tolerance_is_not_anomalous(self):
        """The feed's premise is that most posts should die. Dropping nine in ten,
        where the population drops three in four, is doing the job."""
        inputs = TrustInputs(
            reviews=100, forwards=10, population_forward_rate=POPULATION
        )
        assert trust.skew_penalty(inputs, settings) == 0.0

    def test_blind_forwarding_is_caught_harder_than_blind_dropping(self):
        """Normalising per side is what produces this, and the asymmetry is the point:
        forwarding everything spends the system's reach, dropping everything only
        wastes the dropper's own time."""
        forwards_everything = trust.skew_penalty(
            TrustInputs(reviews=100, forwards=100, population_forward_rate=POPULATION),
            settings,
        )
        drops_almost_everything = trust.skew_penalty(
            TrustInputs(reviews=100, forwards=5, population_forward_rate=POPULATION),
            settings,
        )
        assert forwards_everything > drops_almost_everything

    def test_occasional_fast_reviews_are_tolerated(self):
        """Real reading is bursty - an obvious duplicate, a post whose first line
        settles it. Only a sustained rate means anything."""
        inputs = TrustInputs(reviews=100, paced_reviews=100, hasty_reviews=10)
        assert trust.pace_penalty(inputs, settings) == 0.0

    def test_machine_gun_pacing_is_not(self):
        inputs = TrustInputs(reviews=100, paced_reviews=100, hasty_reviews=100)
        assert trust.pace_penalty(inputs, settings) == 1.0

    def test_thin_evidence_withholds_the_ceiling(self):
        """One fast burst inside four reviews is not a finding."""
        inputs = TrustInputs(reviews=4, paced_reviews=4, hasty_reviews=4)
        assert trust.anomaly_ceiling(inputs, settings) > 0.5


class TestBandsAndReach:
    def test_bands_partition_the_scale(self):
        assert trust.band_for(settings.TRUST_BAND_LOW_MAX - 1, settings) == (
            trust.BAND_LOW
        )
        assert trust.band_for(settings.TRUST_BAND_LOW_MAX, settings) == (
            trust.BAND_NORMAL
        )
        assert trust.band_for(settings.TRUST_BAND_HIGH_MIN - 1, settings) == (
            trust.BAND_NORMAL
        )
        assert trust.band_for(settings.TRUST_BAND_HIGH_MIN, settings) == (
            trust.BAND_HIGH
        )

    def test_reach_is_ordered_and_normal_is_the_default_fanout(self):
        low = trust.fanout_for(trust.BAND_LOW, settings)
        normal = trust.fanout_for(trust.BAND_NORMAL, settings)
        high = trust.fanout_for(trust.BAND_HIGH, settings)

        assert normal == settings.FEED_FANOUT
        assert low < normal < high

    def test_a_forward_always_reaches_someone(self):
        """Whatever the multipliers and however small FEED_FANOUT gets, a forward that
        reached nobody would be a silent failure the reader has no way to notice."""
        for band_name in (trust.BAND_LOW, trust.BAND_NORMAL, trust.BAND_HIGH):
            assert trust.fanout_for(band_name, settings) >= 1

    def test_disabling_trust_restores_the_flat_fanout(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUST_ENABLED", False)
        for band_name in (trust.BAND_LOW, trust.BAND_NORMAL, trust.BAND_HIGH):
            assert trust.fanout_for(band_name, settings) == settings.FEED_FANOUT


class TestWorkedCases:
    """The readers the design was argued over, as a table. These pin the *ordering*
    and the bands, not the exact numbers - the numbers are settings."""

    def test_the_curve_ranks_readers_as_intended(self):
        attentive_daily = score(
            probes_answered=10, probes_correct=10, **clean_reviews(200)
        )
        attentive_casual = score(
            probes_answered=1, probes_correct=1, **clean_reviews(25)
        )
        newcomer = score()
        blind = score(
            probes_answered=12,
            probes_correct=6,
            reviews=300,
            forwards=0,
            paced_reviews=299,
            hasty_reviews=250,
        )

        assert attentive_daily > attentive_casual >= newcomer > blind
        assert trust.band_for(attentive_daily, settings) == trust.BAND_HIGH
        assert trust.band_for(attentive_casual, settings) == trust.BAND_NORMAL
        assert trust.band_for(newcomer, settings) == trust.BAND_NORMAL
        assert trust.band_for(blind, settings) == trust.BAND_LOW
