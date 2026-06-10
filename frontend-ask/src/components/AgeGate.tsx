import { useState } from "react";

// First-run age gate. Persists acknowledgement in localStorage under
// the SHARED key `aztecDocsGPTAgeAck` — same key the docs widget
// uses, so a user who already accepted on the docs site doesn't see
// it again here.
const ACK_KEY = "aztecDocsGPTAgeAck";

function readAck(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(ACK_KEY) === "true";
  } catch {
    // localStorage disabled (Safari private mode, embedded contexts).
    // Fall back to session-only acknowledgement: gate stays up until
    // user clicks Accept, then state-only.
    return false;
  }
}

export function AgeGate({ children }: { children: React.ReactNode }) {
  const [acknowledged, setAcknowledged] = useState(readAck);

  function accept() {
    try {
      window.localStorage.setItem(ACK_KEY, "true");
    } catch {
      /* ignore — session-only acceptance is acceptable */
    }
    setAcknowledged(true);
  }

  if (acknowledged) return <>{children}</>;

  return (
    <div className="age-gate__backdrop" role="dialog" aria-modal="true" aria-labelledby="age-gate-title">
      <div className="age-gate__card">
        <div className="ctx-card__eyebrow">Before you continue</div>
        <h2 id="age-gate-title" className="age-gate__title">
          You must be 18 or older
        </h2>
        <p className="age-gate__body">
          Ask Aztec is provided by the Aztec Foundation under our{" "}
          <a
            href="https://aztec.network/terms-of-service"
            target="_blank"
            rel="noreferrer noopener"
          >
            Terms of Service
          </a>{" "}
          and{" "}
          <a
            href="https://aztec.network/privacy-policy"
            target="_blank"
            rel="noreferrer noopener"
          >
            Privacy Policy
          </a>
          . By continuing you confirm you are at least 18 years old and agree to those terms.
        </p>
        <div className="age-gate__actions">
          <button type="button" className="ask-header__cta" onClick={accept}>
            I am 18+ and agree <span className="mono">→</span>
          </button>
          <a
            className="age-gate__leave"
            href="https://aztec.network"
            rel="noreferrer noopener"
          >
            Leave
          </a>
        </div>
      </div>
    </div>
  );
}
