"""Reviewer Trust: how much of the feed's reach one reader's judgement is worth.

A forward is free - tokens buy `FEED_FANOUT` deliveries at admission and every hand
the post passes through after that is earned (see CLAUDE.md). This decides how wide
"earned" is: a low-trust reader's forward reaches two people, a normal one's three, a
high one's four. Nothing here touches an original post, because original reach is what
its author paid for; a posting discount would be *creator* trust, a separate feature.

This module is the formula and nothing else - pure, I/O-free and settings-driven, the
same shape as `app/feed/pricing.py`, so the curve can be argued about and unit-tested
without a database. `app/core/trust_service.py` gathers the inputs; `app/core/probes.py`
produces the strongest of them.

Three properties are load-bearing, and each of them is a test rather than a comment:

- **Absence of evidence is never evidence.** Every component is shrunk toward neutral
  by its own confidence, so a brand-new account scores exactly the neutral 50 and lands
  in the normal band. Combined with the trailing window (`TRUST_WINDOW_DAYS`), that is
  also the entire handling of inactivity: a reader who disappears for a month comes back
  at neutral, having kept neither a good record nor a bad one. No separate decay rule,
  because there is nothing left for one to do.

- **Anomaly is a ceiling, not a penalty.** It multiplies only the *above-neutral* half
  of the score (see `compute_score`), so its worst case is exactly neutral. It can
  withhold the bonus band; it can never produce the low one. Only failed test posts can
  cost a reader reach. This is the whole answer to "dropping a series of bad posts must
  stay allowed": it is not a tolerance that could be tuned wrong, it is arithmetic.

- **Volume alone cannot buy the high band.** A reader who reviews everything and
  answers no probes tops out below `TRUST_BAND_HIGH_MIN`. Grinding does not buy reach;
  reading does.
"""

from dataclasses import dataclass

from app.core.config import Settings

# The score a reader with no evidence in either direction gets, expressed on the 0-1
# scale the components work in. Not a setting: it is the definition of "neutral", and
# a deployment that moved it would be changing what the bands mean rather than tuning
# them.
NEUTRAL = 0.5

BAND_LOW = "low"
BAND_NORMAL = "normal"
BAND_HIGH = "high"


@dataclass(frozen=True)
class TrustInputs:
    """Everything the formula reads, already reduced to counts over the window.

    Deliberately a flat bag of integers rather than a session or a user: the point of
    splitting this module out is that the curve can be exercised over a table of cases
    without Postgres, and that the gathering side (`trust_service`) has exactly one
    contract to satisfy.
    """

    # Test posts answered in the window, and how many were answered as instructed.
    probes_answered: int = 0
    probes_correct: int = 0

    # Real reviews (forward or drop) in the window. Probe answers are deliberately not
    # counted here: a probe is a measurement, and letting it feed the volume and pace
    # signals would make the instrument part of what it measures.
    reviews: int = 0
    forwards: int = 0

    # Reviews that had a predecessor in the window (the denominator a gap can be
    # measured against), and how many of those followed it faster than a human reads.
    paced_reviews: int = 0
    hasty_reviews: int = 0

    # The deployment's own forward rate over the same window, or None when too few
    # reviews exist for it to mean anything (see TRUST_FORWARD_RATE_MIN_SAMPLE). None
    # disables the skew signal rather than substituting a guess - a fresh deployment
    # has no population behaviour to be anomalous against.
    population_forward_rate: float | None = None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _shrink(value: float, confidence: float, toward: float = NEUTRAL) -> float:
    """Pull `value` toward `toward` in proportion to how little evidence backs it."""
    confidence = _clamp01(confidence)
    return confidence * value + (1.0 - confidence) * toward


