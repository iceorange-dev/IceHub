# Security policy

## Supported versions

Security fixes are provided for the latest published IceHub release and the
current `main` branch.

## Reporting a vulnerability

Please use a private
[GitHub security advisory](https://github.com/iceorange-dev/IceHub/security/advisories/new).
Do not open a public issue for vulnerabilities that expose credentials, permit
untrusted code execution, overwrite files outside an intended deployment, or
bypass ownership and integrity checks.

Include the affected version, reproduction steps, impact, and the smallest
sanitised log excerpt that demonstrates the problem. Remove Steam account data,
Ryuu credentials, local paths, tokens, depot keys, and personally identifying
information before attaching logs or screenshots.

Maintainers will acknowledge a usable report as soon as practical, investigate
it privately, and coordinate disclosure after a fix or mitigation is available.

## Operational scope

IceHub downloads third-party data and binaries, writes to Steam-related
directories, and can launch external tools. Only use it with software and
accounts you are authorised to manage. A source link, HTTPS transport, or hash
record identifies content; it does not by itself establish trust or legal
permission to use that content.
