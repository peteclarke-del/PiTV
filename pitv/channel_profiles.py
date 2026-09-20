"""The shape of a week on each of the four general channels a fresh install ships with.

PiTV One, Two, Three and Four are modelled on BBC One, BBC Two, ITV and Channel 4 as they were
through the 1980s. This is seed data and nothing more: each profile is written to its channel's
`daypart_profile` once, where it can be read and changed in the admin (Channel, Dayparts), and
the scheduler knows nothing of it beyond the dayparts any channel may have. A channel the owner
has renamed or given dayparts of its own is never touched.

A daypart weights the kinds of programme (`tv`, `movie`), children's and sport material, may cap
a programme's length, and may weight genres (`genres`, read canonically): that last is what
lets a channel keep its quiz at teatime and its soap at half past seven. A weight is relative
to 1; sport at 3 or more forms a block in which sport follows sport (docs/PLAN.md section 4.6).

The shapes are checked against listings of the period (docs/research/uk-schedules-1980-1992.md).
Two departures are deliberate. Saturday morning is children's television on all four, as the
owner remembers the decade, although the listings show BBC Two's children's strand on Sunday
mornings (from 1987, so Two has both here) and Channel 4 not on the air on a Saturday morning
until 1986. And game shows and variety are kept to the evening, with the two daytime slots the listings
show most plainly let back in: Countdown's on Channel 4 and ITV's weekday afternoon quiz. ITV's
morning game show (from 1987) and BBC One's Going for Gold are left out; a channel's Dayparts
table in the admin is where to add them.

The broadcast day here runs from 08:00 to midnight, so the hours before the real channels came
on air are filled in keeping: BBC Two's Open University mornings, Channel 4's repeats and
matinees before its teatime start. News, weather and regional programmes, which no library
holds, leave their slots to what surrounded them."""

from __future__ import annotations

from typing import Any


def _dp(name: str, start: str, tv: float, movie: float, kids: float, sport: float, max_minutes: int | None = None,
        **genres: float) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "start": start, "tv": tv, "movie": movie, "kids": kids, "sport": sport}
    if max_minutes:
        row["max_minutes"] = max_minutes
    if genres:
        # Keyword names cannot hold a space or a hyphen: one underscore is a space, two a hyphen.
        row["genres"] = {name.replace("__", "-").replace("_", " "): weight for name, weight in genres.items()}
    return row


