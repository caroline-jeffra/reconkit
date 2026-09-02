# Security policy

## Supported versions

This project is pre-1.0 and moves as one line. Fixes land on the latest
release only; there are no backports.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

Please report security issues privately, through GitHub's private
vulnerability reporting:

**Security tab → Report a vulnerability**

That opens a thread visible only to you and me. **Do not open a public issue
for a security report** — a public issue discloses the problem to everyone
before there is a fix.

Useful things to include, as far as you have them: what the issue is, how to
reproduce it, which version you saw it on, and what an attacker could achieve.
A rough report is still worth sending; I would rather hear about something
half-confirmed than not at all.

This is a personal project maintained on a best-effort basis. I will try to
acknowledge reports but cannot commit to a response time.

## Scope

**In scope** — anything that makes `reconkit` itself unsafe to run:

- Command injection, path traversal, or unsafe deserialization in the package
- Leaking the Cloudflare API token — into output files, logs, or error text
- A crafted input CSV or HTTP response that causes harmful behaviour beyond a
  failed probe
- Anything that makes the tool send requests a user did not ask for

**Not in scope:**

- **Vulnerabilities this tool finds on third-party websites.** If `reconkit`
  shows you that someone's site exposes usernames, that finding belongs to
  that site's owner — please report it to them, not here. This repository is
  not a disclosure channel for other people's sites.
- Using the tool against systems you have no permission to test. That is a
  misuse of the tool, not a flaw in it — see
  [Authorized use only](README.md#authorized-use-only).
- Reports that a probe was rate-limited, blocked, or returned
  `inconclusive`. That is the tool working as designed.

## No warranty

`reconkit` sends real HTTP requests to real servers. You are responsible for
having permission to send them, and for how you use anything it reports. The
software is provided without warranty of any kind, as set out in the
[LICENSE](LICENSE).
