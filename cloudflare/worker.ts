/**
 * Single Worker deployment entry point.
 *
 * Security-sensitive Access JWT verification, private R2 access, and
 * worker-first static routing live in the web-owned canonical implementation.
 * Keeping this as a re-export prevents a second, divergent authentication
 * implementation from being deployed at the repository root.
 */
export { default } from "../web/edge/worker";