# BBC One: Breakfast Time, Pebble Mill and schools by day, Children's BBC from five to four, the
# early evening of Wogan and EastEnders, sitcom and drama either side of the Nine O'Clock News.
# Saturday is Swap Shop or Superstore, Grandstand from a quarter past twelve, then the family
# teatime of Doctor Who, Jim'll Fix It and The Generation Game, and Match of the Day. Sunday is
# quiet until the omnibus, a film, the classic serial at teatime and drama in the evening.
BBC_ONE = {
    "weekday": [
        _dp("Breakfast", "08:00", 1.0, 0.05, 0.3, 0.05, 40, Magazine=2.0, Informational=2.0),
        _dp("Daytime", "09:30", 1.0, 0.2, 0.6, 0.1, Education=2.0, Magazine=2.0),
        _dp("Lunchtime", "12:30", 1.0, 0.1, 0.3, 0.2, 40, Magazine=2.0, Informational=1.5, Comedy=1.2),
        _dp("Afternoon", "13:45", 0.8, 1.6, 0.5, 0.3, Soap=1.5, Drama=1.3, Western=1.5),
        _dp("Children's BBC", "15:55", 1.0, 0.05, 7.0, 0.05, 35, Children=2.0, Animation=2.0, Family=1.5),
        _dp("Early evening", "17:35", 1.0, 0.1, 0.4, 0.2, 40, Magazine=2.5, Informational=2.0, Soap=2.0, Family=1.5, Comedy=1.3, Documentary=1.3),
        _dp("Prime time", "19:00", 1.0, 0.3, 0.05, 0.3, Soap=2.5, Comedy=2.0, Game_Show=1.8, Drama=1.6, Family=1.5, Entertainment=1.5,
            Music=1.3, Adventure=1.3),
        _dp("After the Nine O'Clock News", "21:00", 1.0, 1.2, 0.0, 0.4, Drama=2.0, Crime=1.6, Thriller=1.4, Documentary=1.4),
        _dp("Late", "22:30", 0.8, 1.6, 0.0, 2.0, Comedy=1.2),
    ],
    "saturday": [
        _dp("Saturday morning", "08:00", 1.0, 0.1, 6.0, 0.1, 60, Children=2.0, Animation=2.0),
        _dp("Grandstand", "12:15", 0.2, 0.2, 0.2, 9.0),
        _dp("Saturday teatime", "17:05", 1.0, 0.2, 1.5, 0.3, 50, Family=2.5, Game_Show=2.5, Entertainment=2.0, Science_Fiction=2.0,
            Adventure=2.0, Comedy=1.5),
        _dp("Saturday night", "19:00", 1.0, 0.4, 0.05, 0.3, Game_Show=3.0, Entertainment=2.5, Comedy=2.5, Family=2.0, Drama=1.6, Crime=1.5),
        _dp("Saturday post-watershed", "21:00", 1.0, 1.3, 0.0, 0.5, Comedy=1.8, Drama=1.5, Crime=1.5, Thriller=1.5),
        _dp("Match of the Day", "22:15", 0.6, 1.5, 0.0, 4.0),
    ],
    "sunday": [
        _dp("Sunday morning", "08:00", 1.0, 0.2, 2.0, 0.1, 60, Education=2.0, Children=1.5, Animation=1.5, Documentary=1.3),
        _dp("Sunday lunchtime", "12:00", 1.0, 0.3, 0.5, 0.4, Magazine=1.5, Informational=1.5, Documentary=1.3),
        _dp("Omnibus and the film", "14:00", 0.8, 1.8, 0.4, 1.0, Soap=2.5, Family=1.5, Western=1.5, Adventure=1.5),
        _dp("Sunday teatime", "16:30", 1.0, 0.4, 1.2, 0.3, 50, Drama=2.0, Family=2.0, Mini__series=2.0, History=1.5),
        _dp("Sunday evening", "19:15", 1.0, 0.5, 0.05, 0.2, Drama=2.5, Comedy=2.0, Mini__series=2.0, Game_Show=1.5),
        _dp("Sunday post-watershed", "21:00", 1.0, 1.3, 0.0, 0.3, Drama=2.0, Crime=1.5),
        _dp("Sunday late", "22:30", 0.8, 0.8, 0.0, 0.5, Documentary=2.0, History=1.5, Science=1.3),
    ],
}

