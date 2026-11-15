# A scan of a directory for files the repository's .gitignore says never to commit, as a shell
# script (`scan DIR [secrets]`), used by the checks of `nix flake check` (DECISIONS D32, I-secrets
# of CLAUDE.md). The patterns are read from .gitignore itself, so a new secret pattern there is
# scanned for here with no second list to keep in step.
{ pkgs }:
let
  lib = pkgs.lib;
  text = builtins.readFile ../.gitignore;
  patternsOf = block: builtins.filter (l: l != "" && !(lib.hasPrefix "#" l)) (lib.splitString "\n" block);
  # The "Secrets (never commit)" section: from its comment to the next blank line. A .gitignore
  # without it fails evaluation (elemAt past the end), which is the right outcome.
  secretsSection = builtins.head (
    lib.splitString "\n\n" (builtins.elemAt (lib.splitString "# Secrets (never commit)\n" text) 1)
  );
  patterns = patternsOf text;
  secrets = patternsOf secretsSection;
  names = ps: map (p: pkgs.lib.removeSuffix "/" p) ps;
  patternFile = name: ps: pkgs.writeText name (pkgs.lib.concatStringsSep "\n" (names ps) + "\n");
in
pkgs.writeShellScript "tumbleweed-secret-scan" ''
  set -euo pipefail
  dir="$1"
  kind="''${2:-all}"
  case "$kind" in
    all) patterns=${patternFile "all-patterns" patterns} ;;
    secrets) patterns=${patternFile "secret-patterns" secrets} ;;
    *) echo "unknown pattern set: $kind" >&2; exit 2 ;;
  esac
  [ -s "$patterns" ] || { echo "no patterns to scan for" >&2; exit 2; }
  found=0
  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    # -iname: case-insensitive, like git here.
    hits="$(${pkgs.findutils}/bin/find "$dir" -iname "$pattern")"
    if [ -n "$hits" ]; then
      echo "ignored files in $dir (pattern $pattern):" >&2
      echo "$hits" >&2
      found=1
    fi
  done < "$patterns"
  exit "$found"
''
