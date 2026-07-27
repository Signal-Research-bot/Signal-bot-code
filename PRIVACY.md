# Privacy

**Read this if you are in the group whose messages this software processes.**
It is written for you, not for engineers. Nothing here is marketing.

## What this thing does

It reads the group's messages, strips out who said what, sends the stripped
text to an AI service (Anthropic's Claude) to pull out research questions and
try to answer them, and writes the answers into a private notes archive that
only group members can read.

## The single most important thing to understand

<!-- scrub-ok: privacy-word-overclaim -->
**This is pseudonymisation, not anonymisation.** Those are different, and the
difference matters to you.

Your name and number are replaced with a stable label — "Participant C" — that
means *you* in every batch and every note. It is not random per message. That
is deliberate: without it the AI cannot tell who asked a question from who
answered it, and the output becomes useless.

The consequences you should know about:

- **The operator can reverse it.** They hold the key that maps labels back to
  people. That is not a flaw to be fixed — it is the only way anyone could
  honour a request from you to delete or show your data.
- **Writing style is not disguised.** Published research shows authors can be
  re-identified from text with names stripped out, at high rates, and that
  language models can infer personal attributes from it. If you have a
  distinctive way of writing, a distinctive job, or you mention a distinctive
  fact, the label does not hide you from someone who already knows the group.
- **Nobody claims otherwise anywhere in this repository.** The stronger word is
  never used about this system, and an automated check blocks it from being
  committed.

## What is removed before anything leaves the machine

- Phone numbers — with or without a country code, with or without separators,
  and written in non-Latin numerals. A member's own number, if it is on the
  roster, is caught in **any** form. Someone else's number is caught when it is
  a real, assignable number; the ranges reserved for fiction and documentation
  (`+44 7700 900xxx`, some `+1 555`) are not caught, because they belong to
  nobody, and the same check that recognises real numbers is what stops a
  reserves figure like `127000000000` being blanked out of the research.
- Names, nicknames, and @mentions of group members, including possessives
- Email addresses, account IDs, IBANs
- The group's name and ID
- Attachment filenames (only a count is kept — `holiday-with-<name>.jpg` is
  itself identifying)
- Exact timestamps, rounded to 15 minutes — a millisecond timestamp plus a
  message length is enough to match a "stripped" transcript against anyone
  else's copy of the chat

## What is deliberately kept

Links and cryptocurrency addresses are **not** removed, because in this group
they are usually the subject of the discussion rather than anything personal.
Links to personal profiles (LinkedIn, Telegram, payment handles, and similar)
*are* removed — whether or not they are written with the `https://` on the
front. The system counts how often it keeps a link so the operator can check
that this judgement is holding up.

If you think that trade-off is wrong, say so — it is one line of configuration.

## Links you post may be opened and read

If you post a link and someone makes a claim about what it says, the research
model may **retrieve that page and read it** in order to check the claim. Three
things bound that, and they are enforced in code rather than promised:

- Only links that survived the removal above, exactly as you typed them. A
  personal-profile link is never fetchable, because it never leaves this
  machine in the first place. Neither is a link the model got slightly wrong,
  or invented — the address is checked character-for-character against what was
  actually posted before anything is retrieved.
- A small number of pages per question, with a size cap on each.
- Nothing is sent to the site you linked beyond an ordinary request for the
  page. Nothing about the group, the conversation, or who posted it goes with
  it.

The page's contents are treated as a claim to be verified, not as fact, and
they pass back through the same filter as everything else before reaching the
archive.

This can be switched off entirely by the operator, in one line of
configuration, without changing anything else.

## What is never processed at all

- **Disappearing messages.** If the group has a disappearing-message timer on,
  those messages are dropped immediately and never stored. Keeping them would
  contradict the whole point of switching the timer on.
- **Deleted and edited messages.** If you delete a message for everyone, it is
  removed here too, even if it was already cached. If you edit one, only the
  edited version survives.
- **Anyone who opts out.** Ask the operator and your messages are dropped at
  the point of collection — along with your reactions and any text of yours
  quoted by others.
- **Any single message you mark.** Type `[research-bot]` anywhere in a message
  and it is dropped at collection. That marker exists so the bot's own posts
  are not re-processed, and it works for anyone — no need to ask first.

Messages the bot itself posts carry that same visible `[research-bot]` tag, so
you can always tell which messages came from the automation rather than from
the operator typing.

## What reaches Anthropic, and what they do with it

The stripped transcript, and only that. Anthropic's commercial terms state they
do not train models on API inputs. API inputs are not retained by default for
the models used here.

Two honest caveats:

- If Anthropic's safety systems flag a request, that content can be retained
  for up to two years, and **there is no signal back to us that this happened**.
  We cannot verify it either way.
- If the operator ever pastes real content into Anthropic's web Console to
  debug something, that is outside those guarantees. The project rule is
  synthetic test data only, but it is a rule, not a technical control.

## What is stored on the operator's machine

An encrypted database of the raw messages, so a crash does not lose anything
between collection and processing. It is encrypted with SQLCipher, and the
project verifies the file is genuinely encrypted rather than assuming it.

The mapping from labels back to people is stored separately, and its key lives
in the operating system's credential store, not in a file next to the data.

## What cannot be undone

- **Anything already sent has been sent.** Opting out stops future collection.
  It cannot retrieve a batch that already went to Anthropic.
- **Access to the notes archive is not retroactive.** If someone leaves the
  group and their access is revoked, any copy they already downloaded is still
  theirs. Revocation stops future updates, nothing more.

This is why the strength of the stripping matters more than the access
controls, and why the operator was asked to tell the group *before* switching
this on rather than after.

## How to check any of this yourself

Every claim above is enforced by code you can read. The whole privacy story
lives in one file, [`signal_research_bot/egress.py`](signal_research_bot/egress.py) —
it is the only part of the system permitted to talk to the network, and it
refuses to send anything that fails its checks.

If you do not read code, the next best thing is
[`tests/test_egress.py`](tests/test_egress.py), where each test is a plain
sentence about something the system must not do.

## Asking for something

Ask the operator directly. You can request:

- To be excluded entirely, from now on
- To see what is stored about you
- To have your entries removed from the notes archive

The label mapping exists so that these requests can actually be carried out.
