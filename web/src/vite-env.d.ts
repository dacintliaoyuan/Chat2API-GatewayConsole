/// <reference types="vite/client" />

/** Build-time constants written by vite.config.ts `define` (ADR 0025). */
/** The console this build assembles; checked against the backend at runtime. */
declare const __CONSOLE_ROLE__: "local" | "cloud";
/** True for the candidate lines (`--mode candidate*`), whose demo code a
 *  production build drops. */
declare const __CONSOLE_CANDIDATE__: boolean;
