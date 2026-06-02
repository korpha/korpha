# Korpha Theme Contest — community mechanic

**Audience**: Korpha maintainers + community members considering
submitting a theme.

**Status**: docs only — this document defines the rules, submission
flow, and judging criteria. The contest runs through GitHub
Discussions + GitHub PRs.

---

## What's the contest

Every quarter, the Korpha community runs a theme contest.
Members submit a YAML theme file. The top 3 ship as **built-in
themes** in the next Korpha release with author credit in the
release notes, the README, and the `description` field of the theme
itself.

Why this exists:

- **Moat through community ownership of aesthetic.** Hermes ran an
  informal version (the "Hermes Mod" + Pinokio ecosystem); Strike
  Freedom Cockpit was a community submission that became canonical
  reference example. We make it explicit and recurring.
- **Lowers the marketing surface to ~zero.** Screenshots of the
  dashboard already spread organically; the contest gives that
  energy a structure.
- **Built-ins compound.** Each contest cycle adds 3 new themes that
  ship to every install. Year 1 = 12 community-authored built-ins.

---

## Schedule

- **Submissions open** — first day of each calendar quarter (Jan 1,
  Apr 1, Jul 1, Oct 1)
- **Submissions close** — 3 weeks later (Jan 22 / Apr 22 / Jul 22 / Oct 22)
- **Community voting** — week 4 of the quarter, run inside the
  GitHub Discussions thread for that quarter (👍 reaction = vote,
  one set of votes per GitHub account)
- **Top 3 announced** — last day of week 4
- **Built-in PR merged** — within 2 weeks of announcement, shipping
  in the next release

---

## How to submit

### 1. Author your theme

Follow [`THEME_PROTOCOL.md`](THEME_PROTOCOL.md). Drop a YAML at
`~/.korpha/dashboard-themes/<your-name>.yaml`. Iterate until you
like it.

Tips for a contest-worthy theme:

- **Pick a vibe** — "Hawaii sunset," "fluorescent office," "1990s
  IDE," "Bauhaus poster." Themes that try to be everything end up
  being nothing.
- **Test all three densities** — `compact / comfortable / spacious`.
  Your theme should look right in all three, not just the one you
  authored in.
