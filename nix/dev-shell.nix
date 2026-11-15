# The development shell (DECISIONS D32): Python with the project's dependencies and dev tools
# from nixpkgs, GTK 4 and friends with their typelibs on the search paths, the sandbox's tools,
# the voice programs, and weston for the headless display `scripts/linux-session` starts.
{
  pkgs,
  python,
  runtime,
}:
let
  pythonEnv = python.withPackages (
    ps: with ps; [
      anthropic
      httpcore2
      httpx2
      numpy
      onnxruntime
      pydantic
      pygobject3
      regex

      hypothesis
      import-linter
      jsonschema
      mypy
      pygobject-stubs
      pytest
      pytest-cov
      pytest-timeout
      types-jsonschema
      types-regex
    ]
  );
  # Piper is a Python application on nixpkgs' default Python (3.14). Put only its program on the
  # PATH: as a build input its Python packages would be added to PYTHONPATH and shadow this env's.
  piper = pkgs.runCommand "piper-program" { } ''
    mkdir -p $out/bin
    ln -s ${pkgs.piper-tts}/bin/piper $out/bin/piper
  '';
in
pkgs.mkShell {
  packages = [
    pythonEnv
    pkgs.uv
    pkgs.ruff
    pkgs.git

    # The sandbox: bubblewrap, slirp4netns, prlimit, libseccomp (loaded by name, below).
    pkgs.bubblewrap
    pkgs.slirp4netns
    pkgs.util-linux
    pkgs.libseccomp
    pkgs.btrfs-progs

    # The UI: GTK 4, libadwaita, VTE, GStreamer, with the typelibs the tests import.
    pkgs.gtk4
    pkgs.libadwaita
    pkgs.vte-gtk4
    pkgs.gdk-pixbuf
    pkgs.webp-pixbuf-loader
    pkgs.gst_all_1.gstreamer
    pkgs.gst_all_1.gst-plugins-base
    pkgs.gobject-introspection
    pkgs.dbus
    pkgs.dejavu_fonts

    # Voice (S06-S08): whisper.cpp's server and Piper, run natively and pointed at by config.
    pkgs.whisper-cpp
    piper

    # The headless Wayland compositor of `scripts/linux-session`.
    pkgs.weston
  ];

  shellHook = ''
    # The source tree, not an installed copy, so that an edit is seen at once.
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
    # libseccomp.so.2 is loaded by name (`tumbleweed.sandbox.seccomp`).
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.libseccomp ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    # GSettings schemas and fonts for GTK, found by the schema path the build hooks collect.
    export XDG_DATA_DIRS="''${GSETTINGS_SCHEMAS_PATH:-}''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
    # Pango needs a font configuration, and the test host has none of its own.
    export FONTCONFIG_FILE="${pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; }}"
    # The sandbox runtime closure, as the installed program has it (`tumbleweed.broker.nixruntime`).
    export TUMBLEWEED_TEST_RUNTIME_MANIFEST="${runtime.manifest}"
  '';
}