def probe_component(inputs: TrustInputs, settings: Settings) -> float:
    """Accuracy on test posts, in 0-1. The strongest signal, and the only honest one.

    Everything else the score can see is behaviour a script can imitate: a bot can pace
    itself, and it can forward a plausible fraction of what it is handed. What it cannot
    do is answer a post that asks, in its own words and in a different way each time, to
    be forwarded or dropped. That is the one measurement that distinguishes reading from
    performing the shape of reading.

    Chance maps to **zero**, not to neutral. Half the variants ask to be forwarded and
    half to be dropped, so a reader who never reads converges on
    `TRUST_PROBE_CHANCE_RATE` - and if that scored neutral, ignoring probes would cost
    nothing and the whole mechanism would be decorative.

    The rescale is what makes the component strict where it should be: probes are
    written to be unmissable by anyone who read the post, so the interesting range is
    the top half of the accuracy scale, not all of it. Confidence is what stops that
    strictness from landing on someone who has simply not been probed much yet - at one
    probe the component barely moves off neutral whichever way it went.
    """
    if inputs.probes_answered <= 0:
        return NEUTRAL
    rate = inputs.probes_correct / inputs.probes_answered
    chance = settings.TRUST_PROBE_CHANCE_RATE
    value = _clamp01((rate - chance) / (1.0 - chance)) if chance < 1.0 else 0.0
    confidence = inputs.probes_answered / settings.TRUST_PROBE_CONFIDENCE_N
    return _shrink(value, confidence)


def volume_component(inputs: TrustInputs, settings: Settings) -> float:
    """Sustained reviewing, in 0-1, on a scale whose floor is neutral.

    Bonus-only by construction: the output is `NEUTRAL` at or below
    `TRUST_VOLUME_NEUTRAL_REVIEWS` and climbs from there, so reviewing a lot can lift a
    reader and reviewing little can never lower one. A casual reader is quiet, not
    untrustworthy, and a feature that answered "you read 12 posts this month" with less
    reach would be punishing the wrong thing.

    That floor is also why this needs no confidence term. Confidence exists to keep a
    thin sample from being read as a verdict, and a count is not a sample - four reviews
    is not weak evidence of low volume, it *is* the volume. What keeps it fair is that
    the low end is neutral, not zero.
    """
    span = settings.TRUST_VOLUME_FULL_REVIEWS - settings.TRUST_VOLUME_NEUTRAL_REVIEWS
    if span <= 0:
        return NEUTRAL
    over = inputs.reviews - settings.TRUST_VOLUME_NEUTRAL_REVIEWS
    return NEUTRAL + (1.0 - NEUTRAL) * _clamp01(over / span)


def pace_penalty(inputs: TrustInputs, settings: Settings) -> float:
    """0-1: how much of this reader's reviewing happened faster than reading allows.

    Measured as the share of reviews that followed their predecessor inside
    `TRUST_MIN_READ_SECONDS`, less a generous tolerance. Real reading is bursty - an
    obvious duplicate, a post whose first line settles it, a double-tap on a card that
    was already decided - so an isolated fast review says nothing. Only a *sustained*
    share of them does, which is exactly what a share-with-tolerance measures and what a
    "fastest review" or "any review under Xs" rule would not.
    """
    if inputs.paced_reviews <= 0:
        return 0.0
    hasty_share = inputs.hasty_reviews / inputs.paced_reviews
    tolerance = settings.TRUST_PACE_TOLERANCE
    if tolerance >= 1.0:
        return 0.0
    return _clamp01((hasty_share - tolerance) / (1.0 - tolerance))


def skew_penalty(inputs: TrustInputs, settings: Settings) -> float:
    """0-1: how far this reader's forward rate sits from the deployment's.

    Measured against the population rather than against an arbitrary 50/50, because the
    feed's whole premise is that most posts should die: a reader who drops three
    quarters of what they see is doing the job, not failing it. The comparison is
    therefore "unlike everyone else", not "unlike a coin".

    Normalised by the room available on each side, so the extremes mean the same thing
    whatever the population rate is: 1.0 is *never forwards* below the mean and *always
    forwards* above it. That normalisation is what makes the signal asymmetric, and the
    asymmetry is the point - where the population forwards a quarter, blind forwarding
    is scored far harder than blind dropping, because blind forwarding spends the
    system's reach while blind dropping only wastes the dropper's own time.

    Returns 0.0 when there is no population rate to compare against: a deployment too
    young to have one has no consensus to deviate from, and guessing one would invent
    the very signal this is supposed to observe.
    """
    mean = inputs.population_forward_rate
    if mean is None or inputs.reviews <= 0:
        return 0.0
    rate = inputs.forwards / inputs.reviews
    room = mean if rate < mean else 1.0 - mean
    if room <= 0:
        return 0.0
    distance = _clamp01(abs(rate - mean) / room)
    tolerance = settings.TRUST_SKEW_TOLERANCE
    if tolerance >= 1.0:
        return 0.0
    return _clamp01((distance - tolerance) / (1.0 - tolerance))


