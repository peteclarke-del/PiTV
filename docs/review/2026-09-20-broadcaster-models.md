# The four broadcaster models against 1980s listings (review of 20 September 2026)

Part of the review Pete asked for on 20 September. The daypart seeds in
`pitv/channel_profiles.py` were compared with the period listings already transcribed in
[uk-schedules-1980-1992.md](../research/uk-schedules-1980-1992.md) and with programme and
timeline pages fetched for this pass. BBC Genome, Transdiffusion and TV Cream could not be
fetched this time, so no new full-day listing was added; the Transdiffusion listings below are
the ones in the research report and were not re-verified. What was done with each finding is in
[the review summary](2026-09-20-review.md).

General finding: the seeds are in good shape. The boundaries the listings fix are right
(Grandstand and World of Sport 12:15, Children's BBC 15:55 to 17:35, Children's ITV 16:00 to
17:15, Saturday teatime 17:05, Sunday evening 19:15, Countdown 16:30, Brookside 20:00, the
Saturday Brookside omnibus 17:00). The discrepancies are mostly genre weights that put the right
kind of programme in the wrong row, and a few rows whose name or weights rest on a 1990s memory.
No length cap is proposed inside the peak hours: series are held for 17:30 to 22:30 and films
fill what they do not, so a cap there would bar films and push a thin day into relaxation.

## PiTV One (BBC One)

Weekdays:

- Breakfast 08:00 carried kids 1.0 and Children 1.5. Breakfast Time (from 17 January 1983) was a
  news magazine with no children's strand; children at breakfast was TV-am.
