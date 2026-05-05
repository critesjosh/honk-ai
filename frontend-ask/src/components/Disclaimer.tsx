// Compliance disclaimer block. Wording matches the docs widget's
// PLAN-widget-compliance-aztec-packages.md so the two surfaces give
// the same legal posture.

export function Disclaimer() {
  return (
    <div className="ask-disclaimer">
      <span className="ask-disclaimer__label">Notice</span>
      <span className="ask-disclaimer__text">
        AI-generated — informational only. Not investment, tax, or legal advice.
        Always verify against primary sources before acting.
      </span>
    </div>
  );
}
