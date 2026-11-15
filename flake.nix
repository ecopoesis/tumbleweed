{
  description = "Tumbleweed: an AI-first OS shell (voice and chat in, generated tools out)";

  # nixos-unstable, pinned by flake.lock: nixos-26.05 has anthropic 0.97, httpx2/httpcore2 2.3,
  # numpy 2.4 and regex 2026.4, all below the floors in pyproject.toml (DECISIONS D32).
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      # The systems the target runs on (ARCHITECTURE section 11). macOS has no bubblewrap and no
      # Linux sandbox: it uses the container or a Linux builder (README).
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forSystems = lib.genAttrs systems;

      # The source of the package, spelled as an allowlist: pyproject.toml, the README it names
      # as its readme and src/. A flake from a git checkout holds only tracked files, but a `path:`
      # flake holds everything in the directory, and the store is world-readable: nothing outside
      # the list, and nothing key-like inside it, is ever copied.
      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./README.md
          (lib.fileset.fileFilter (file: !(import ./nix/secret-names.nix { inherit lib; } file.name)) ./src)
        ];
      };

      perSystem =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = import ./nix/python.nix { inherit pkgs; };
          runtime = import ./nix/runtime.nix { inherit pkgs python; };
          tumbleweed = pkgs.callPackage ./nix/package.nix {
            inherit python src;
            runtimeManifest = runtime.manifest;
          };
        in
        {
          packages = {
            inherit tumbleweed;
            default = tumbleweed;
            sandbox-runtime = runtime.env;
            sandbox-runtime-manifest = runtime.manifest;
          };
          devShells.default = import ./nix/dev-shell.nix {
            inherit
              pkgs
              python
              runtime
              tumbleweed
              ;
          };
          checks = import ./nix/checks.nix {
            inherit
              pkgs
              runtime
              src
              self
              ;
            package = tumbleweed;
          };
        };
      outputs = forSystems perSystem;
    in
    {
      packages = lib.mapAttrs (_: o: o.packages) outputs;
      devShells = lib.mapAttrs (_: o: o.devShells) outputs;
      checks = lib.mapAttrs (_: o: o.checks) outputs;
      apps = forSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.tumbleweed}/bin/tumbleweed-shell";
          meta.description = "Run the Tumbleweed shell";
        };
      });
    };
}
