/// <reference types="astro/client" />

type Env = {
  TURSO_DATABASE_URL?: string;
  /** Read-only token: preferred, and what the dashboard connects with when set. */
  TURSO_TOKEN_RO?: string;
  TURSO_AUTH_TOKEN?: string;
};

type Runtime = import("@astrojs/cloudflare").Runtime<Env>;

declare namespace App {
  interface Locals extends Runtime {}
}

interface ImportMetaEnv extends Env {}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
