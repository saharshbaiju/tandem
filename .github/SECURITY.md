# Security policy

## Supported versions

Only the latest release gets security fixes. The app and the Linux side
should be updated together, since they share one protocol.

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately through GitHub instead: go to the
[Security tab](https://github.com/saharshbaiju/tandem/security) and click
**Report a vulnerability**. If that doesn't work for you, email
saharshbaiju@gmail.com.

Include what an attacker can do, what they need (same Wi-Fi, an app on the
phone, a local account on the PC), and the steps to reproduce it. I'll reply
within a week, and I'll credit you in the release notes unless you'd rather I
didn't.

## How Tandem is secured

[`docs/SECURITY.md`](../docs/SECURITY.md) describes the threat model, the
pairing and TLS design, and how it was tested. It's worth reading before you
report, so you can see which risks are already covered.
