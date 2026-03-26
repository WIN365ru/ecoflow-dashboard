# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.5.x   | Yes       |
| < 0.5   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability, please:

1. **Do NOT open a public issue**
2. Open a [private security advisory](https://github.com/WIN365ru/ecoflow-dashboard/security/advisories/new)
3. Include steps to reproduce and potential impact

We will respond within 7 days.

## Credential Safety

- Never commit `.env` files — they contain your EcoFlow credentials
- The `.gitignore` excludes `.env` and `*.db` files by default
- The web dashboard has no authentication — only expose on trusted networks
- MQTT connections use TLS encryption
