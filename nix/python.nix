# The Python package set: nixpkgs' CPython 3.12 (the version `.python-version` pins and the
# Linux image runs) with the packages nixpkgs lacks or has at another major version, taken from uv.lock.
{ pkgs }:
let
  lockedWheel = pkgs.callPackage ./locked-wheel.nix {
    python = pkgs.python312;
    lockFile = ../uv.lock;
  };
in
pkgs.python312.override {
  packageOverrides = self: _super: {
    # nixpkgs has anthropic 2.3, a different major than the lock's 1.13 the code is written and
    # tested against, and its google-auth dependency drags in a from-source grpcio build.
    anthropic = lockedWheel {
      name = "anthropic";
      dependencies = with self; [
        anyio
        docstring-parser
        httpx2
        jiter
        pydantic
        sniffio
        typing-extensions
      ];
      pythonImportsCheck = [ "anthropic" ];
    };
    # nixpkgs has 1.27; pyproject.toml asks for >=1.31 (the lock has 1.31.0).
    onnxruntime = lockedWheel {
      name = "onnxruntime";
      native = true;
      dependencies = with self; [
        flatbuffers
        numpy
        packaging
        protobuf
      ];
      pythonImportsCheck = [ "onnxruntime" ];
    };
    # import-linter and its Rust-backed graph library are not in nixpkgs; the devShell needs them
    # (`lint-imports`, tests/test_import_contracts.py).
    grimp = lockedWheel {
      name = "grimp";
      native = true;
      pythonImportsCheck = [ "grimp" ];
    };
    import-linter = lockedWheel {
      name = "import-linter";
      dependencies = with self; [
        click
        grimp
        rich
        typing-extensions
      ];
      pythonImportsCheck = [ "importlinter" ];
    };
  };
}
