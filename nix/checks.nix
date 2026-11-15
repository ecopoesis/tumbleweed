# `nix flake check` (DECISIONS D32): what can be proved without a display or a user namespace.
# The test suite itself runs in `nix develop` (CI's `nix` job), which has both.
{
  pkgs,
  runtime,
  src,
  self,
  package,
}:
let
  scan = import ./secret-scan.nix { inherit pkgs; };
in
{
  # Builds the package, including its install check: every program answers --version.
  tumbleweed = package;

  # The flake source (what `nix` copied to the store, world-readable) holds no secret-named file.
  # With a git checkout that is the tracked files; a `path:` flake would hold the whole directory.
  flake-source-has-no-secrets = pkgs.runCommand "flake-source-has-no-secrets" { } ''
    ${scan} ${self} secrets
    touch $out
  '';

  # The package's source is the allowlist and nothing in it is ignored by git.
  package-source-is-clean = pkgs.runCommand "package-source-is-clean" { } ''
    ${scan} ${src} all
    expected="README.md
    pyproject.toml
    src"
    [ "$(ls -A ${src})" = "$expected" ] || { echo "unexpected top-level entries:"; ls -A ${src}; exit 1; }
    touch $out
  '';

  # The scan itself works: it must fail on a tree with a key and a PEM file in it. Without this a
  # scan that matched nothing would pass forever.
  secret-scan-detects-a-key =
    pkgs.runCommand "secret-scan-detects-a-key"
      { }
      ''
        mkdir -p fixture/src
        touch fixture/.anthropic-key fixture/src/SERVER.PEM fixture/src/ok.py
        if ${scan} fixture secrets 2> out; then
          echo "the scan passed a tree with a key in it" >&2
          exit 1
        fi
        grep -q anthropic-key out
        grep -qi server.pem out
        touch $out
      '';

  # The sandbox runtime: Python with Pillow and numpy starts as the sandbox starts it (`-I`:
  # no environment, no current directory), and its closure is only that.
  sandbox-runtime =
    pkgs.runCommand "sandbox-runtime"
      {
        nativeBuildInputs = [ pkgs.jq ];
      }
      ''
        ${runtime.env}/bin/python3 -I -c '
        import PIL.Image, numpy
        image = PIL.Image.new("RGB", (4, 4), (10, 20, 30))
        assert numpy.asarray(image).shape == (4, 4, 3)
        '
        jq -e --arg env ${runtime.env} '
          .kind == "tumbleweed.nix-runtime/1"
          and all(.mounts[]; startswith("/nix/store/"))
          and (.mounts | index($env) != null)
          and (.interpreter == $env + "/bin/python3")
          and (.path == $env + "/bin")' ${runtime.manifest} > /dev/null
        # The closure carries nothing of the UI, the project or its tests.
        if jq -r '.mounts[]' ${runtime.manifest} | grep -Ei 'gtk|tumbleweed|pytest|adwaita|gstreamer'; then
          echo "the runtime closure holds more than the sandbox needs" >&2
          exit 1
        fi
        touch $out
      '';
}
