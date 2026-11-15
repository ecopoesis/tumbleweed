# The `tumbleweed` package (DECISIONS D32): the project built from nixpkgs' Python packages, with
# GTK 4's typelibs and the programs the code runs wrapped in. A package, not an application, so
# that the dev shell's Python can hold it too: the tests start `python -I -m tumbleweed...` and
# need the project importable the way an installed one is, which `PYTHONPATH` cannot do. The source is an allowlist, so
# nothing else of the working tree (a key, a venv, a workspace) can reach the world-readable store.
{
  lib,
  python,
  src,
  runtimeManifest,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  vte-gtk4,
  gdk-pixbuf,
  webp-pixbuf-loader,
  gst_all_1,
  bubblewrap,
  slirp4netns,
  util-linux,
  btrfs-progs,
  libseccomp,
}:
let
  ps = python.pkgs;
  # What the programs run: the sandbox launcher (S14, S22), prlimit (S15), the snapshot backend
  # (S21). systemd-run (the limits scope) is the host's: it is looked up on the `PATH` the user
  # service has, and a host without it runs with the limits it cannot enforce refused (D28).
  runtimePath = lib.makeBinPath [
    bubblewrap
    slirp4netns
    util-linux
    btrfs-progs
  ];
in
ps.buildPythonPackage {
  pname = "tumbleweed";
  version = (builtins.fromTOML (builtins.readFile ../pyproject.toml)).project.version;
  pyproject = true;
  inherit src;

  build-system = [ ps.uv-build ];
  # pyproject.toml asks for uv_build >=0.12.1,<0.13 (what uv.lock's tools use); nixpkgs has 0.13.
  # The backend only reads pyproject.toml and packs src/, so the pin is not checked here.
  pypaBuildFlags = [ "--skip-dependency-check" ];

  dependencies = with ps; [
    anthropic
    httpcore2
    httpx2
    numpy
    onnxruntime
    pydantic
    pygobject3
    regex
  ];

  nativeBuildInputs = [
    wrapGAppsHook4
    gobject-introspection
  ];

  buildInputs = [
    gtk4
    libadwaita
    vte-gtk4
    gdk-pixbuf
    webp-pixbuf-loader
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
  ];

  # wrapGAppsHook4 would wrap each program itself; the Python wrapper does it once, with its
  # arguments (GI_TYPELIB_PATH, XDG_DATA_DIRS, GDK_PIXBUF_MODULE_FILE, ...).
  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=("''${gappsWrapperArgs[@]}")
  '';
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    runtimePath
    # libseccomp.so.2 is loaded by name (`tumbleweed.sandbox.seccomp`); a store path has no
    # system search path to find it on.
    "--prefix"
    "LD_LIBRARY_PATH"
    ":"
    (lib.makeLibraryPath [ libseccomp ])
    # The shell does not start a broker of its own (`tumbleweed.agent.broker_link`): it would run
    # `sys.executable -I -m tumbleweed.broker.daemon`, and the interpreter has none of the packages;
    # each program here adds them to its own sys.path. The broker is `tumbleweed-broker`, started by
    # the user or by S27's systemd service.
    "--set-default"
    "TUMBLEWEED_BROKER_SPAWN"
    "0"
    # The sandbox runtime closure, a store path fixed at build time (`tumbleweed.broker.nixruntime`).
    "--set"
    "TUMBLEWEED_RUNTIME_MANIFEST"
    "${runtimeManifest}"
  ];

  # Every program answers --version without a display, a broker or a key.
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    for program in tumbleweed-shell tumbleweed-sandbox-exec tumbleweed-render \
        tumbleweed-broker tumbleweed-tools; do
      "$out/bin/$program" --version
    done
    runHook postInstallCheck
  '';

  pythonImportsCheck = [ "tumbleweed" ];

  # The tests need a display, bubblewrap and the dev tools: they run in `nix develop` (D32).
  doCheck = false;

  meta = {
    description = "An AI-first OS shell: voice and chat in, generated tools out.";
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
    mainProgram = "tumbleweed-shell";
  };
}