- Early evening 17:35 favoured Soap 3.0 and Game Show 2.5. No BBC One listing shows a quiz
  before 19:00. The hour was news and magazine all decade (news 17:40 and Nationwide to 1983,
  Sixty Minutes, then the Six O'Clock News and the regional magazine from September 1984); the
  only soap there was the Neighbours repeat at 17:35 from January 1988.
- Prime time 19:00 had no Soap weight, although EastEnders at 19:30 on Tuesdays and Thursdays is
  the defining fact of that row from 1985, with Wogan at 19:00, quizzes 19:00 to 19:40 and Top of
  the Pops on Thursdays. The model sent soaps to 17:35 and kept them out of 19:00 to 21:00, the
  wrong way round.
- After the Nine O'Clock News is right; Panorama and Omnibus moved after the news from February
  1985, which justifies a small Documentary weight. The news itself cannot be shown; drama or a
  film from 21:00 is the right stand-in.

Proposed:

    _dp("Breakfast", "08:00", 1.0, 0.05, 0.3, 0.05, 40, Magazine=2.0, Informational=2.0),
    _dp("Early evening", "17:35", 1.0, 0.1, 0.4, 0.2, 40, Magazine=2.5, Informational=2.0, Soap=2.0, Family=1.5, Comedy=1.3, Documentary=1.3),
    _dp("Prime time", "19:00", 1.0, 0.3, 0.05, 0.3, Soap=2.5, Comedy=2.0, Game_Show=1.8, Drama=1.6, Family=1.5, Entertainment=1.5, Music=1.3, Adventure=1.3),
    _dp("After the Nine O'Clock News", "21:00", 1.0, 1.2, 0.0, 0.4, Drama=2.0, Crime=1.6, Thriller=1.4, Documentary=1.4),

Saturday: boundaries match the listing of 1 November 1986. Variety (the Entertainment genre)
was the core of 18:10 to 21:00 and was favoured nowhere; Saturday night drama (Bergerac, Juliet
Bravo) was underweighted.

    _dp("Saturday teatime", "17:05", 1.0, 0.2, 1.5, 0.3, 50, Family=2.5, Game_Show=2.5, Entertainment=2.0, Science_Fiction=2.0, Adventure=2.0, Comedy=1.5),
    _dp("Saturday night", "19:00", 1.0, 0.4, 0.05, 0.3, Game_Show=3.0, Entertainment=2.5, Comedy=2.5, Family=2.0, Drama=1.6, Crime=1.5),

Sunday: the morning was Play School, religion and adult education, with children's television a
matter of twenty minutes; the late evening was Everyman or a short documentary, the Sunday film
being at 21:10.

    _dp("Sunday morning", "08:00", 1.0, 0.2, 0.8, 0.1, 60, Education=2.5, Documentary=1.5, Children=1.2, Animation=1.2),
    _dp("Sunday late", "22:30", 0.8, 0.8, 0.0, 0.5, Documentary=2.0, History=1.5, Science=1.3),

## PiTV Two (BBC Two)

Weekdays:

- "Open University" 08:00 to 11:00 is misnamed: weekday Open University was before 08:00 and late
  at night. What filled 09:00 to 15:00 from 19 September 1983 was Daytime on Two (schools); before
  that the channel was closed by day. The Education weights are the right stand-in; the following
  Daytime row should be education-led too.
- "Six o'clock" 17:30 favoured Science Fiction 3.0 under a 55 minute cap. Cult imports at 18:00
  date from DEF II (May 1988). For most of the decade the slot held a feature film or repeats,
  which the cap barred.
- Nine o'clock and Late are good: comedy at 21:00 and films at 21:30 against a floating Newsnight
  are sourced.

Proposed:

    _dp("Daytime on Two", "08:00", 1.0, 0.1, 0.5, 0.05, 40, Education=3.0, Science=2.0, Technology=2.0, Documentary=1.5, Children=1.3),
    _dp("Daytime", "11:00", 1.0, 0.3, 0.3, 0.3, Education=2.5, Documentary=1.5, Nature=1.5, History=1.5),
    _dp("Six o'clock", "17:30", 1.0, 0.8, 0.5, 1.0, Science_Fiction=2.0, Comedy=1.8, Western=1.5, Family=1.5, Adventure=1.5, Fantasy=1.3),

Saturday: the Saturday morning children's weight is Pete's stated departure from the period (no
BBC Two Saturday listing shows children's programmes before 1989) and stays. Science Fiction in
the early evening and Comedy at nine have no support: the listings show sport to 18:15, an arts
documentary, and films from 21:25.

    _dp("Saturday early evening", "17:30", 1.0, 0.4, 0.3, 1.5, Documentary=2.0, Music=1.5, History=1.5, Nature=1.3),
    _dp("Saturday nine o'clock", "21:00", 0.8, 2.2, 0.0, 1.5, Drama=1.5, Comedy=1.2),

Sunday: Sunday Grandstand ran about 12:50 to 18:30, not 14:00 to 18:00; Comedy at nine has no
support (opera, drama serials, Grand Prix highlights, Moviedrome from 1988).

    _dp("Sunday Grandstand", "13:00", 0.3, 0.8, 0.2, 7.0),
    _dp("Sunday early evening", "18:30", 1.0, 0.4, 0.2, 1.0, Nature=2.0, Documentary=2.0, Magazine=1.5),
    _dp("Sunday nine o'clock", "21:00", 1.0, 1.5, 0.0, 0.8, Drama=1.8, Music=1.5, Documentary=1.3, Comedy=1.2),

## PiTV Three (ITV)

Weekdays: the Morning row favoured magazine and soap, but until 29 June 1987 09:30 to 12:00 was
For Schools in term; soap and magazine mornings are 1987 onwards. Children's ITV, Teatime, Nine
o'clock drama and After News at Ten are right. News at Ten cannot be shown; a film from 21:00
running through it is faithful.

    _dp("Morning", "09:30", 1.0, 0.2, 0.5, 0.1, Education=2.5, Documentary=1.5, Magazine=1.3, Soap=1.2),

Saturday: game shows ended by 20:00 or 20:15 in every listing and a US crime hour took over
(Vegas, T J Hooker, Hunter); after the news came a television film every time.

    _dp("Saturday night", "19:00", 1.0, 0.4, 0.05, 0.3, Game_Show=3.0, Entertainment=2.5, Comedy=2.0, Family=1.5),
    _dp("Saturday action hour", "20:00", 1.0, 0.6, 0.05, 0.3, Crime=2.5, Action=2.5, Adventure=1.5, Drama=1.3, Game_Show=0.8),
    _dp("Saturday post-watershed", "21:00", 0.9, 2.0, 0.0, 0.5, Crime=2.0, Drama=1.8, Thriller=1.5),

Sunday: the morning was worship and adult education with Sesame Street at 09:00; lunchtime was
Weekend World then a US series; the evening was variety by the mid decade (Surprise Surprise,
Live from Her Majesty's); and 22:00 on Sundays was a comedy half hour in place of News at Ten
(Spitting Image from 26 February 1984), with The South Bank Show at 22:30.

    _dp("Sunday morning", "08:00", 1.0, 0.2, 2.0, 0.1, 60, Children=1.5, Animation=1.5, Education=1.5),
    _dp("Sunday lunchtime", "12:00", 1.0, 0.3, 0.4, 0.5, Informational=1.5, Magazine=1.5, Documentary=1.5, Adventure=1.3, Action=1.3),
    _dp("Sunday night", "19:15", 1.0, 0.5, 0.05, 0.2, Drama=2.5, Entertainment=2.0, Comedy=2.0, Mini__series=2.0, Crime=1.8, Action=1.5, Game_Show=1.3),
    _dp("Sunday ten o'clock", "22:00", 1.0, 0.3, 0.0, 0.2, 35, Comedy=2.5),
    _dp("Sunday late", "22:30", 0.8, 1.5, 0.0, 1.0, Documentary=1.5, Music=1.5),

## PiTV Four (Channel 4)

Weekdays: the channel did not open until 16:45 (1982 to 1984) or 14:30 (1984 to 1987); the
Morning and Lunchtime rows stand in with what arrived in 1987 (schools, Business Daily, Just 4
Fun, Sesame Street), which the lunchtime kids weight of 0.3 shut out. Soap at 18:00 has no
support in any listing: the hour was The Munsters, light factual, then Channel 4 News. Matinee,
Teatime (Countdown 16:30), Eight o'clock (Brookside 20:00), Nine o'clock and Late are right.

    _dp("Lunchtime", "12:00", 1.0, 0.6, 1.0, 0.1, Documentary=1.5, Magazine=1.5, Children=1.3, Comedy=1.3),
    _dp("Early evening", "18:00", 1.0, 0.2, 0.3, 0.2, 55, Comedy=2.0, Documentary=2.0, Informational=1.5, Science_Fiction=1.5, Music=1.3),

Saturday: matches the 1984, 1985 and 1990 listings; the morning children's weight is Pete's
departure and stays. Sunday is the weakest evidenced day in the model: no listing was found.
American football at Sunday teatime and family drama at lunchtime are supported; "Film on Four"
on Sunday at 21:00 is unsourced (the first went out on launch night, a Tuesday) and the row
should carry a neutral name.

    _dp("Sunday lunchtime", "12:00", 1.0, 0.6, 0.4, 0.1, Family=2.0, Documentary=1.5, Drama=1.3, Comedy=1.3, Western=1.3),

## What the daypart model cannot express

- Fixed appointments on given weekdays (Wogan, EastEnders, Coronation Street, Brookside, Top of
  the Pops on Thursday, Sportsnight on Wednesday, The Tube on Friday). This matters a great deal
  to the feel. Anchors already give a series a weekly slot or a weekday strip; the fix is seeded
  or suggested anchors for series with famous slots, not more dayparts.
- Weekday-specific sport. Late sport weights make every weeknight a possible Sportsnight; the real
  one was Wednesday. A per-weekday override or a Wednesday sport band would express it.
- News, weather and regional slots. They fixed the grid and are the largest single departure from
  the period's feel. Neighbours absorb them now. If period bulletins can ever be supplied, News is
  already a genre and a band at the fixed times would place them exactly.
- Closedown, Ceefax and the test card. BBC Two was off air most of the day until September 1983,
  BBC One showed Ceefax through much of the daytime until October 1986, Channel 4 opened in the
  afternoon, and everything closed between 23:45 and 01:30. The model fills those hours and
  replays the day overnight. A band whose fill is a Pages from Ceefax loop would express it with
  no scheduler change. This is Pete's decision, given his preference for a working channel.
- Junction times. ITV and Channel 4 ran to the hour and half hour; BBC One used odd times until
  February 1985, then hours and half hours. PiTV rounds starts to five minutes, and only on
  channels that carry adverts.
- Serials stripped across weekdays, omnibus repeats, seasonality, the changes within the decade
  (1983 breakfast, 1985 BBC One relaunch, October 1986 BBC daytime, 1987 to 1988 ITV mornings and
  24 hour running) and regional variation. Minor, or already expressible with a strip anchor.

## Prose to correct when the rows are touched

- The ITV comment in `channel_profiles.py` and PLAN 4.6 say World of Sport ran from 12:30; the
  row and the sources say 12:15.
- The BBC Two comment says Saturday night "ended in the horror double bill"; that was July and
  August of 1975 to 1981 and 1983.

## Sources

Fetched for this pass:
https://en.wikipedia.org/wiki/Timeline_of_BBC_One ,
https://en.wikipedia.org/wiki/Timeline_of_BBC_Two ,
https://en.wikipedia.org/wiki/Timeline_of_ITV ,
https://en.wikipedia.org/wiki/Timeline_of_Channel_4 ,
https://en.wikipedia.org/wiki/1985_in_British_television ,
https://en.wikipedia.org/wiki/Wogan ,
https://en.wikipedia.org/wiki/Newsnight ,
https://en.wikipedia.org/wiki/DEF_II ,
https://en.wikipedia.org/wiki/Horror_Double_Bills ,
https://en.wikipedia.org/wiki/World_of_Sport_(British_TV_programme) ,
https://en.wikipedia.org/wiki/Match_of_the_Day ,
https://en.wikipedia.org/wiki/The_Big_Match ,
https://en.wikipedia.org/wiki/Bullseye_(British_game_show) ,
https://en.wikipedia.org/wiki/Surprise_Surprise_(British_TV_series) ,
https://en.wikipedia.org/wiki/Brookside_(TV_series) ,
https://en.wikipedia.org/wiki/The_Tube_(1982_TV_series) ,
https://en.wikipedia.org/wiki/Timeline_of_American_Football_on_UK_television ,
https://www.tvforum.co.uk/tvhome/sunday-evening-tv-memories-46381/page-2 ,
https://forums.doyouremember.co.uk/forum/tv-movies/television/310235-10-00-pm-comedy-shows-on-sunday-evening-itv .

Listings relied on from the research report (Transdiffusion scans, not re-verified this pass):
the BBC listings of 17 September 1980, 25 April 1986, 1 and 2 November 1986, 26 August 1990 and
18 October 1991; the ITV and Channel 4 listings of 5 October 1980, 14 November 1981, 25 October
1982, 28 January and 21 March 1984, 4 May and 12 June 1985, 2 May 1986, 1 March 1988 and 28
February 1989. Their URLs are in the research report.

Still unverified: any Channel 4 Sunday listing; any ITV Sunday listing after 1980; the clock
times of Bullseye and the American football; the night of Film on Four. BBC Genome
(genome.ch.bbc.co.uk/schedules/bbcone/london/YYYY-MM-DD) would settle the BBC ones by hand; an ITV
or Channel 4 Sunday needs a TVTimes scan.

The Entertainment, Informational, Magazine and Education weights only bite where the catalogue
carries those genres.
