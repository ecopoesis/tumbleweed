# A Python package taken as the wheel that uv.lock names, hash and all: for the few packages
# nixpkgs has too old, or does not have (DECISIONS D32). The URL and the SHA-256 come from the
# lock file, so this cannot drift from it and adds nothing for a reviewer to compare by hand.
{
  lib,
  fetchurl,
  stdenv,
  autoPatchelfHook,
  python,
  lockFile,
}:
let
  lock = builtins.fromTOML (builtins.readFile lockFile);
  arch = stdenv.hostPlatform.parsed.cpu.name; # x86_64 or aarch64
  tag = "cp${lib.replaceStrings [ "." ] [ "" ] python.pythonVersion}";
in
{
  name,
  # Wheels with compiled code are `native`: the CPython tag and a manylinux build for this CPU.
  native ? false,
  dependencies ? [ ],
  pythonImportsCheck ? [ ],
}:
let
  entry =
    lib.findFirst (p: p.name == name) (throw "uv.lock has no package ${name}")
      lock.package;
  wanted =
    wheel:
    let
      file = baseNameOf wheel.url;
    in
    if native then
      lib.hasInfix "-${tag}-${tag}-manylinux" file && lib.hasSuffix "${arch}.whl" file
    else
      lib.hasSuffix "-py3-none-any.whl" file;
  wheel =
    lib.findFirst wanted (throw "uv.lock has no suitable wheel of ${name} for ${arch}")
      entry.wheels;
in
python.pkgs.buildPythonPackage {
  pname = name;
  inherit (entry) version;
  format = "wheel";
  src = fetchurl {
    url = wheel.url;
    sha256 = lib.removePrefix "sha256:" wheel.hash;
  };
  nativeBuildInputs = lib.optional native autoPatchelfHook;
  buildInputs = lib.optional native stdenv.cc.cc.lib;
  inherit dependencies pythonImportsCheck;
}
