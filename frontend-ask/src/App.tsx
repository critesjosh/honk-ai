import { ChatSurface } from "./components/ChatSurface";
import { SigHeadline } from "./components/SigHeadline";
import { AgeGate } from "./components/AgeGate";

export function App() {
  return (
    <AgeGate>
      <div className="ask-page brand-quiet density-comfortable">
        <header className="ask-header">
          <a href="https://aztec.network" className="ask-header__brand">
            <span className="ask-header__brand-text">Aztec</span>
          </a>
          <span className="ask-header__divider"></span>
          <span className="ask-header__product">
            <span className="dot"></span>Ask Aztec
          </span>
          <nav className="ask-header__links">
            <a href="https://docs.aztec.network/developers" target="_blank" rel="noreferrer noopener">
              Developers
            </a>
            <a href="https://noir-lang.org" target="_blank" rel="noreferrer noopener">
              Noir
            </a>
            <a href="https://docs.aztec.network/network" target="_blank" rel="noreferrer noopener">
              Network
            </a>
            <a href="https://docs.aztec.network" target="_blank" rel="noreferrer noopener">
              Docs
            </a>
          </nav>
          <a
            href="https://docs.aztec.network/developers/getting_started"
            className="ask-header__cta"
            target="_blank"
            rel="noreferrer noopener"
          >
            Start Building <span className="mono">→</span>
          </a>
        </header>

        <main className="ask-main">
          <div className="ask-hero">
            <div className="ask-eyebrow">RAG Assistant · Public Beta</div>
            <h1 className="ask-display">
              <SigHeadline
                text="Ask Aztec anything."
                frags={[
                  [1, 3],
                  [5, 7],
                  [11, 13],
                ]}
              />
            </h1>
            <p className="ask-lead">
              <b>A retrieval bot grounded in Aztec's public documentation</b> — developer
              docs, network docs, Noir, contracts, and the SDK. Ask a question; get an
              answer with citations to the source.
            </p>

            <ChatSurface />

            <div className="ask-foot">
              <span className="ask-foot__left">
                <span className="ask-foot__diamond"></span>
                Privacy — Built In
              </span>
              <span>
                <a href="https://aztec.network/privacy-policy" target="_blank" rel="noreferrer noopener">
                  Privacy
                </a>
                &nbsp;·&nbsp;
                <a href="https://aztec.network/terms-of-service" target="_blank" rel="noreferrer noopener">
                  Terms
                </a>
                &nbsp;·&nbsp;
                <a
                  href="https://github.com/critesjosh/docsgpt-aztec/issues/new"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Send Feedback
                </a>
              </span>
            </div>
          </div>
        </main>

        <footer className="ask-pagefoot">
          <span>© 2026 Aztec Foundation</span>
          <span>
            <a href="https://aztec.network" target="_blank" rel="noreferrer noopener">
              aztec.network
            </a>
            &nbsp;·&nbsp;
            <a href="https://x.com/aztecnetwork" target="_blank" rel="noreferrer noopener">
              @aztecnetwork
            </a>
            &nbsp;·&nbsp;
            <a href="https://discord.gg/aztec" target="_blank" rel="noreferrer noopener">
              Discord
            </a>
          </span>
        </footer>
      </div>
    </AgeGate>
  );
}
