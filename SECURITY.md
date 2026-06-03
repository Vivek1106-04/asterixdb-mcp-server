# Security Policy

## Supported Versions

This project is under active development. Security fixes are applied to the
latest released minor version on the `main` branch. Older versions are not
maintained.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the **Security** tab → **Report a vulnerability**).

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof-of-concept.
- The affected version or commit.
- Any suggested remediation.

### Response expectations

- **Acknowledgement** within 3 business days.
- **Triage and severity assessment** within 7 business days.
- **Fix or mitigation timeline** communicated after triage.

Please allow a reasonable disclosure window before any public discussion.

## Security Model

This gateway is a standalone sidecar between an LLM client and an AsterixDB
Cluster Controller (CC). Its trust boundaries:

- **Read-only enforcement is delegated to the database.** The gateway hardcodes
  `readonly=true` on every query sent to the CC `/query/service` endpoint. The
  CC is the single authority on mutation rejection; the gateway never parses
  SQL++ and never maintains a mutation deny-list.
- **Egress is bounded.** Each query is constrained by a wall-clock timeout and a
  response byte ceiling to limit resource exhaustion from a single request.
- **Requests are namespaced and auditable.** Every query carries a namespaced
  `client_context_id` for end-to-end traceability.
- **Optional shared secret.** When `ASTERIXDB_MCP_CC_SHARED_SECRET` is set, the
  gateway attaches it as an `X-Gateway-Secret` header on the CC hop.

### Operator responsibilities

- Run the gateway and the CC over a trusted network path, or terminate TLS in
  front of the CC. The gateway does not add transport encryption itself.
- Treat the CC base URL and shared secret as sensitive configuration. Supply
  them via environment variables or a secret manager — never commit them.
- Restrict the CC account/role reachable by the gateway to the minimum
  privileges required for read-only access.

## Scope

In scope: vulnerabilities in this gateway's code that enable mutation despite
`readonly=true`, bypass of egress limits, leakage of the shared secret or
`client_context_id` correlation data, or injection into the CC request.

Out of scope: vulnerabilities in Apache AsterixDB itself (report those to the
[AsterixDB project](https://asterixdb.apache.org/)), and issues requiring a
pre-compromised host or network.