def anomaly_ceiling(inputs: TrustInputs, settings: Settings) -> float:
    """0-1 multiplier on the bonus half of the score. 1.0 is "nothing odd here".

    Deliberately *not* a third weighted term. As a term it could push a reader into the
    low band on behaviour alone, and the two behaviours it watches - fast reviewing and
    a lopsided forward rate - both have entirely legitimate explanations. As a ceiling
    its worst case is neutral: it withholds the bonus and stops there.

    That shape also closes the one exploit the visible probe marker opens. A reader who
    learns to spot test posts, answers every one correctly and blind-drops everything
    else would otherwise score at the top on probe accuracy alone; here the forward rate
    that strategy produces caps them at normal reach. It does not close the patient
    version of the same trick (forward a token five percent), which is genuinely
    indistinguishable from careful curation - see CLAUDE.md for the metric that would
    show it happening.

    Shrunk toward 1.0 rather than toward neutral: the absence of evidence for anomalous
    behaviour is the absence of a ceiling, not a half-applied one.
    """
    raw = _clamp01(pace_penalty(inputs, settings) + skew_penalty(inputs, settings))
    confidence = _clamp01(inputs.reviews / settings.TRUST_ANOMALY_CONFIDENCE_N)
    return 1.0 - raw * confidence


def compute_score(inputs: TrustInputs, settings: Settings) -> int:
    """The reader's Reviewer Trust, 0-100.

    Two additive components (probe accuracy, review volume) produce a base, and the
    anomaly ceiling scales whatever part of that base sits *above* neutral. Applying it
    one-sidedly is what makes the ceiling a ceiling: below neutral there is no bonus to
    withhold, and a reader who got there did so by failing probes, which the ceiling has
    no business amplifying.
    """
    weight_probe = settings.TRUST_WEIGHT_PROBE
    weight_volume = settings.TRUST_WEIGHT_VOLUME
    total_weight = weight_probe + weight_volume
    if total_weight <= 0:
        return round(NEUTRAL * 100)

    base = (
        weight_probe * probe_component(inputs, settings)
        + weight_volume * volume_component(inputs, settings)
    ) / total_weight

    if base > NEUTRAL:
        base = NEUTRAL + (base - NEUTRAL) * anomaly_ceiling(inputs, settings)
    return max(0, min(100, round(base * 100)))


def band_for(score: int, settings: Settings) -> str:
    """Which reach band a score falls in: BAND_LOW / BAND_NORMAL / BAND_HIGH."""
    if score < settings.TRUST_BAND_LOW_MAX:
        return BAND_LOW
    if score >= settings.TRUST_BAND_HIGH_MIN:
        return BAND_HIGH
    return BAND_NORMAL


def reach_multiplier(band: str, settings: Settings) -> float:
    """What this band does to a forward's fan-out, as a multiple of FEED_FANOUT."""
    if band == BAND_LOW:
        return settings.TRUST_REACH_LOW_MULTIPLIER
    if band == BAND_HIGH:
        return settings.TRUST_REACH_HIGH_MULTIPLIER
    return 1.0


def fanout_for(band: str, settings: Settings) -> int:
    """Recipients one forward from this band reaches. Never fewer than 1 - a forward
    that reached nobody would be a silent failure, and the reader who earned it has no
    way to tell.

    Rounds half up rather than with `round`, whose banker's rounding resolves the
    frequent .5 cases in alternating directions - the same reason `pricing.route_price`
    does it by hand.
    """
    if not settings.TRUST_ENABLED:
        return settings.FEED_FANOUT
    scaled = int(settings.FEED_FANOUT * reach_multiplier(band, settings) + 0.5)
    return max(1, scaled)
