"""A living household for `sim/days.py`: N days of people and sensors, seeded and reproducible.

People are not machines. Bedtime spreads ±45 min around its mean and leans late (sleep-timing
studies report a weekday SD of tens of minutes and a weekend shift of one to two hours, "social
jetlag"); some evenings run past midnight; lights get forgotten; there are sick days at home, guests,
and one trip away. Sensors lie, rarely, in the ways the fault literature names: ghost motion (false
positive), a sensor stuck for hours (stuck-at), a temperature spike (outlier), slow drift (bias), a
dropout (unavailable), a presence tracker that flaps on bad Wi-Fi, a door contact that bounces.

Every event carries a `tag`: `routine`, `human` (a deviation a person makes) or `fault:<kind>` (a
sensor lie). Tags are ground truth for the checks (sim/invariants.py): what the add-on should have
learnt from, and what it should have ignored.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# entities of the base house (sim/fake_ha.py); a plan only keeps those the house has
KITCHEN, LIVING, BEDROOM, HALL = (
    "light.kitchen_ceiling",
    "light.living_room_lamp",
    "light.bedroom_lamp",
    "light.hallway",
)
TV, BLINDS = "media_player.living_room_tv", "cover.living_room_blinds"
HALL_MOTION, LIVING_MOTION = "binary_sensor.hallway_motion", "binary_sensor.living_room_motion"
DOOR, LOCK, PERSON = "binary_sensor.front_door_contact", "lock.front_door", "person.owner"
WINDOW, HEATER = "binary_sensor.bedroom_window", "switch.space_heater"
TEMPS = {"sensor.living_room_temperature": 21.0, "sensor.bedroom_temperature": 19.0}
MOTIONS = (HALL_MOTION, LIVING_MOTION)


@dataclass(frozen=True, order=True)
class Event:
    ts: float  # house time, epoch seconds
    entity_id: str = field(compare=False)
    state: str = field(compare=False)
    cause: str = field(compare=False)  # user (app/UI) | physical (sensor, wall switch)
    tag: str = field(compare=False)  # routine | human | fault:<kind>
    note: str = field(compare=False, default="")


@dataclass
class Chaos:
    """How messy the house is. Rates are per day unless named otherwise; 0 turns a kind off."""

    bedtime_sd_min: float = 45.0
    weekend_shift_h: float = 1.5
    late_night: float = 0.08  # past 01:00
    forgot_light: float = 0.10  # a light left on all night
    sick_day: float = 0.03  # a workday at home
    guests: float = 0.08  # an evening with more people, lights and motion
    airing: float = 0.15  # a cold evening: heater on, and someone opens the bedroom window anyway
    trip_days: int = 4  # one trip away of this many days (0 = none); placed in the second half
    ghost_motion: float = 0.3  # false positives a day, house-wide
    stuck: float = 0.07  # a motion sensor stuck "on" for 1-6 h
    spike: float = 0.15  # a temperature reading wildly off, once
    drift_c_per_day: float = 0.0  # one temperature sensor's bias grows by this per day
    dropout: float = 0.15  # a sensor unavailable for 2-20 min
    flapping: float = 0.1  # the tracker flaps home/not_home for a few minutes
    bounce: float = 0.1  # the door contact bounces on/off within seconds

    @classmethod
    def calm(cls) -> Chaos:
        kinds = ("late_night", "forgot_light", "sick_day", "guests", "airing", "ghost_motion", "stuck",
                 "spike", "dropout", "flapping", "bounce")  # fmt: skip
        z = dict.fromkeys(kinds, 0.0)
        return cls(bedtime_sd_min=10, weekend_shift_h=0, trip_days=0, **z)


def plan(days: int, start: datetime, seed: int = 7, chaos: Chaos | None = None) -> list[Event]:
    """`days` days of the household from local midnight `start` (aware), sorted by time."""
    c, rng = chaos or Chaos(), random.Random(seed)
    out: list[Event] = []
    trip = _trip(days, c, rng)

    def add(t: datetime, eid: str, state: str, cause: str = "physical", tag: str = "routine", note: str = ""):
        out.append(Event(t.timestamp(), eid, state, cause, tag, note))

    for d in range(days):
        day = start + timedelta(days=d)
        if d in trip:
            _away_day(day, d == min(trip), d == max(trip), add, rng)
            _climate(day, d, c, add, rng)
            continue
        weekend = day.weekday() >= 5
        sick = not weekend and rng.random() < c.sick_day
        home_all_day = weekend or sick
        tag = "human" if sick else "routine"
        # morning: wake, kitchen light, a walk through the hall
        wake = _at(day, (8.6 if weekend else 6.8) + (1.2 if sick else 0), 20, rng)
        add(wake, KITCHEN, "on", "user", tag)
        _motion(add, wake + timedelta(minutes=2), HALL_MOTION, rng)
        add(wake + timedelta(minutes=rng.uniform(35, 70)), KITCHEN, "off", "user", tag)
        if not home_all_day:  # out to work and back
            leave = wake + timedelta(minutes=rng.gauss(85, 10))
            back = _at(day, 17.8, 50, rng)
            _door(add, leave, rng)
            add(leave + timedelta(minutes=2), LOCK, "locked", "user")
            add(leave + timedelta(minutes=5), PERSON, "not_home")
            add(back, PERSON, "home")
            _door(add, back + timedelta(minutes=1), rng)
        else:
            for _ in range(rng.randint(6, 14)):  # pottering about at home
                _motion(add, _at(day, rng.uniform(9, 17), 0, rng), rng.choice(MOTIONS), rng, tag)
        # evening: lamp and TV, the owner's "Evening blinds" automation, then bed
        guests = rng.random() < c.guests
        evening = _at(day, 19.0, 30, rng)
        add(evening, LIVING, "on", "user")
        add(evening + timedelta(minutes=rng.uniform(5, 30)), TV, "on", "user")
        if guests:
            for eid in (KITCHEN, HALL):
                add(evening + timedelta(minutes=rng.uniform(10, 60)), eid, "on", "user", "human", "guests")
            for _ in range(rng.randint(8, 16)):
                _motion(
                    add, evening + timedelta(minutes=rng.uniform(0, 200)), rng.choice(MOTIONS), rng, "human"
                )
        bed = _bedtime(day, weekend or guests, c, rng)
        late = bed.hour < 12 and bed.date() > day.date()
        btag = "human" if late or guests else "routine"
        add(bed - timedelta(minutes=rng.uniform(5, 20)), TV, "off", "user", btag)
        forgot = rng.random() < c.forgot_light
        if not forgot:
            add(bed - timedelta(minutes=rng.uniform(1, 8)), LIVING, "off", "user", btag)
        if guests:
            for eid in (KITCHEN, HALL):
                add(bed - timedelta(minutes=rng.uniform(1, 4)), eid, "off", "user", "human", "guests")
        add(bed, BEDROOM, "on", "user", btag)
        add(bed + timedelta(minutes=rng.uniform(10, 35)), BEDROOM, "off", "user", btag)
        add(bed + timedelta(minutes=rng.uniform(0, 5)), LOCK, "locked", "user", btag)
        if forgot:  # noticed in the morning
            add(_at(day + timedelta(days=1), 7.2, 30, rng), LIVING, "off", "user", "human", "forgot the lamp")
        for _ in range(rng.randint(10, 25)):  # moving around in the evening
            _motion(add, evening + timedelta(minutes=rng.uniform(0, max(30, (bed - evening).seconds / 60))),
                    rng.choice(MOTIONS), rng)  # fmt: skip
        if rng.random() < c.airing:
            t = _at(day, 20.5, 40, rng)
            add(t, HEATER, "on", "user", "human")
            add(t + timedelta(minutes=10), WINDOW, "on", "physical", "human", "airing while heating")
            add(t + timedelta(minutes=rng.uniform(8, 25)), WINDOW, "off", "physical", "human")
            add(t + timedelta(minutes=rng.uniform(60, 120)), HEATER, "off", "user", "human")
        _climate(day, d, c, add, rng)
        _faults(day, c, add, rng)
    return sorted(out)


def summary(events: list[Event]) -> dict:
    tags: dict[str, int] = {}
    for e in events:
        tags[e.tag] = tags.get(e.tag, 0) + 1
    return {"events": len(events), "tags": dict(sorted(tags.items()))}


# --- pieces -----------------------------------------------------------------------------------------
def _at(day: datetime, hour: float, sd_min: float, rng: random.Random) -> datetime:
    return day + timedelta(hours=hour, minutes=rng.gauss(0, sd_min / 2) if sd_min else 0)


def _bedtime(day: datetime, late_day: bool, c: Chaos, rng: random.Random) -> datetime:
    """Minutes past 22:30 are log-normal with median 45 (23:15) and SD `bedtime_sd_min`: most nights
    near the mean, a long tail of late ones; later again on weekends and guest nights."""
    median, sd = 45.0, max(c.bedtime_sd_min, 1.0)
    x = (sd / median) ** 2  # lognormal SD = median * sqrt((e^s2 - 1) * e^s2)  ->  solve for s2
    sigma = math.sqrt(math.log((1 + math.sqrt(1 + 4 * x)) / 2))
    t = day + timedelta(hours=22.5, minutes=rng.lognormvariate(math.log(median), sigma))
    if late_day:
        t += timedelta(hours=c.weekend_shift_h * rng.uniform(0.6, 1.3))
    if rng.random() < c.late_night:
        t = day + timedelta(days=1, hours=rng.uniform(1.0, 2.5))
    return t


def _motion(add, t: datetime, eid: str, rng: random.Random, tag: str = "routine") -> None:
    add(t, eid, "on", "physical", tag)
    add(t + timedelta(seconds=rng.randint(30, 150)), eid, "off", "physical", tag)


def _door(add, t: datetime, rng: random.Random) -> None:
    add(t, DOOR, "on")
    add(t + timedelta(seconds=rng.randint(20, 60)), DOOR, "off")


def _trip(days: int, c: Chaos, rng: random.Random) -> set[int]:
    if not c.trip_days or days < c.trip_days + 4:
        return set()
    first = rng.randint(days // 2, days - c.trip_days - 1)
    return set(range(first, first + c.trip_days))


def _away_day(day: datetime, first: bool, last: bool, add, rng: random.Random) -> None:
    if first:
        t = _at(day, 10, 60, rng)
        _door(add, t, rng)
        add(t + timedelta(minutes=2), LOCK, "locked", "user", "human", "trip starts")
        add(t + timedelta(minutes=6), PERSON, "not_home", "physical", "human", "trip starts")
    if last:
        t = _at(day, 18, 60, rng)
        add(t, PERSON, "home", "physical", "human", "back from the trip")
        _door(add, t + timedelta(minutes=1), rng)
        add(t + timedelta(minutes=3), HALL, "on", "user", "human")
        add(t + timedelta(minutes=40), HALL, "off", "user", "human")


def _climate(day: datetime, d: int, c: Chaos, add, rng: random.Random) -> None:
    for h in range(24):
        for eid, base in TEMPS.items():
            warm = 1 if 7 <= h <= 22 else -1
            bias = c.drift_c_per_day * d if eid.endswith("bedroom_temperature") else 0.0
            v = base + 1.2 * warm * rng.random() + rng.gauss(0, 0.2) + bias
            add(day + timedelta(hours=h, minutes=rng.randint(0, 59)), eid, f"{v:.1f}", "physical",
                "fault:drift" if bias >= 0.5 else "routine")  # fmt: skip


def _faults(day: datetime, c: Chaos, add, rng: random.Random) -> None:
    def happens(rate: float) -> int:  # a Poisson count for one day
        n, p, k = 0, math.exp(-rate), rng.random()
        while k > p:
            n, k = n + 1, k * rng.random()
        return n

    for _ in range(happens(c.ghost_motion)):
        t = _at(day, rng.uniform(0, 24), 0, rng)
        _motion(add, t, rng.choice(MOTIONS), rng, "fault:ghost_motion")
    for _ in range(happens(c.stuck)):
        t, eid = _at(day, rng.uniform(8, 20), 0, rng), rng.choice(MOTIONS)
        add(t, eid, "on", "physical", "fault:stuck", "stuck on")
        add(t + timedelta(hours=rng.uniform(1, 6)), eid, "off", "physical", "fault:stuck", "unstuck")
    for _ in range(happens(c.spike)):
        t, eid = _at(day, rng.uniform(0, 24), 0, rng), rng.choice(list(TEMPS))
        add(t, eid, f"{rng.choice((-40.0, 85.0, 0.0)):.1f}", "physical", "fault:spike")
        normal = f"{TEMPS[eid] + rng.gauss(0, 0.3):.1f}"
        add(t + timedelta(minutes=rng.uniform(1, 10)), eid, normal, "physical", "fault:spike", "normal")
    for _ in range(happens(c.dropout)):
        t = _at(day, rng.uniform(0, 24), 0, rng)
        eid = rng.choice([*MOTIONS, DOOR, *TEMPS])
        add(t, eid, "unavailable", "physical", "fault:dropout")
        back = "off" if eid.startswith("binary_sensor") else f"{TEMPS.get(eid, 20.0):.1f}"
        add(t + timedelta(minutes=rng.uniform(2, 20)), eid, back, "physical", "fault:dropout", "back")
    for _ in range(happens(c.flapping)):  # in the evening, when the owner is home: it ends on "home"
        t = _at(day, rng.uniform(20.5, 22), 0, rng)
        for i in range(rng.randint(2, 4)):
            add(t + timedelta(minutes=2 * i), PERSON, "not_home", "physical", "fault:flapping")
            add(t + timedelta(minutes=2 * i + 1), PERSON, "home", "physical", "fault:flapping")
    for _ in range(happens(c.bounce)):
        t = _at(day, rng.uniform(7, 23), 0, rng)
        for i in range(rng.randint(2, 3)):
            add(t + timedelta(seconds=3 * i), DOOR, "on", "physical", "fault:bounce")
            add(t + timedelta(seconds=3 * i + 1), DOOR, "off", "physical", "fault:bounce")
