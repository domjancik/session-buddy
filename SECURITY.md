# Security

## Reporting a vulnerability

Report privately through [GitHub's private vulnerability reporting](https://github.com/domjancik/session-buddy/security/advisories/new),
or by email to hi@domj.net. Please do not open a public issue for a vulnerability.

Include what an attacker could reach and the steps to reproduce. **Redact your transcripts** —
describe the shape of the data rather than pasting it.

## What this tool touches

Session Buddy reads local agent transcripts and writes a SQLite index at
`~/.session-buddy/index.sqlite`. That index contains your conversations in full: prompts,
tool output, and anything that was ever on screen.

Two consequences worth stating plainly:

- **The index is as sensitive as your session history.** It is a single file, unencrypted, with
  ordinary file permissions. Anything with read access to your home directory can read it.
- **Nothing is sent anywhere.** There are no network calls in `src/`, and adding one is
  out of scope for this project. Vulnerability reports about data leaving the machine are
  therefore especially welcome, because that would be a bug by definition.

Reads of a tool's own store are read-only; the index is the only file written.
