# React widget — moved

The previous npm DocsGPT widget that lived in this directory has been
**removed from this fork**. It is not what serves
[docs.aztec.network](https://docs.aztec.network/).

## Where the production widget actually lives

The live Aztec Docs chat widget is a Docusaurus client module in the
`aztec-packages` monorepo:

```
docs/src/components/AztecDocsWidget/
├── index.jsx       # widget React component
└── styles.css      # scoped design tokens
docs/src/clientModules/docsgpt.js   # Docusaurus mounting glue
```

It was added in PR
[AztecProtocol/aztec-packages#22513](https://github.com/AztecProtocol/aztec-packages/pull/22513)
("feat(docs): add DocsGPT widget to documentation site").

That widget calls back into this repo's backend (`/stream`,
`/api/feedback`) — the only thing this repo provides for the widget is
the API surface. UI lives entirely in `aztec-packages`.

## Why we removed the npm widget

The upstream [`docsgpt`](https://www.npmjs.com/package/docsgpt) widget
that previously occupied this directory:

- was not used by any Aztec deployment;
- diverged in look and feel from the Aztec design system;
- created confusion when patching policy/disclaimer changes (edits made
  here would have shipped *only* if a downstream site ever consumed the
  npm package, which Aztec doesn't).

If you need to change widget UX (footer disclaimer, age gate, links to
the [privacy policy](https://aztec.network/privacy-policy) or
[terms of service](https://aztec.network/terms-of-service), styling,
streaming behavior, etc.), edit
`docs/src/components/AztecDocsWidget/index.jsx` in `aztec-packages`,
not this fork.

## Embedding the widget elsewhere

If a non-Aztec site wants to embed a DocsGPT chat against this backend,
the simplest path today is to copy
`docs/src/components/AztecDocsWidget/index.jsx` from `aztec-packages`
and adapt it. The upstream npm widget at
[`arc53/DocsGPT`](https://github.com/arc53/DocsGPT/tree/main/extensions/react-widget)
remains available if you want to start from that instead.
