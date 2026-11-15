# The sandbox's runtime as a Nix closure (S20, D27, DECISIONS D32): CPython with Pillow and numpy,
# and a manifest naming every store path of its closure. The broker builds its `RuntimeSpec` from
# the manifest (`tumbleweed.broker.nixruntime`), so a sandbox mounts exactly these store paths
# read-only and nothing from a home directory, and finds `python3` on the `PATH` the manifest
# gives.
{
  pkgs,
  python,
}:
let
  pythonEnv = python.withPackages (ps: [
    ps.numpy
    ps.pillow
  ]);
  # numpy's OpenBLAS starts a thread per CPU with a large buffer each, which the sandbox's
  # memory limit refuses ("OpenBLAS error: Memory allocation still failed"; the Debian image's
  # reference BLAS has no threads). One thread is also all a handler needs. Set by the program
  # itself, so that it holds however the sandbox's environment was built.
  env =
    pkgs.runCommand "tumbleweed-sandbox-python"
      {
        nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
      }
      ''
        mkdir -p $out/bin
        makeWrapper ${pythonEnv}/bin/python3 $out/bin/python3 \
          --set OPENBLAS_NUM_THREADS 1 --set OMP_NUM_THREADS 1
      '';
  closure = pkgs.closureInfo { rootPaths = [ env ]; };
in
{
  inherit env;
  manifest =
    pkgs.runCommand "tumbleweed-runtime.json"
      {
        nativeBuildInputs = [ pkgs.jq ];
        # The store paths in the manifest's text keep the whole closure alive wherever the
        # manifest goes (they are its references).
        interpreter = "${env}/bin/python3";
        path = "${env}/bin";
      }
      ''
        jq --null-input --sort-keys \
          --rawfile paths ${closure}/store-paths \
          --arg interpreter "$interpreter" --arg path "$path" \
          '{
            kind: "tumbleweed.nix-runtime/1",
            mounts: ($paths | split("\n") | map(select(length > 0))),
            interpreter: $interpreter,
            path: $path
          }' > "$out"
      '';
}
