import { useEffect, useState } from 'react';

const STORAGE_KEY = 'aztecDocsGPTAgeAck';

export default function AgeGate() {
  const [acknowledged, setAcknowledged] = useState<boolean | null>(null);

  useEffect(() => {
    setAcknowledged(localStorage.getItem(STORAGE_KEY) === 'true');
  }, []);

  if (acknowledged === null || acknowledged) return null;

  const accept = () => {
    localStorage.setItem(STORAGE_KEY, 'true');
    setAcknowledged(true);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="age-gate-title"
      className="fixed inset-0 z-[1500] flex items-center justify-center bg-black/60 px-4"
    >
      <div className="bg-background text-foreground w-full max-w-md rounded-lg p-6 shadow-xl">
        <h2 id="age-gate-title" className="text-lg font-semibold">
          Before you continue
        </h2>
        <p className="mt-3 text-sm leading-relaxed">
          Aztec DocsGPT is intended for users aged 18 or older. Answers are
          informational only and do not constitute investment, tax, or legal
          advice.
        </p>
        <p className="mt-3 text-sm leading-relaxed">
          By continuing, you confirm you are 18+ and agree to the{' '}
          <a
            href="https://aztec.network/terms-of-service"
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >
            Terms of Service
          </a>{' '}
          and{' '}
          <a
            href="https://aztec.network/privacy-policy"
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >
            Privacy Policy
          </a>
          .
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <a
            href="https://aztec.network"
            className="rounded-md border px-4 py-2 text-sm hover:bg-black/5 dark:hover:bg-white/10"
          >
            Leave
          </a>
          <button
            type="button"
            onClick={accept}
            className="bg-foreground text-background rounded-md px-4 py-2 text-sm font-medium"
          >
            I am 18+ and agree
          </button>
        </div>
      </div>
    </div>
  );
}
