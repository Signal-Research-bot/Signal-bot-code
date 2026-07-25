---
name: signal-envelope
description: Documents the four non-obvious traps in parsing signal-cli JSON-RPC envelopes as a linked device — own-messages arriving as syncMessage, mention offsets being UTF-16 code units, receive-mode determining whether messages are lost, and mutation events requiring a retraction path. Use this skill whenever editing envelope.py or receiver.py, whenever a message appears to be missing or duplicated, whenever mention substitution is off by a character, or before changing any signal-cli flag or Docker receiver configuration.
---

# Signal envelope parsing

Four traps. Each produces silent, plausible-looking wrong behaviour rather than an error.

## 1. Your own messages arrive as `syncMessage`, not `dataMessage`

signal-cli runs as a **linked device on the operator's own account**. Messages the operator sends arrive as `envelope.syncMessage.sentMessage`, with the dataMessage fields inlined (`@JsonUnwrapped` on the Java side — `groupInfo`, `message`, `mentions` appear at that level, not nested under `dataMessage`).

The natural filter "ignore syncMessage, it's just device sync noise" discards **100% of the operator's own contributions** — and the operator is usually the most active participant in a group they built a research bot for.

```python
def accepts(envelope, target_group_id):
    data = envelope.get("dataMessage")
    sent = envelope.get("syncMessage", {}).get("sentMessage")
    for m in (data, sent):
        if m and m.get("groupInfo", {}).get("groupId") == target_group_id:
            return m
    return None
```

Dedupe on `(sourceUuid or "self", timestamp)` — the same logical message can be observed twice.

**Test for it:** send a message from the operator's own phone and assert it lands in the cache. This is the regression test that matters most.

## 2. Mention offsets are UTF-16 code units, not codepoints

`mentions[].start` and `.length` are Java string indices — UTF-16 code units (signal-cli #1504). Python strings are sequences of codepoints, so `text[start:start + length]` drifts by **one per non-BMP character** (most emoji) appearing earlier in the message.

```python
def replace_mentions(text: str, mentions: list[dict], label_for) -> str:
    buf = text.encode("utf-16-le")
    for m in sorted(mentions, key=lambda m: m["start"], reverse=True):  # descending!
        lo, hi = m["start"] * 2, (m["start"] + m["length"]) * 2
        buf = buf[:lo] + label_for(m).encode("utf-16-le") + buf[hi:]
    return buf.decode("utf-16-le")
```

Two things are load-bearing: slice in **2-byte units**, and substitute in **descending `start` order** so earlier replacements don't invalidate later offsets.

**Test for it:** a message containing an emoji before an `@mention`. Without the fix the substitution eats one character of the surrounding text — which usually still looks like plausible output.

## 3. `--receive-mode` decides whether messages are lost

Run the daemon as:

```
signal-cli --config /data daemon --tcp 0.0.0.0:7583 --receive-mode=on-connection
```

`on-connection` means signal-cli fetches from Signal's servers **only while a JSON-RPC client is attached**. When the consumer is down, the queue stays server-side and is delivered on reconnect. Any other mode acknowledges messages to Signal *before* the consumer sees them — a crash then loses them permanently, with no error anywhere.

**Do not substitute `bbernhard/signal-cli-rest-api`.** Its fan-out is an unbuffered Go channel with a non-blocking `default:` — a slow or absent consumer drops already-ACKed messages. `RECEIVE_WEBHOOK_URL` does not fix this: the fan-out runs first, webhook errors are logged and the loop continues, with no retry and no queue. `AUTO_RECEIVE_SCHEDULE` is not a poll fallback either; a `receive` consumes all pending messages and races the poller.

**Test for it:** stop the consumer, send three messages, restart, assert all three arrive.

## 4. Messages mutate — the cache and KB need retraction, not append-only

| Envelope field | Meaning | Required handling |
|---|---|---|
| `expiresInSeconds` non-null | Disappearing messages are on | **Hard-drop.** Never cache, never send, never write to the KB. |
| `isExpirationUpdate` | The timer changed | Update group state; re-evaluate what is retained. |
| `remoteDelete` | Sender deleted for everyone | Delete from cache; if it reached the KB, retract the derived entry. |
| `editMessage` | Sender edited | Supersede the original; never keep both as separate claims. |
| reaction removal | Reaction withdrawn | Drop it from derived signal. |

Writing content into a permanent knowledge base after every human client has deleted it is a clearer breach of the senders' intent than anything in the GDPR analysis, and it is trivially detectable from the envelope. There is no excuse for getting this wrong.

## Operational facts worth remembering

- The linked device unlinks after roughly 45 days of receiver inactivity, or ~30 days with the phone offline; max 5 devices. *These figures are corroborated but not primary-verified — confirm in a browser before relying on them.* Re-linking requires a human with the phone; it cannot be automated. Add a `last_seen` health probe.
- signal-cli **cannot** backfill history. On link it requests only GROUPS/CONTACTS/BLOCKED/CONFIGURATION/KEYS. Choose "Don't Transfer" at link time — the archive offer is wasted on it.
- The `/data` directory **is** the linked-device identity. It lives in a Docker named volume, never a bind mount into the repo tree. Back it up only while the container is stopped.
- Sends from this device appear in the group as messages **from the operator** — a linked device has no separate bot identity. Anything `notify.py` posts must be marked so the receiver filters it out, or the next window ingests the bot's own summary and summarises the summary.
