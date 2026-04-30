# Plan: privacy/ToS compliance for the Aztec Docs widget

**Repo:** `AztecProtocol/aztec-packages`
**Target PR / branch:** the branch backing
[PR #22513](https://github.com/AztecProtocol/aztec-packages/pull/22513)
("feat(docs): add DocsGPT widget to documentation site").

## Why

The widget is the user-facing surface for AI Q&A on
[docs.aztec.network](https://docs.aztec.network/). It's run by the Aztec
Foundation, so it's subject to the published
[privacy policy](https://aztec.network/privacy-policy) and
[ToS](https://aztec.network/terms-of-service). Three compliance items
need to land **before this PR ships**:

1. **Footer disclaimer + policy links.** ToS §5 disclaims investment /
   tax / legal advice. Without a visible disclaimer the widget reads as
   official Aztec advice — that's the single most likely source of a
   complaint.
2. **18+ age gate.** ToS §2 and Privacy Policy §6 both require users to
   be 18+. A first-render notice-and-attestation modal satisfies the bar
   for an informational docs bot.
3. **Brand framing.** The widget is operated by the Foundation, so
   "official" framing is fine — but pair it with the disclaimer above.

The companion backend changes (1-year retention purge, data export,
right-to-erasure) shipped in
[`critesjosh/docsgpt-aztec#54`](https://github.com/critesjosh/docsgpt-aztec/pull/54).
That PR carries the `/api/export_conversations` endpoint and the
`/forget-me` Discord command. The widget itself stores conversations
client-side only, so right-to-erasure on the widget is a "clear my chat
history" button that wipes local state — optional, not strictly
required.

## Files to edit

Everything is in **`docs/src/components/AztecDocsWidget/index.jsx`**
(the 1300-line component added by this PR). One CSS file may also need
a class added in `docs/src/components/AztecDocsWidget/styles.css`.

## Edit 1 — replace the footer line

### Find

In `renderPanel()`, near the bottom of the component (the panel's
"composer" / send-button area), there is a small footer line that reads:

```jsx
<div
  style={{
    marginTop: 8,
    fontFamily: "var(--azw-font-mono)",
    fontSize: 10,
    color: panelFg2,
    letterSpacing: "0.06em",
  }}
>
  AI-generated · Verify important facts
</div>
```

### Replace with

Two short lines: the disclaimer + the policy links. Keep the same
typographic treatment so it reads as fine print, not chrome.

```jsx
<div
  style={{
    marginTop: 8,
    display: "flex",
    flexDirection: "column",
    gap: 2,
    fontFamily: "var(--azw-font-mono)",
    fontSize: 10,
    color: panelFg2,
    letterSpacing: "0.06em",
  }}
>
  <span>
    AI-generated — informational only. Not investment, tax, or legal advice.
  </span>
  <span>
    <a
      href="https://aztec.network/privacy-policy"
      target="_blank"
      rel="noopener noreferrer"
      style={{ color: "inherit", textDecoration: "underline" }}
    >
      Privacy
    </a>
    {" · "}
    <a
      href="https://aztec.network/terms-of-service"
      target="_blank"
      rel="noopener noreferrer"
      style={{ color: "inherit", textDecoration: "underline" }}
    >
      Terms
    </a>
  </span>
</div>
```

That's it for the footer. Don't move it, don't restyle the panel — keep
the diff minimal so this passes review fast.

## Edit 2 — first-run age gate

### Strategy

A controlled modal that:

- Persists acknowledgement in `localStorage` under the key
  **`aztecDocsGPTAgeAck`** (same key the main app uses, so admins who
  have already accepted on the dashboard don't see it twice).
- Renders inside the chat panel (not as a global page overlay) so it
  only affects users who actually open the widget.
- Has two actions: **I am 18+ and agree** (sets the flag, dismisses) and
  **Leave** (closes the panel without setting the flag).
- Is rendered *before* `renderPanel()`'s normal contents, so the user
  cannot read past it.

### Implementation

Inside `AztecDocsWidget`, near the other `useState` hooks:

```jsx
const AGE_ACK_KEY = "aztecDocsGPTAgeAck";

const [ageAcknowledged, setAgeAcknowledged] = useState(() => {
  if (typeof window === "undefined") return true; // SSR — Docusaurus prerender; defer to client
  return window.localStorage.getItem(AGE_ACK_KEY) === "true";
});

function acknowledgeAge() {
  try {
    window.localStorage.setItem(AGE_ACK_KEY, "true");
  } catch {
    /* localStorage may be disabled — accept session-only */
  }
  setAgeAcknowledged(true);
}
```

Then in `renderPanel()`, immediately inside the panel's outer `<div>`
(before the header), add:

```jsx
{!ageAcknowledged && (
  <div
    role="dialog"
    aria-modal="true"
    aria-labelledby="azw-age-gate-title"
    style={{
      position: "absolute",
      inset: 0,
      background: panelBg,
      zIndex: 1,
      display: "flex",
      flexDirection: "column",
      justifyContent: "center",
      padding: 24,
      gap: 16,
    }}
  >
    <h2
      id="azw-age-gate-title"
      style={{
        margin: 0,
        fontFamily: "var(--azw-font-display)",
        fontSize: 20,
        color: panelFg,
      }}
    >
      Before you continue
    </h2>
    <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, color: panelFg }}>
      Aztec DocsGPT is for users aged 18 or older. Answers are
      informational only and do not constitute investment, tax, or legal
      advice.
    </p>
    <p style={{ margin: 0, fontSize: 12, lineHeight: 1.5, color: panelFg2 }}>
      By continuing you confirm you are 18+ and agree to the{" "}
      <a
        href="https://aztec.network/terms-of-service"
        target="_blank"
        rel="noopener noreferrer"
        style={{ color: "inherit" }}
      >
        Terms of Service
      </a>{" "}
      and{" "}
      <a
        href="https://aztec.network/privacy-policy"
        target="_blank"
        rel="noopener noreferrer"
        style={{ color: "inherit" }}
      >
        Privacy Policy
      </a>
      .
    </p>
    <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
      <button
        type="button"
        onClick={() => setOpen(false)}
        style={{
          padding: "8px 14px",
          fontFamily: "var(--azw-font-mono)",
          fontSize: 12,
          background: "transparent",
          color: panelFg,
          border: `1px solid ${panelFg2}`,
          cursor: "pointer",
        }}
      >
        Leave
      </button>
      <button
        type="button"
        onClick={acknowledgeAge}
        autoFocus
        style={{
          padding: "8px 14px",
          fontFamily: "var(--azw-font-mono)",
          fontSize: 12,
          background: accentColor,
          color: "var(--azw-ink)",
          border: "none",
          cursor: "pointer",
          fontWeight: 600,
        }}
      >
        I am 18+ and agree
      </button>
    </div>
  </div>
)}
```

Note `position: absolute; inset: 0;` — the gate covers the whole panel.
The panel's outer container already has `position: fixed`, so absolute
positioning on the gate is correct. If the panel uses `overflow: hidden`
the gate is naturally clipped to the panel boundary.

The `autoFocus` on the **agree** button is the minimum-viable focus
management. A full focus trap is nice-to-have but the bar for an
informational widget is low.

## Edit 3 — (optional) "Clear my chat history" button

This isn't strictly required but it's cheap and it answers a question
legal will probably ask. Add a small icon button to the panel header
next to the existing reset/close buttons that calls `handleReset()`
*and* clears any conversation-id from localStorage if the widget
persists one. Tooltip: **Clear my chat history**.

If the widget doesn't currently persist any chat state across reloads,
`handleReset()` is enough — and a button label like **New chat** is
already adequate. Skip this edit if so.

## Test plan to put on the PR

- [ ] First-load: widget panel opens; age gate is the only thing visible
  inside the panel; **Leave** closes the panel; **I am 18+ and agree**
  dismisses the gate and shows the chat.
- [ ] Reload after accepting: gate does not reappear (`localStorage`
  flag honored).
- [ ] Clear `aztecDocsGPTAgeAck` from devtools, reload: gate reappears.
- [ ] Footer below the composer shows the disclaimer + Privacy / Terms
  links; clicking each opens the canonical aztec.network page in a new
  tab.
- [ ] Keyboard: tab order inside the gate reaches both buttons; the
  **agree** button is focused on open (`autoFocus`).
- [ ] Lighthouse / axe: no new contrast or aria violations vs main.

## Notes

- **localStorage key sharing.** Using the same key
  `aztecDocsGPTAgeAck` as the main DocsGPT React app means a user who
  has acknowledged on the admin dashboard does not see the modal again
  on the docs site (since both run on different origins, it's actually
  per-origin — the shared key is more of a convention than a true
  carry-over).
- **SSR.** Docusaurus prerenders at build time; the `useState` initialiser
  guards on `typeof window === "undefined"`. Without that guard, the
  build would crash on `localStorage`.
- **Don't add Foundation-specific subprocessor disclosures here.** The
  privacy policy on aztec.network needs to be updated separately to
  list AI inference vendors (OpenRouter, OpenAI for embeddings) as
  subprocessors that handle user queries. That's a foundation legal/web
  task, not a code change in this PR.
- **Backend changes already shipped** in
  [critesjosh/docsgpt-aztec#54](https://github.com/critesjosh/docsgpt-aztec/pull/54)
  — 1-year retention purge, GDPR data export, GDPR right-to-erasure.
  Nothing in the widget needs to change for those; they're transparent
  to the client.
