# Security Policy

Mind Grapes is a self-hosted system that guards personal memory — security
reports are taken seriously.

## Reporting a vulnerability

Email **mindgrapes@cotellese.me**. Please do not open a public GitHub issue
for anything exploitable.

Include what you can: affected component (OAuth server, MCP auth gate, Django
web, edge config), reproduction steps, and impact as you understand it.

This is a maintainer-funded personal project, not a company with a security
team — response is best-effort, but acknowledgment within a few days is the
goal. Coordinated disclosure is appreciated; you'll be credited in the fix
unless you prefer otherwise.

## Scope notes for self-hosters

- The security boundary for OAuth clients is the **passkey-gated consent
  step**, not client registration — `/oauth/register` is open by design
  (Dynamic Client Registration; Claude and other clients rely on it).
- The deployment model assumes TLS is terminated at the edge (Tailscale or
  Caddy) and Postgres is bound to loopback. Deviating from
  `docs/deploy.md` changes your exposure.
- Known hardening items are tracked publicly in the issue tracker.

## Supported versions

Only the latest `main` is supported. There are no backported fixes.
