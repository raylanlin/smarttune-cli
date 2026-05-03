# Security Policy

## Reporting a Vulnerability

This project processes flight logs and does **not** make network connections — analysis runs fully offline.

If you discover a security concern (e.g., a log file crafted to cause code execution, or a dependency vulnerability):

**Please do not open a public issue.** Send details directly to:

- **GitHub Issues (private):** [Create a security advisory](https://github.com/raylanlin/smarttune-cli/security/advisories/new)
- **Email:** raylanlin@users.noreply.github.com

You should receive a response within 48 hours.

## Scope

- CLI code and bundled dependencies
- CI/CD workflows (GitHub Actions — no secrets exposed in logs)

Out of scope:
- Third-party python packages installed via pip (report to their respective maintainers)
- Flight controller firmware itself

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | ✅ |
| older   | ❌ — upgrade to latest |