- **Test contrast** — readable text against background ≥ 4.5:1
  per WCAG. Run [WebAIM contrast checker](https://webaim.org/resources/contrastchecker/)
  on your `palette.background` vs `palette.midground`.
- **Mind the warm glow** — `palette.warm_glow` at low alpha (0.06-0.14)
  adds character. Above 0.20 it usually looks like a bug.
- **Read your `custom_css` aloud** — if it's >20 lines you're probably
  fighting the schema. Stop. Use `component_styles` and
  `color_overrides` instead.

### 2. Submit via the GitHub Discussions thread

Open the pinned `[QN YYYY] Theme Contest — submissions` discussion
in [GitHub Discussions](https://github.com/korpha/korpha/discussions)
and post a top-level comment with:

1. **Theme YAML** — paste it as a code block, OR attach as a file
2. **Two screenshots** — dashboard home view + one other view
   (issues, costs, agents — your pick)
3. **One-line tagline** — what's the vibe? Becomes the picker's
   `description` field if you win
4. **Your name + (optional) website / X / GitHub** — for credit in
   the release notes

Comment title format: `[Q3 2026] <theme name> by <your name>`

### 3. Promote your submission

Same thread is where voting happens. Reply to other submissions
saying what you like. The contest doubles as a content engine —
screenshots tend to spread.

---

## Voting

- **Who can vote** — any GitHub user
- **How** — 👍 react on your top 3 picks in the submissions thread.
  One GitHub account = one set of votes
- **No self-votes** — submitting your own = automatic disqualification
  if caught. We trust the community on this; the cost of policing is
  higher than the cost of a missed bad actor

Tied themes break by:

1. **Earlier submission timestamp** wins
2. **Higher non-vote engagement** (replies received) — proxy for
   "people actually wanted to talk about this theme"
3. **Maintainer judgment** — a final call by the Korpha core team

---

## Judging — what gets a theme to the top

Voting is community-driven, but here's the rubric we ask voters to
consider:

| Criterion | What it means |
| --- | --- |
| **Vibe coherence** | Every element (palette + typography + layout) reinforces a single mood. No mismatched fonts. No "kitchen sink" themes. |
| **Solopreneur fit** | The theme should feel right in a "I'm shipping a $5k/mo SaaS at 11pm" context. Not a fashion show, a workhorse with character. |
| **Readability under load** | The dashboard surfaces a *lot* of text. Themes that look great in screenshots but make the inbox unreadable lose. |
| **Original** | New palette, not a re-skin of an existing built-in. We already ship `default / midnight / sage / ember` — submit something we don't have yet. |
| **Self-contained** | One YAML, no external assets that might 404 in 6 months. Inline `data:` URLs welcome for small assets; large remote images are flagged. |

---

## What winners get

- **Built-in status** — your theme ships with every Korpha install
  starting the next release after the contest closes
- **README mention** — your theme + your name + your link appear
  in the [Eval baselines / Themes / Acknowledgments] section of
  [`README.md`](../README.md)
- **Release-notes feature** — the contest result is the lead bullet
  of the next release's announcement
- **Permanent credit** — the theme's `description` field includes
  *"Community submission by @yourname (Korpha Theme Contest QN YYYY)"*
- **Pinned discussion** — your win stays pinned for ~30 days in
  GitHub Discussions

No cash, no swag, no obligation. The reward is ownership of part of
the Korpha visual identity for as long as the project exists.

---

## What winners give up

- The MIT license. Built-in themes ship in the Korpha repo as
  source code. By submitting, you grant Korpha the right to ship
  your theme MIT-licensed alongside the rest of the codebase.
  Authors keep all rights — you can re-publish your YAML elsewhere
  under any license you want — but the *Korpha copy* lives MIT.

---

## What gets rejected

- **Self-votes / vote rings** — if you ask non-contributors to make
  GitHub accounts purely to vote for your theme, it's a no. We can
  usually tell.
- **Themes that violate WCAG AA contrast** for body text against
  background. The dashboard is a working tool; if Mike can't read
  his own KPIs, the theme isn't shipping.
- **Trademarked logos / fonts you don't have rights to.** Don't
  ship a theme whose `assets.logo` is *literally Coca-Cola's logo*.
  We will Google.
- **Themes that disable / break accessibility features.** Removing
  focus rings, hiding error-state colors, etc. — auto-reject.

---

## Who runs it

The Korpha core team (the maintainers + contributors)
moderates the Discussions thread + merges the winning PRs. Community
members do the voting + most of the discussion.

If you want to help organize a future contest (run the thread,
collect submissions into PR-ready batches, draft the announcement
post), reply in the `#themes` discussion — there's room for community
maintainers and we'd rather have help than not.

---

## FAQ

**Q: Can I submit multiple themes?**
A: Yes — but only your highest-voted one counts toward the top 3.
Submit your three best, not seven mediocre.

**Q: What if no themes meet the bar?**
A: Maintainers can decide to ship 0-3 winners. We'd rather skip a
quarter than ship a bad theme. Track record so far: zero skipped
quarters, but the option exists.

**Q: I won — when does my theme ship?**
A: Within 2 weeks of contest close. We open a PR adding your theme
to `korpha/themes/presets.py`, you get tagged as reviewer, you
approve, we merge.

**Q: My theme didn't win — can I still share it?**
A: Of course. Drop the YAML on a Gist, share the link in
Discussions, people can install it locally with one `cp` command.
The contest is about the canonical built-in slot; it's not
gatekeeping the distribution surface.

**Q: I want to use my submission's screenshots in marketing.**
A: Go for it. Korpha is OSS — every screenshot is yours to use.
We just ask for credit (`Korpha dashboard, theme by you`).

---

## Reference

- Theme schema: [`docs/THEME_PROTOCOL.md`](THEME_PROTOCOL.md)
- Where built-ins live: [`korpha/themes/presets.py`](../korpha/themes/presets.py)
- GitHub Discussions: [github.com/korpha/korpha/discussions](https://github.com/korpha/korpha/discussions)
- Inspiration: [Hermes Mod](https://github.com/cocktailpeanut/hermes-mod)
  was a community-built visual editor for Hermes-agent skins; it
  proved the "data-driven theme + drop-in YAML + community ownership"
