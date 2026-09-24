# Spec: using the app from an iPad

Status: **planned, not built.** Hosting approach chosen: Mac + Tailscale.

## The constraint that decides everything

Conversion works by shelling out to the Claude Code CLI on a machine that is
logged in with `claude login`. That is what keeps usage on the subscription
instead of a metered API key. So the converter has to run somewhere that can
execute a binary and keep a credential.

This rules out Supabase, Vercel, Netlify and similar for the conversion itself.
Supabase gives Postgres, Storage, Auth and Deno edge functions; none of those
run the `claude` binary as a subprocess. A database would change where notes are
stored, which was never what stopped the iPad from working.

It does **not** rule out a server. `claude setup-token` mints a one-year OAuth
token, set as `CLAUDE_CODE_OAUTH_TOKEN`, which authenticates with a Pro, Max,
Team or Enterprise subscription and can only make model requests. So an
always-on box is possible later. `subprocess_env()` already passes that variable
through untouched — it strips only the variables that move billing off the
subscription.

## Chosen approach: the Mac serves, Tailscale carries

The Mac keeps running the app exactly as it does now, with its existing
`claude login`. Tailscale puts the Mac and the iPad on one private network, so
the iPad reaches the app from anywhere with nothing exposed to the public
internet.

Trade-off accepted: **the Mac has to be awake to convert.**

```
iPad (Safari) ──tailnet──> Mac: uvicorn :8000 ──subprocess──> claude -p
                                      │
                                      └── notes/ and assets/ on the Mac's disk
```

### Setup

1. Install Tailscale on the Mac and the iPad, sign both into the same account.
2. On the Mac, bind the server to the tailnet interface:
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```
   `0.0.0.0` also exposes it to the local Wi-Fi, which is why the password gate
   below is not optional.
3. Find the Mac's tailnet name (`mac-mini.tailnet-name.ts.net`) and open it from
   the iPad. **Add to Home Screen** gives an app-like launcher with no browser
   chrome.
4. Stop the Mac sleeping while you rely on it: System Settings → Lock Screen, or
   run the server under `caffeinate -i`.
5. Optional but worth it: `tailscale cert` issues a real certificate for the
   tailnet name, so the iPad gets HTTPS rather than a warning.

### Run it automatically

A `launchd` agent at `~/Library/LaunchAgents/com.goodnotes-md.plist` with
`RunAtLoad` and `KeepAlive`, invoking uvicorn with the project's working
directory. Without this you have to open a terminal before every session, which
defeats the point of reaching it from a tablet.

## Required before anything is reachable

### 1. Authentication

There is none today. On localhost that is fine; bound to `0.0.0.0` it is not —
anyone on the Wi-Fi, or anyone the tailnet is shared with, can convert notes
against your allowance and read your library.

Minimum viable: a single shared password, checked once, stored as a signed
session cookie.

- `GNMD_PASSWORD` env var. If unset, the app binds to `127.0.0.1` only and logs
  a warning explaining why — safe by default, so the gate cannot be forgotten.
- `POST /api/login` takes the password, compares with `secrets.compare_digest`,
  sets an `HttpOnly`, `SameSite=Lax`, `Secure`-when-HTTPS cookie signed with a
  key derived from the password.
- Middleware rejects every route except `/api/login` and the login page without
  a valid cookie. **`/print` and the asset mount must be covered too** — they
  serve note content and are easy to forget.
- Rate-limit failed attempts, since a shared password is guessable.

This is deliberately not user accounts. One person, one device set, one secret.

### 2. Touch-friendly page reordering

The thumbnail strip reorders with HTML5 drag events, which iOS Safari does not
fire from touch. On the iPad, reordering is simply dead.

Two options, and doing both is cheap:

- **Move buttons.** A `‹` and `›` on each thumbnail that swap it with its
  neighbour. Unglamorous, works everywhere, testable, and accessible by
  keyboard.
- **Touch drag.** `touchstart`/`touchmove`/`touchend` with a long-press to pick
  up, `touch-action: none` on the thumb while dragging, and a transform to
  follow the finger.

Ship the buttons first; they are the reliable path and they also help on
desktop.

### 3. Tablet layout details

The layout already holds at iPad width — verified at 834×1194 with touch
emulation, no horizontal scroll on either view. What still needs attention:

- Tap targets. The thumbnail remove button is a 14px `×`; it wants to be at
  least 44×44 per Apple's guidance.
- The split Markdown/Rendered pane stacks at 900px, so on an iPad in portrait
  you scroll past the whole editor to reach the preview. A segmented
  **Markdown / Rendered** toggle would suit a tablet better than stacking.
- `-webkit-text-size-adjust: 100%` to stop Safari inflating text on rotation.
- The file picker works as-is: GoodNotes → Share → Save to Files, then pick the
  PDF from the app. Worth documenting in the README, since it is not obvious.

## Later, if the Mac being awake becomes annoying

Move the converter to a small VM (Fly.io, Railway, a VPS — roughly $5/month)
with a persistent volume for `notes/` and `assets/`, authenticated with
`CLAUDE_CODE_OAUTH_TOKEN`. Everything above still applies, and the password gate
becomes genuinely load-bearing because the box is on the public internet.

Consider then, not now:

- A real secret manager for the token rather than an env var in a dashboard.
- Backups of the volume, since notes would no longer live on a machine you also
  back up.
- Token expiry: one year, and it fails closed with a login error. Put a reminder
  somewhere.

## Where Supabase would actually fit

Not as the converter, but as a sync target, and only if you want the library
readable when the Mac is off:

- The Mac stays the source of truth and pushes saved notes plus their assets to
  Supabase Storage after each save.
- A small read-only view reads from Supabase, so the iPad can browse past notes
  with the Mac asleep. Converting still needs the Mac.
- Supabase Auth could replace the shared password if this ever becomes
  multi-user.

This is real work for a benefit that only matters if you read old notes away
from the Mac. Worth doing after the app is actually in daily use, if at all.

## Task checklist

- [ ] Password gate, covering `/print` and `/assets`, defaulting to loopback-only
- [ ] Move buttons for page reordering
- [ ] Touch drag for the thumbnail strip
- [ ] 44px tap targets; Markdown/Rendered toggle for narrow screens
- [ ] README section: Tailscale setup, launchd agent, GoodNotes → Files flow
- [ ] Then use it for a week before building anything else here

## Other deferred ideas

- **Per-conversion usage display.** The CLI returns full `usage` and
  `total_cost_usd` in its result envelope and the SSE stream currently discards
  both. Surfacing "~2,100 output tokens" in the preview line, with a running
  total in the library, would make allowance spend visible where it is being
  spent. Small change, self-contained.
- **Transcription review pass.** See `review-pass.md`.