# BBC Two: Open University and schools, then documentary by day; the afternoons took the sport
# BBC One had no room for, and the evenings ran snooker and darts for weeks at a time, so sport
# is welcome here on weekday afternoons and evenings as well as at weekends. Cult imports at
# six, the documentary and leisure strands at seven and eight, comedy at nine, Newsnight and
# a film. Sunday Grandstand came here in 1981; Saturday night ends in a late film, a horror
# double bill in the summers that had one (1975 to 1981 and 1983).
BBC_TWO = {
    "weekday": [
        _dp("Daytime on Two", "08:00", 1.0, 0.1, 0.5, 0.05, 40, Education=3.0, Science=2.0, Technology=2.0, Documentary=1.5, Children=1.3),
        _dp("Daytime", "11:00", 1.0, 0.3, 0.3, 0.3, Education=2.5, Documentary=1.5, Nature=1.5, History=1.5),
        _dp("Afternoon", "14:00", 0.7, 1.6, 0.3, 2.0, Western=1.3),
        _dp("Six o'clock", "17:30", 1.0, 0.8, 0.5, 1.0, Science_Fiction=2.0, Comedy=1.8, Western=1.5, Family=1.5, Adventure=1.5, Fantasy=1.3),
        _dp("Evening", "19:00", 1.0, 0.4, 0.05, 1.2, Documentary=2.0, Technology=2.0, Science=2.0, Nature=2.0,
            Magazine=1.8, Informational=1.8, History=1.5),
        _dp("Nine o'clock", "21:00", 1.0, 1.0, 0.0, 1.5, Comedy=2.5, Drama=1.6, Documentary=1.5),
        _dp("Late", "22:30", 0.8, 2.0, 0.0, 2.5, Horror=1.5, Music=1.5, Thriller=1.3),
    ],
    "saturday": [
        _dp("Saturday morning", "08:00", 1.0, 0.2, 4.0, 0.05, 60, Children=2.0, Animation=2.0, Education=1.5),
        _dp("Saturday afternoon", "12:00", 0.6, 2.0, 0.3, 3.0),
        _dp("Saturday early evening", "17:30", 1.0, 0.4, 0.3, 1.5, Documentary=2.0, Music=1.5, History=1.5, Nature=1.3),
        _dp("Saturday evening", "19:00", 1.0, 0.8, 0.05, 1.0, Documentary=2.0, Drama=1.5, Music=1.5, History=1.5),
        _dp("Saturday nine o'clock", "21:00", 0.8, 2.2, 0.0, 1.5, Drama=1.5, Comedy=1.2),
        _dp("Horror double bill", "22:30", 0.5, 2.5, 0.0, 2.0, Horror=2.0, Thriller=1.5),
    ],
    "sunday": [
        _dp("Sunday morning", "08:00", 1.0, 0.2, 3.0, 0.05, 60, Children=2.0, Animation=2.0, Education=2.0),
        _dp("Sunday lunchtime", "12:00", 1.0, 0.5, 0.3, 1.0, Documentary=1.5, Nature=1.5, Magazine=1.5),
        _dp("Sunday Grandstand", "13:00", 0.3, 0.8, 0.2, 7.0),
        _dp("Sunday early evening", "18:30", 1.0, 0.4, 0.2, 1.0, Nature=2.0, Documentary=2.0, Magazine=1.5),
        _dp("Sunday evening", "19:30", 1.0, 0.8, 0.05, 0.5, Drama=1.5, Music=1.5, Documentary=1.5, Comedy=1.5),
        _dp("Sunday nine o'clock", "21:00", 1.0, 1.5, 0.0, 0.8, Drama=1.8, Music=1.5, Documentary=1.3, Comedy=1.2),
        _dp("Sunday late", "22:30", 0.6, 2.2, 0.0, 1.0),
    ],
}

