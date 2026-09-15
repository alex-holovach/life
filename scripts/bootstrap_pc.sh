#!/bin/sh
# One-time Linux setup. Run as the installation user; sudo prompts in the terminal.
set -eu
life_user=$(id -un)
[ "$life_user" != root ] || { echo 'Run as your regular installation user.' >&2; exit 1; }
command -v docker >/dev/null
command -v tailscale >/dev/null
sudo usermod -aG docker "$life_user"
sudo systemctl enable --now docker.service
sudo tailscale set --operator="$life_user"
printf '%s\n' 'Server prerequisites configured. Open a new SSH session so Docker group membership takes effect.'
