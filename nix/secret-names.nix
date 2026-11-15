# Whether a file name is one the package's source must never hold: the "Secrets" section of
# .gitignore and the byte-code the interpreter writes beside the source, matched without regard
# to case (git here runs with core.ignorecase, and Nix's file names are case-sensitive).
{ lib }:
name:
let
  lower = lib.toLower name;
in
lower == ".anthropic-key"
|| builtins.match ".*\\.(key|pem|pyc|pyo|pyd)" lower != null
|| lib.hasPrefix ".env" lower
|| lower == ".ds_store"
