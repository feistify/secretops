"""check-node.sh prints a proxy as scheme://host:port only, never a user, password or another value."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "infisical" / "check-node.sh"
HIDDEN = "set (not shown: not a plain proxy URL)"
CREDS = " (holds a user and password, not shown)"
SHELLS = [shell for shell in ("sh", "dash", "busybox") if shutil.which(shell)]


def functions() -> str:
    text = SCRIPT.read_text()
    parts = [re.search(rf"^{name}\(\) {{\n.*?^}}\n", text, re.M | re.S) for name in ("describe", "pick", "published")]
    assert all(parts)
    return "".join(part.group(0) for part in parts)


def run(shell: str, body: str, value: str) -> str:
    argv = [shell, "sh"] if shell == "busybox" else [shell]
    result = subprocess.run([*argv, "-c", functions() + body, "check", value],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


CASES = [
    ("http://u:p4ss@proxy.example:8080", "http://proxy.example:8080" + CREDS),
    ('"http://proxy.example:3128/"', "http://proxy.example:3128"),
    ("px:3128", "px:3128"),
    ("u:p/w@px:3128", "px:3128" + CREDS),
    ("http://u:pa/ss@w0rd@px:8080/x", "http://px:8080" + CREDS),
    ("http://u:p@[::1]:3128", "http://[::1]:3128" + CREDS),
    ("http://h:1 K3S_TOKEN=K10secret", "http://h:1"),
    ("http://h:1?token=abc", "http://h:1"),
    ("http://h:1\nTOKEN=abc", "http://h:1"),
    ("http://u:pass", HIDDEN),
    ("http://u:1234 5@h:1", HIDDEN),
    ("http://u:1234;x@h:1", HIDDEN),
    ("http://u:1234'x@h:1", HIDDEN),
    ("http://u:p@ss123 word@h:1", HIDDEN),
    ("http://u:p@4321'x@h:1", HIDDEN),
    ("ftp://h:21", HIDDEN),
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(("value", "expected"), CASES)
def test_describe_prints_only_scheme_and_host(shell, value, expected):
    assert run(shell, 'describe "$1"', value) == expected


SYSTEMD = [
    ('"HTTPS_PROXY=http://u:p!x@h:1" OTHER=x', "http://h:1" + CREDS),
    ('"HTTPS_PROXY=http://u:1234 abcd@h:1" K3S_TOKEN=K10x', HIDDEN),
    ('"HTTPS_PROXY=http://u:p@ss99 x@h:1"', HIDDEN),
    ("A=1 HTTPS_PROXY=http://px:3128 B=2", "http://px:3128"),
    ("K3S_TOKEN=abc CONTAINERD_HTTPS_PROXY=http://px:3128", "http://px:3128"),
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(("environment", "expected"), SYSTEMD)
def test_systemd_environment_shows_only_the_proxy_host(shell, environment, expected):
    body = """printf '%s\\n' "$1" | grep -oE '"[^"]*"|[^ "]+' | pick | { read -r v; describe "$v"; }"""
    assert run(shell, body, environment) == expected


# Real rule shapes: kube-proxy NodePort and ClusterIP, a CNI portmap DNAT
# (nerdctl), Docker's DNAT, and one bound to a host address.
NAT = (
    '-A KUBE-NODEPORTS -p tcp -m comment --comment "default/web" -m tcp --dport 30843 -j KUBE-EXT-ABC\n'
    '-A KUBE-SERVICES -d 10.96.0.1/32 -p tcp -m comment --comment "default/kubernetes:https" -m tcp --dport 443 -j KUBE-SVC-X\n'
    '-A KUBE-SERVICES -d 10.101.2.3/32 -p tcp -m comment --comment "default/web" -m tcp --dport 80 -j KUBE-SVC-Y\n'
    "-A CNI-DN-1234 -p tcp -m tcp --dport 8443 -j DNAT --to-destination 10.4.0.2:8443\n"
    "-A DOCKER ! -i docker0 -p tcp -m tcp --dport 9443 -j DNAT --to-destination 172.17.0.2:443\n"
    "-A CNI-DN-5678 -d 192.0.2.10/32 -p tcp -m tcp --dport 7443 -j DNAT --to-destination 10.4.0.3:443\n"
    # Hand-written shapes: a wider -d, a multiport list, a port range.
    "-A PREROUTING -d 10.0.0.0/8 -p tcp -m tcp --dport 6443 -j DNAT --to-destination 10.9.0.1:6443\n"
    "-A PREROUTING -p tcp -m multiport --dports 5000,6000 -j DNAT --to-destination 10.9.0.2\n"
    "-A PREROUTING -p tcp -m tcp --dport 7000:7100 -j DNAT --to-destination 10.9.0.3\n"
)
LOCALS = "127.0.0.1 192.0.2.10 "
PORTS = [("30843", True), ("8443", True), ("9443", True), ("7443", True),
         ("6443", True), ("6000", True), ("5000", True), ("7050", True), ("7100", True),
         ("443", False), ("80", False), ("3084", False), ("22", False), ("7101", False), ("600", False)]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(("port", "found"), PORTS)
def test_published_finds_nodeports_and_container_ports_not_cluster_ips(shell, port, found):
    argv = [shell, "sh"] if shell == "busybox" else [shell]
    body = functions() + 'nat=$2; if published "$1" "$3"; then echo yes; else echo no; fi'
    result = subprocess.run([*argv, "-c", body, "check", port, NAT, LOCALS],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ("yes" if found else "no")
