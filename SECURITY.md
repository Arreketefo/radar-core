# Security policy

## Supported versions

Security fixes are applied to the latest tagged release. Until a stable `1.0`
release, consumers should pin an exact tag or reviewed commit.

## Reporting a vulnerability

Do not open a public issue with exploit details, credentials, production data or
account identifiers.

Use GitHub's **Security → Report a vulnerability** flow when it is available.
If private vulnerability reporting is not enabled, contact the maintainer
through the GitHub profile and share only enough information to establish a
private reporting channel.

Please include:

- the affected version or commit;
- the component and expected security boundary;
- minimal reproduction steps using synthetic data;
- the likely impact;
- any mitigation already tested.

You should receive an acknowledgement within seven days. Disclosure will be
coordinated after a fix or mitigation is available.

## Scope-sensitive areas

Reports are especially useful for:

- credentials appearing in URLs or logs;
- authorization bypasses in bot control;
- unsafe configuration override handling;
- request floods caused by retry or rate-limit behavior;
- SQL identifier injection in generic store helpers;
- shutdown behavior that can duplicate or lose externally visible actions.
