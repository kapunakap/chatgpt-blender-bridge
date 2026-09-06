# Security Policy

This project connects a public cloud application to software running on a user's local machine. Treat authentication and tunnel material as secrets.

## Never commit or post

- tunnel credentials, tokens, or secret URLs,
- ChatGPT authentication material,
- raw private tunnel configuration,
- private keys or certificates,
- authorization codes,
- machine identifiers that are not required for debugging,
- unredacted logs containing any of the above.

## Safe bug reports

When filing an issue, prefer:

- component/version information,
- sanitized error messages,
- redacted config structure,
- localhost host/port information when it is not secret,
- clear reproduction steps.

Replace secret values with obvious placeholders such as `<REDACTED>`.

## If a secret is exposed

1. Revoke or rotate the credential immediately.
2. Remove it from public issues, comments, logs, screenshots, and repository content.
3. Assume Git history and third-party caches may retain deleted material.
4. Create a replacement credential only after the exposed one is invalidated.

## Reporting a security issue

Do not open a public issue containing exploit details or credentials. Use GitHub's private security reporting mechanism if it is enabled for this repository, or contact the maintainer privately through an appropriate channel.
