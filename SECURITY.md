# Security Policy

## Supported versions

The current release and the `main` branch are supported. Older tags are not
maintained, and fixes are not backported to them.

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately to sohilmomin2000@gmail.com, or use GitHub's private security
advisory on this repository (*Security → Report a vulnerability*). A private
advisory is preferred: it keeps the report, the fix and the release in one place.

Please include:

- what the issue is, and which version or commit it affects;
- the steps to reproduce, or a proof of concept;
- the impact you believe it has;
- any suggested fix.

## What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | within 48 hours |
| Initial assessment | within 7 days |
| Fix or mitigation | within 30 days for high severity |

## Scope

In scope:

- a file that decodes to something other than what it claims (a round-trip that
  is not byte-exact);
- path traversal or writing outside the destination directory;
- a container or archive that crashes, hangs or exhausts memory instead of being
  refused;
- the web interface being reachable from another machine, or serving a file
  outside its session directory.

Out of scope:

- a deliberately corrupt file being *accepted* when it is also detectable — send
  it anyway, that is a bug worth fixing;
- resource use proportional to the input (compression is CPU-bound by design);
- the absence of authentication in the web UI: it binds to `127.0.0.1` and is
  documented as local-only. Binding it to a public interface is a misconfiguration
  and is your responsibility.

## Design notes that matter for reports

- Every write is a temp file plus `os.replace`; a crash should never damage the
  original.
- Archive members are checked for traversal before anything is created.
- `kosh verify` recomputes every hash; if it says intact and a byte differs, that
  is a security-relevant bug, not a compression bug.
