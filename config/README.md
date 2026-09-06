# Configuration

Only sanitized examples belong in this directory.

## Current state

`env.example` contains the local Blender MCP target used by the diagnostic scripts.

A runnable `tunnel-client.yaml.example` is **intentionally not committed yet** because the exact current tunnel-client schema/command line must be captured from a successful end-to-end repair first. Publishing a guessed schema would make this repository harder to use.

Issue #1 is the gate for adding the first verified tunnel-client example.

## Rule for future examples

Before committing a tunnel config example:

1. prove it with a real ChatGPT → tunnel → Blender call,
2. replace every credential/private endpoint with a placeholder,
3. verify the example contains no machine-specific identifiers,
4. document the tested tunnel-client version in `versions.env`.