# ITV: TV-am, daytime quiz and soap, the lunchtime children's slot and Crown Court, Children's
# ITV at four, Blockbusters and the teatime soaps, Coronation Street at half past seven, action
# and crime drama, films after News at Ten and Midweek Sports Special. Saturday is Tiswas or
# No. 73, World of Sport from a quarter past twelve with the wrestling at four, The A-Team and the
# big game shows. Sunday has The Big Match, Bullseye at teatime and the Sunday night drama.
ITV = {
    "weekday": [
        _dp("TV-am", "08:00", 1.0, 0.05, 1.5, 0.05, 40, Magazine=2.0, Informational=2.0, Children=1.5, Animation=1.5),
        _dp("Morning", "09:30", 1.0, 0.2, 0.5, 0.1, Education=2.5, Documentary=1.5, Magazine=1.3, Soap=1.2),
        _dp("Lunchtime", "12:00", 1.0, 0.1, 1.5, 0.2, 40, Soap=2.5, Drama=1.5, Children=1.5),
        _dp("Afternoon", "13:30", 1.0, 1.4, 0.4, 0.5, Soap=2.0, Drama=1.5, Game_Show=1.5),
        _dp("Children's ITV", "16:00", 1.0, 0.05, 7.0, 0.05, 35, Children=2.0, Animation=2.0),
        _dp("Teatime", "17:15", 1.0, 0.1, 0.4, 0.2, 40, Game_Show=3.0, Soap=3.0, Comedy=1.3),
        _dp("Prime time", "19:00", 1.0, 0.4, 0.05, 0.3, Soap=2.5, Comedy=2.0, Game_Show=2.0, Crime=1.8, Action=1.8, Drama=1.6),
        _dp("Nine o'clock drama", "21:00", 1.0, 1.0, 0.0, 0.4, Crime=2.2, Drama=2.0, Action=1.6, Thriller=1.5),
        _dp("After News at Ten", "22:30", 0.8, 1.8, 0.0, 2.5, Crime=1.3, Horror=1.3),
    ],
    "saturday": [
        _dp("Saturday morning", "08:00", 1.0, 0.1, 6.0, 0.1, 60, Children=2.0, Animation=2.0),
        _dp("World of Sport", "12:15", 0.2, 0.2, 0.2, 9.0, Wrestling=1.5),
        _dp("Saturday teatime", "17:00", 1.0, 0.2, 1.2, 0.3, 60, Action=3.0, Adventure=2.5, Game_Show=2.5, Family=2.0, Science_Fiction=1.5),
        _dp("Saturday night", "19:00", 1.0, 0.4, 0.05, 0.3, Game_Show=3.0, Entertainment=2.5, Comedy=2.0, Family=1.5),
        _dp("Saturday action hour", "20:00", 1.0, 0.6, 0.05, 0.3, Crime=2.5, Action=2.5, Adventure=1.5, Drama=1.3, Game_Show=0.8),
        _dp("Saturday post-watershed", "21:00", 0.9, 2.0, 0.0, 0.5, Crime=2.0, Drama=1.8, Thriller=1.5),
        _dp("Saturday late", "22:30", 0.6, 2.0, 0.0, 2.0, Horror=1.5),
    ],
    "sunday": [
        _dp("Sunday morning", "08:00", 1.0, 0.2, 2.5, 0.1, 60, Children=1.5, Animation=1.5, Education=1.5),
        _dp("Sunday lunchtime", "12:00", 1.0, 0.3, 0.4, 0.5, Informational=1.5, Magazine=1.5, Documentary=1.5, Adventure=1.3, Action=1.3),
        _dp("The Big Match", "14:00", 0.5, 2.0, 0.3, 3.5),
        _dp("Sunday afternoon drama", "16:00", 1.0, 0.4, 1.0, 0.4, 50, Family=2.0, Drama=1.5, Adventure=1.5),
        _dp("Sunday teatime", "17:00", 1.0, 0.3, 1.0, 0.3, 50, Game_Show=3.0, Family=2.0, Drama=1.5),
        _dp("Sunday night", "19:15", 1.0, 0.5, 0.05, 0.2, Drama=2.5, Entertainment=2.0, Comedy=2.0, Mini__series=2.0, Crime=1.8, Action=1.5,
            Game_Show=1.3),
        _dp("Sunday post-watershed", "21:00", 1.0, 1.3, 0.0, 0.3, Drama=2.0, Crime=2.0, Thriller=1.5),
        _dp("Sunday ten o'clock", "22:00", 1.0, 0.3, 0.0, 0.2, 35, Comedy=2.5),
        _dp("Sunday late", "22:30", 0.8, 1.5, 0.0, 1.0, Documentary=1.5, Music=1.5),
    ],
}

