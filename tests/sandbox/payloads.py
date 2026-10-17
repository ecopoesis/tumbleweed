"""Programs that run inside the sandbox in the tests, each printing one JSON object."""

# Interfaces with their flags, connection attempts, routes and a name lookup.
NETWORK = """
import errno, fcntl, json, socket, struct, sys
out = {"ifaces": {}, "connect": {}}
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# /proc/net/dev, not socket.if_nameindex(): glibc's reads interfaces over a netlink socket, which
# the sandbox's filter does not allow.
names = [line.split(":")[0].strip() for line in open("/proc/net/dev").read().splitlines()[2:]]
for name in names:
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
import errno, json, os, socket
lines = open("/proc/self/status").read().splitlines()
status = dict(line.split(":\\t", 1) for line in lines if ":\\t" in line)
def listing(path):
    # Landlock (S15) leaves `/` and `/etc` unlistable, so say that instead of failing.
    try:
        return sorted(os.listdir(path))
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0, str(exc))
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
    "root": listing("/"),
    "etc": listing("/etc"),
    "mount_options": {fields[4]: fields[5].split(",") for fields in mounts},
}
# Every descriptor /proc/self/fd lists, not a guess at a range. Listing opens descriptors of its
# own, so each number is checked again once the listing is closed.
directory = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
numbers = [int(name) for name in os.listdir(directory)]
os.close(directory)
def is_open(number):
    try:
        os.fstat(number)
        return True
    except OSError:
        return False
out["open_fds"] = [n for n in sorted(numbers) if n > 2 and is_open(n)]
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

# Raw syscalls by number: each one's errno name, or "ok". argv[1] is [[label, number, [args]]].
SYSCALLS = """
import ctypes, errno, json, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
out = {}
for label, number, args in json.loads(sys.argv[1]):
    result = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(arg) for arg in args))
    code = ctypes.get_errno()
    out[label] = "ok" if result != -1 else errno.errorcode.get(code, str(code))
print(json.dumps(out))
"""

# The kernel keyring: read, describe, search and add keys, and ask for one. argv: the key's id and
# the numbers of keyctl, add_key and request_key.
KEYRING = """
import ctypes, errno, json, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
key = int(sys.argv[1])
keyctl, add_key, request_key = (int(n) for n in sys.argv[2:5])
SESSION = -3
def call(number, *args):
    ctypes.set_errno(0)
    return libc.syscall(ctypes.c_long(number), *args), ctypes.get_errno()
def outcome(result, code):
    return errno.errorcode.get(code, str(code)) if result == -1 else "ok"
buf = ctypes.create_string_buffer(64)
out = {}
result, code = call(keyctl, ctypes.c_long(11), ctypes.c_long(key), buf, ctypes.c_long(64))
out["read"] = outcome(result, code)
out["secret"] = buf.raw[: max(result, 0)].decode(errors="replace")
out["describe"] = outcome(
    *call(keyctl, ctypes.c_long(6), ctypes.c_long(key), buf, ctypes.c_long(64))
)
out["search"] = outcome(
    *call(keyctl, ctypes.c_long(10), ctypes.c_long(SESSION), ctypes.c_char_p(b"user"),
          ctypes.c_char_p(b"tumbleweed-s15-secret"), ctypes.c_long(0))
)
out["add"] = outcome(
    *call(add_key, ctypes.c_char_p(b"user"), ctypes.c_char_p(b"planted"),
          ctypes.c_char_p(b"x"), ctypes.c_long(1), ctypes.c_long(SESSION))
)
out["request"] = outcome(
    *call(request_key, ctypes.c_char_p(b"user"), ctypes.c_char_p(b"tumbleweed-s15-secret"),
          ctypes.c_char_p(None), ctypes.c_long(0))
)
print(json.dumps(out))
"""

# What a unix socket can do: connect to the stream, datagram and seqpacket listeners whose paths
# are argv[1], argv[2] and argv[3], by every way there is to get a socket, and what still works.
UNIX_SOCKETS = """
import asyncio, errno, json, socket, sys
stream_path, dgram_path, seqpacket_path = sys.argv[1:4]
out = {}
def attempt(label, action):
    try:
        out[label] = action()
    except OSError as exc:
        out[label] = errno.errorcode.get(exc.errno or 0, str(exc))
def connect_stream():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(stream_path)
    return "connected"
def connect_dgram():
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(dgram_path)
        client.send(b"direct")
    return "connected"
def pair_then_connect():
    # A datagram socketpair can be connected to another address afterwards.
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with first, second:
        first.connect(dgram_path)
        first.send(b"via-pair")
    return "connected"
def stream_pair_reconnect():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with first, second:
        first.connect(stream_path)
    return "connected"
def seqpacket_pair_reconnect():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with first, second:
        first.connect(seqpacket_path)
    return "connected"
