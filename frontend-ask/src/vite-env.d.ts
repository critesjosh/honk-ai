/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ASK_AZTEC_AGENT_KEY: string;
  readonly VITE_ASK_AZTEC_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