# Channel 4 came on the air at teatime in its first years, so its mornings here are the repeats
# and education it later filled them with, and its afternoons the black and white matinee and
# the racing. Countdown and the American repeats at teatime, Brookside at eight, documentary,
# then the imports and comedy it was known for (Hill Street Blues, Cheers at ten), Film on Four
# and a late night of cult films and music. American football on Sunday evening; little sport
# otherwise.
CHANNEL_4 = {
    "weekday": [
        _dp("Morning", "08:00", 1.0, 0.3, 0.4, 0.05, Education=2.0, Documentary=1.5, Comedy=1.2),
        _dp("Lunchtime", "12:00", 1.0, 0.6, 1.0, 0.1, Documentary=1.5, Magazine=1.5, Children=1.3, Comedy=1.3),
        _dp("Matinee", "14:00", 0.6, 2.5, 0.3, 1.0, Western=1.5, War=1.3),
        _dp("Teatime", "16:30", 1.0, 0.1, 1.5, 0.1, 40, Game_Show=3.0, Comedy=2.0, Animation=1.5, Family=1.5, Music=1.5),
        _dp("Early evening", "18:00", 1.0, 0.2, 0.3, 0.2, 55, Comedy=2.0, Documentary=2.0, Informational=1.5, Science_Fiction=1.5, Music=1.3),
        _dp("Eight o'clock", "20:00", 1.0, 0.5, 0.05, 0.2, Soap=2.0, Documentary=2.0, Drama=1.6, History=1.5, Science=1.5),
        _dp("Nine o'clock", "21:00", 1.0, 1.6, 0.0, 0.3, Comedy=2.5, Crime=2.0, Drama=1.8),
        _dp("Late", "22:30", 0.8, 2.2, 0.0, 0.5, Comedy=1.8, Horror=1.5, Music=1.5, Thriller=1.3),
    ],
    "saturday": [
        _dp("Saturday morning", "08:00", 1.0, 0.3, 4.0, 0.05, 60, Children=2.0, Animation=2.0, Comedy=1.3),
        _dp("Racing and the double bill", "13:00", 0.5, 2.2, 0.3, 3.0),
        _dp("Saturday teatime", "17:00", 1.0, 0.3, 0.6, 0.2, Soap=3.0, Comedy=1.5, Music=1.5),
        _dp("Saturday evening", "19:00", 1.0, 1.0, 0.05, 0.2, Documentary=1.8, History=1.5, Music=1.5, Drama=1.5),
        _dp("Saturday nine o'clock", "21:00", 1.0, 1.8, 0.0, 0.3, Comedy=2.0, Drama=1.5, Crime=1.5),
        _dp("Saturday late", "22:30", 0.6, 2.5, 0.0, 0.5, Comedy=1.8, Horror=1.8, Music=1.5),
    ],
    "sunday": [
        _dp("Sunday morning", "08:00", 1.0, 0.3, 1.0, 0.05, 60, Education=1.5, Animation=1.3),
        _dp("Sunday lunchtime", "12:00", 1.0, 0.6, 0.4, 0.1, Family=2.0, Documentary=1.5, Drama=1.3, Comedy=1.3, Western=1.3),
        _dp("Sunday matinee", "14:00", 0.5, 2.8, 0.3, 0.3),
        _dp("American football", "17:00", 0.8, 0.5, 0.3, 2.5, Family=1.3),
        _dp("Sunday evening", "19:00", 1.0, 0.8, 0.05, 0.2, Documentary=1.8, Drama=1.6, Comedy=1.5, History=1.5, Nature=1.5),
        _dp("Sunday film", "21:00", 0.8, 2.0, 0.0, 0.2, Drama=2.0, Comedy=1.5),
        _dp("Sunday late", "22:30", 0.6, 2.2, 0.0, 0.3, Comedy=1.5, Music=1.3),
    ],
}

# Quizzes, game shows and variety were evening television. By day a game show has no place, so
# every part of a day that starts before five bars it (a weight of 0 is a bar, not a small
# preference) unless the row above says otherwise. The listings give two such cases and both
# are kept: Countdown at half past four on Channel 4, and ITV's weekday afternoon quizzes (Mr
# and Mrs, University Challenge).
EVENING_ONLY = ("Game Show", "Entertainment")
for _profile in (BBC_ONE, BBC_TWO, ITV, CHANNEL_4):
    for _rows in _profile.values():
        for _row in _rows:
            if _row["start"] < "17:00":
                for _name in EVENING_ONLY:
                    _row.setdefault("genres", {}).setdefault(_name, 0.0)

# The default channel each profile seeds, by the number it ships with.
BY_DEFAULT_CHANNEL = {1: BBC_ONE, 2: BBC_TWO, 3: ITV, 4: CHANNEL_4}