def stream_pair_works():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with first, second:
        first.send(b"x")
        return "ok" if second.recv(1) == b"x" else "wrong"
def seqpacket_pair_works():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with first, second:
        first.send(b"x")
        return "ok" if second.recv(1) == b"x" else "wrong"
def event_loop():
    asyncio.run(asyncio.sleep(0))
    return "ok"
def tcp_socket():
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    return "ok"
for label, action in [
    ("connect_stream", connect_stream),
    ("connect_dgram", connect_dgram),
    ("pair_then_connect", pair_then_connect),
    ("stream_pair_reconnect", stream_pair_reconnect),
    ("seqpacket_pair_reconnect", seqpacket_pair_reconnect),
    ("stream_pair_works", stream_pair_works),
    ("seqpacket_pair_works", seqpacket_pair_works),
    ("event_loop", event_loop),
    ("tcp_socket", tcp_socket),
]:
    attempt(label, action)
print(json.dumps(out))
"""

# Ordinary process creation, which `clone3` being ENOSYS must not break (glibc falls back).
PROCESSES = """
import ctypes, errno, json, os, subprocess, threading
out = {}
pid = os.fork()
if pid == 0:
    os._exit(7)
out["fork"] = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
done = []
thread = threading.Thread(target=lambda: done.append(1))
thread.start()
thread.join()
out["thread"] = done == [1]
out["subprocess"] = subprocess.run(["/usr/bin/true"], check=False).returncode
spawned = os.posix_spawn("/usr/bin/true", ["true"], {})
out["spawn"] = os.waitstatus_to_exitcode(os.waitpid(spawned, 0)[1])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
result = libc.syscall(ctypes.c_long(435), ctypes.c_long(0), ctypes.c_long(0))  # clone3
out["clone3"] = errno.errorcode.get(ctypes.get_errno(), "?") if result == -1 else "ok"
print(json.dumps(out))
"""

# A system call of the i386 ABI (`int 0x80`) and one of the x32 ABI, on an x86_64 kernel.
I386_SYSCALL = """
import ctypes, mmap, sys
stub = bytes.fromhex("b814000000cd80c3")  # mov eax, 20 (getpid); int 0x80; ret
page = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
page.write(stub)
call = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(page)))
print("before", flush=True)
print("returned", call(), flush=True)
"""
X32_SYSCALL = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
print("before", flush=True)
result = libc.syscall(ctypes.c_long(0x40000000 + 39))  # getpid, through the x32 entry
print("returned", result, flush=True)
"""

# What a sandboxed program does every day, each as "ok" or an errno name: Landlock must leave all
# of it alone.
WORKS = """
import errno, json, os, subprocess
out = {}
def attempt(label, action):
    try:
        action()
        out[label] = "ok"
    except OSError as exc:
        out[label] = errno.errorcode.get(exc.errno or 0, str(exc))
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
def script():
    with open("/workspace/run.sh", "w") as handle:
        handle.write("#!/bin/sh\\nexit 3\\n")
    os.chmod("/workspace/run.sh", 0o755)
    if subprocess.run(["/workspace/run.sh"], check=False).returncode != 3:
        raise OSError(errno.EIO, "the script did not run")
attempt("workspace_write", lambda: write("/workspace/f"))
attempt("workspace_mkdir", lambda: os.mkdir("/workspace/d"))
attempt("rename_across_directories", lambda: os.rename("/workspace/f", "/workspace/d/f"))
attempt("truncate", lambda: os.truncate("/workspace/d/f", 0))
attempt("symlink", lambda: os.symlink("f", "/workspace/d/l"))
attempt("mkfifo", lambda: os.mkfifo("/workspace/fifo"))
attempt("unlink", lambda: os.unlink("/workspace/d/l"))
attempt("rmdir_after", lambda: (os.unlink("/workspace/d/f"), os.rmdir("/workspace/d")))
attempt("tmp_write", lambda: write("/tmp/x"))
attempt("shm_write", lambda: write("/dev/shm/x"))
attempt("dev_null", lambda: write("/dev/null"))
attempt("dev_urandom", lambda: open("/dev/urandom", "rb").read(4))
attempt("read_runtime", lambda: open("/usr/bin/env", "rb").read(4))
attempt("proc_status", lambda: open("/proc/self/status").read())
attempt("list_workspace", lambda: os.listdir("/workspace"))
attempt("exec_from_workspace", script)
print(json.dumps(out))
"""

