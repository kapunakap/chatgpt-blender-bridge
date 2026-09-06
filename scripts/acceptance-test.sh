#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

printf 'ChatGPT Blender Bridge - Local Acceptance Gate\n'
printf '===============================================\n\n'

bash scripts/doctor.sh

printf '\nLOCAL GATE: PASS\n\n'
printf 'The local gate only proves that required local processes/connectivity look healthy.\n'
printf 'It does NOT prove the cloud/tunnel/plugin path.\n\n'
printf 'Final end-to-end acceptance must be run from ChatGPT and must prove:\n'
printf '  [ ] Blender plugin connects without a 502\n'
printf '  [ ] live scene information is retrieved\n'
printf '  [ ] a harmless Blender Python operation executes through the plugin\n'
printf '  [ ] returned state proves it came from the running Blender instance\n'
printf '  [ ] temporary test state is cleaned up and the scene is unchanged\n\n'
printf 'Until all five checks pass through ChatGPT itself: END-TO-END STATUS = NOT PROVEN\n'
