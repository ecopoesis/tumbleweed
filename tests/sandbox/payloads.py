"""Programs that run inside the sandbox in the tests, each printing one JSON object."""

# Interfaces with their flags, connection attempts, routes and a name lookup.
NETWORK = """
import errno, fcntl, json, socket, struct, sys
out = {"ifaces": {}, "connect": {}}
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _, name in socket.if_nameindex():
    request = struct.pack("16sH14x", name.encode(), 0)
    out["ifaces"][name] = struct.unpack("16sH14x", fcntl.ioctl(probe, 0x8913, request))[1]
for host, port in json.loads(sys.argv[1]):
    attempt = socket.socket(socket.AF_INET)
    attempt.settimeout(3)
    try:
        attempt.connect((host, port))
        out["connect"][f"{host}:{port}"] = "connected"
    except OSError as exc:
        out["connect"][f"{host}:{port}"] = errno.errorcode.get(exc.errno or 0, str(exc))
out["routes"] = open("/proc/net/route").read().count("\\n") - 1
out["inet6"] = open("/proc/net/if_inet6").read()
try:
    socket.getaddrinfo("example.com", 80)
    out["dns"] = "resolved"
except OSError:
    out["dns"] = "failed"
print(json.dumps(out))
"""

# What of the host a program can see: each path stat'ed, and each socket connected to.
VISIBILITY = """
import errno, json, os, socket, sys
out = {"stat": {}, "connect": {}}
for path in json.loads(sys.argv[1]):
    out["stat"][path] = os.path.lexists(path)
for address in json.loads(sys.argv[2]):
    name = address.replace("@", "\\0", 1) if address.startswith("@") else address
    client = socket.socket(socket.AF_UNIX)
    try:
        client.connect(name)
        out["connect"][address] = "connected"
    except OSError as exc:
        out["connect"][address] = errno.errorcode.get(exc.errno or 0, str(exc))
out["proc_net_unix"] = open("/proc/net/unix").read()
listed = json.loads(sys.argv[3])
out["listing"] = {p: sorted(os.listdir(p)) if os.path.isdir(p) else None for p in listed}
print(json.dumps(out))
"""

# Reads, writes and listings on granted paths: what each did, as an errno name or "ok".
ACCESS = """
import errno, json, os, sys
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0, str(exc))
def read(path):
    with open(path, "rb") as handle:
        return handle.read().decode()
def write(path, text="written"):
    def action():
        with open(path, "w") as handle:
            handle.write(text)
    return attempt(action)
spec = json.loads(sys.argv[1])
out = {"read": {}, "write": {}, "listing": {}, "mkdir": {}, "unlink": {}}
for path in spec.get("read", []):
    try:
        out["read"][path] = read(path)
    except OSError as exc:
        out["read"][path] = errno.errorcode.get(exc.errno or 0, str(exc))
for path in spec.get("write", []):
    out["write"][path] = write(path)
for path in spec.get("mkdir", []):
    out["mkdir"][path] = attempt(lambda p=path: os.mkdir(p))
for path in spec.get("unlink", []):
    out["unlink"][path] = attempt(lambda p=path: os.unlink(p))
for path in spec.get("list", []):
    try:
        out["listing"][path] = sorted(os.listdir(path))
    except OSError as exc:
        out["listing"][path] = errno.errorcode.get(exc.errno or 0, str(exc))
print(json.dumps(out))
"""

# The process's own situation: capabilities, namespaces, descriptors, mounts.
SELF = """
import json, os, socket
lines = open("/proc/self/status").read().splitlines()
status = dict(line.split(":\\t", 1) for line in lines if ":\\t" in line)
mounts = [line.split() for line in open("/proc/self/mountinfo").read().splitlines()]
out = {
    "cap_eff": status["CapEff"].strip(),
    "cap_bnd": status["CapBnd"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(),
    "pid": os.getpid(),
    "uid": os.getuid(),
    "pids": sorted(int(n) for n in os.listdir("/proc") if n.isdigit()),
    "hostname": socket.gethostname(),
    "ns": {n: os.readlink(f"/proc/self/ns/{n}") for n in os.listdir("/proc/self/ns")},
    "fds": sorted(int(n) for n in os.listdir("/proc/self/fd")),
    "cwd": os.getcwd(),
    "sid": os.getsid(0),
    "pgrp": os.getpgrp(),
    "root": sorted(os.listdir("/")),
    "etc": sorted(os.listdir("/etc")) if os.path.isdir("/etc") else None,
    "mount_options": {fields[4]: fields[5].split(",") for fields in mounts},
}
open_fds = []
for number in range(3, 256):
    try:
        os.fstat(number)
        open_fds.append(number)
    except OSError:
        pass
out["open_fds"] = open_fds
print(json.dumps(out))
"""

# Whether a nested user namespace can be made, and what else a program might try.
NESTED_USERNS = """
import errno, json, os
try:
    os.unshare(os.CLONE_NEWUSER)
    result = "created"
except OSError as exc:
    result = errno.errorcode.get(exc.errno or 0, str(exc))
print(json.dumps({"unshare_user": result}))
"""

# The environment, as the program sees it three ways.
ENVIRONMENT = """
import json, os
def read(path):
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        return f"unreadable: {exc.strerror}"
    return sorted(raw.decode().split("\\0")[:-1] if raw else [])
seen = {"os": dict(os.environ), "self": read("/proc/self/environ"), "init": read("/proc/1/environ")}
print(json.dumps(seen))
"""
