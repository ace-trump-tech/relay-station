# Security policy

This is a single-user self-hosted MVP. Do not submit real credentials, private
messages, database files, vault keys or authorization callback URLs in issues.

For a vulnerability that could expose credentials or user data, use this
repository's private vulnerability reporting feature if enabled. If private
reporting is unavailable, open a minimal issue requesting a private reporting
channel without exploit details or sensitive data.

The current design assumes one trusted local administrator and one server
process. Remote use requires HTTPS and a protected reverse proxy. The local
vault key is not hardware protected. See README.md for the threat model and
known limitations.
