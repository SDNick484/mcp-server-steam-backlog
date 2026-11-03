# How the couch score works

The couch score ranks backlog games for playing on the couch with a Steam Deck. It is the
dashboard's `score()` in `backlog.html`; this server computes the same numbers, checked against
the same test vectors.

## The score

score = play + deck + length + metacritic + Deck playtime + controller + Up next

| Term | Points |
|---|---|
| Play state | never played **+30**, tried (under 2 h) **+22**, played 2 h or more **−20** |
| Steam Deck rating | Verified **+30**, Playable **+14**, Unknown **0**, Unsupported **−45** |
| Length (HowLongToBeat) | 5 h or less **+24**, up to 12 h **+18**, up to 25 h **+9**, up to 50 h **0**, over 50 h **−10**; unknown **0** |
| Metacritic | (score − 75) ÷ 2, e.g. 86 → **+5.5**, 61 → **−7**; unknown **0** |
| Played on the Deck | any Deck playtime **+5** |
| Controller | full controller support **+4** |
| Up next | in the Up next lane **+100** |

- **Length** is the main-story time, or main + extras when there's no main-story time.
  Completionist time is never used.
- **Unknown is never bad**: a missing length, Metacritic score or Deck rating adds 0.
- Points are added exactly, with no rounding.

## Ranking

- **Couch picks** are backlog games (not played 2 h+, and not in Skip or Done) that pass the
  active filters, sorted by score, highest first. Ties keep the library's order (alphabetical).
- The **+100 for Up next** usually puts those games first, but not always: an Up next game that
  is Unsupported on the Deck and very long can score below a short, Verified, untriaged one.
  Picks are sorted by score alone, the same way the dashboard sorts them.

## Example

Outer Wilds, never played, Verified, 17 h main story, Metacritic 85:
never played +30, Verified +30, 17 h +9, MC 85 +5 = **74**.

## Related buckets (not part of the score)

- **Overview grid** lengths: under 5 h short, under 15 h mid, under 40 h long, else epic.
- **Length filter** presets: short ≤ 4.9 h, mid 5–14.9 h, long 15–39.9 h, epic 40 h+, on the
  chosen HowLongToBeat measure (main, main + extras, or completionist), with no fallback.