# What only Landlock stops: each as "ok" or an errno name.
BEYOND_THE_MOUNTS = """
import errno, json, os, stat
out = {}
def attempt(label, action):
    try:
        action()
        out[label] = "ok"
    except OSError as exc:
        out[label] = errno.errorcode.get(exc.errno or 0, str(exc))
def comm():
    with open("/proc/self/comm", "w") as handle:
        handle.write("renamed")
attempt("list_root", lambda: os.listdir("/"))
attempt("list_etc", lambda: os.listdir("/etc"))
attempt("write_proc", comm)
def make_socket_file():
    os.mknod("/workspace/s", stat.S_IFSOCK | 0o600)
    os.unlink("/workspace/s")
attempt("make_socket_file", make_socket_file)
attempt("read_loader_cache", lambda: open("/etc/ld.so.cache", "rb").read(4))
print(json.dumps(out))
"""

# Reads and writes in the granted directories argv[1] (the one the inner ruleset names) and argv[2]
# (the one it may leave out): each as "ok" or an errno name.
TWO_DIRECTORIES = """
import errno, json, os, sys
out = {}
def attempt(label, action):
    try:
        action()
        out[label] = "ok"
    except OSError as exc:
        out[label] = errno.errorcode.get(exc.errno or 0, str(exc))
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
named, other = sys.argv[1:3]
attempt("read_named", lambda: open(f"{named}/file", "rb").read())
attempt("read_other", lambda: open(f"{other}/file", "rb").read())
attempt("write_other", lambda: write(f"{other}/new"))
attempt("list_other", lambda: os.listdir(other))
attempt("write_workspace", lambda: write("/workspace/ok"))
print(json.dumps(out))
"""

# Landlock's scope on abstract unix sockets: connect to one made outside this process's domain, then
# to one made inside it. argv[1] is the outside socket's abstract name.
ABSTRACT_SCOPE = """
import ctypes, errno, json, socket, sys
from tumbleweed.sandbox.sandbox_exec import apply_landlock, landlock_abi
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0  # PR_SET_NO_NEW_PRIVS
abi, why = landlock_abi()
assert abi is not None, why
apply_landlock([("write", "/")], abi)
def connect(name):
    client = socket.socket(socket.AF_UNIX)
    try:
        client.connect("\\0" + name)
        return "connected"
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0, str(exc))
    finally:
        client.close()
inside = socket.socket(socket.AF_UNIX)
inside.bind("\\0" + sys.argv[1] + "-inside")
inside.listen(1)
outside = connect(sys.argv[1])
print(json.dumps({"abi": abi, "outside": outside, "inside": connect(sys.argv[1] + "-inside")}))
"""

# The unix-socket denial against family numbers with high bits, which the kernel drops from the
# `int` it reads: socket and socketpair as raw syscalls with (1 << 32) | AF_UNIX and more, then a
# connect to the listeners (argv[1] stream, argv[2] datagram) by whatever socket that gave.
# argv[3] and argv[4] are the numbers of socket and socketpair.
HIGH_BIT_UNIX = """
import ctypes, errno, json, os, struct, sys
stream_path, dgram_path = sys.argv[1:3]
socket_nr, socketpair_nr = int(sys.argv[3]), int(sys.argv[4])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.connect.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
libc.send.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
FAMILIES = {"plain": 1, "bit32": (1 << 32) | 1, "top32": (0xFFFFFFFF << 32) | 1}
out = {}
def name():
    return errno.errorcode.get(ctypes.get_errno(), "?")
def reach(descriptor, path, kind):
    address = struct.pack("H", 1) + path.encode() + b"\\0"
    if libc.connect(descriptor, address, len(address)) != 0:
        return "connect " + name()
    if kind == 2:
        libc.send(descriptor, b"high-bit", 8, 0)
    return "connected"
for label, family in FAMILIES.items():
    for kind, path in ((1, stream_path), (2, dgram_path)):
        descriptor = libc.syscall(ctypes.c_long(socket_nr), ctypes.c_long(family),
                                  ctypes.c_long(kind), ctypes.c_long(0))
        key = f"socket_{label}_{kind}"
        out[key] = reach(descriptor, path, kind) if descriptor >= 0 else name()
    pair = (ctypes.c_int * 2)()
    result = libc.syscall(ctypes.c_long(socketpair_nr), ctypes.c_long(family),
                          ctypes.c_long(2), ctypes.c_long(0), pair)
    key = f"socketpair_{label}_dgram"
    out[key] = reach(pair[0], dgram_path, 2) if result == 0 else name()
print(json.dumps(out))
"""

# Raw `socket` calls with the families argv[2:] (numbers), the syscall's number being argv[1].
FAMILY_PROBE = """
import ctypes, errno, json, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
out = {}
for text in sys.argv[2:]:
    family = int(text)
    # SOCK_DGRAM: netlink and the inet families make one, and the filter looks at the family only.
    result = libc.syscall(ctypes.c_long(int(sys.argv[1])), ctypes.c_long(family),
                          ctypes.c_long(2), ctypes.c_long(0))
    out[text] = "ok" if result >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")
print(json.dumps(out))
"""
