#!/bin/sh
# Read-only check of a Linux host before installing the self-hosted Infisical
# on it, with nerdctl compose or with Helm on a Kubernetes node. It changes
# nothing, prints no secret and reads no Kubernetes Secret. Run it as the
# human; with sudo it can also look at containerd's namespaces:
#
#   sh check-node.sh            # or: sudo sh check-node.sh
#
# Proxy variables are reported as set or not set, never with their values
# (they can hold a password). Paste the output to whoever helps you; it holds
# host names, addresses and versions, nothing else.
set -u

say() { printf '%-6s %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
first_line() { "$@" 2>/dev/null | head -n 1; }

echo "== Host"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    say INFO "OS: ${PRETTY_NAME:-unknown}"
fi
say INFO "kernel $(uname -r), $(uname -m), $(getconf _NPROCESSORS_ONLN 2>/dev/null || echo '?') CPUs"
awk '/^MemTotal:/ { printf "INFO   memory: %.1f GB\n", $2 / 1048576 }' /proc/meminfo
for dir in / /var/lib /opt; do
    [ -d "$dir" ] && df -Ph "$dir" | awk -v d="$dir" 'NR == 2 { printf "INFO   free on %s: %s (%s)\n", d, $4, $6 }'
done
say INFO "host name: $(hostname -f 2>/dev/null || hostname)"
if have ip; then
    say INFO "IPv4: $(ip -4 -o addr show scope global 2>/dev/null | awk '{ sub("/.*", "", $4); printf "%s ", $4 }')"
fi
[ "$(id -u)" = 0 ] && say INFO "running as root" || say INFO "running as $(id -un) (sudo shows containerd namespaces too)"

echo "== Accounts with the vault containers' uids (compose variant)"
for uid in 61001 61002 70; do
    if have getent; then
        name=$(getent passwd "$uid" | cut -d: -f1)
    else
        name=$(awk -F: -v u="$uid" '$3 == u { print $1 }' /etc/passwd)
    fi
    if [ -n "$name" ]; then
        say WARN "uid $uid belongs to host account '$name': change it in compose.yaml or use Helm"
    else
        say OK "uid $uid: no host account"
    fi
done

echo "== Ports"
if have ss; then
    for port in 80 443 8443; do
        if ss -Hltn "sport = :$port" 2>/dev/null | grep -q .; then
            say WARN "port $port is in use"
        else
            say OK "port $port is free"
        fi
    done
else
    say INFO "ss not found; ports not checked"
fi

echo "== Container tools"
for tool in docker podman nerdctl ctr crictl containerd buildkitd; do
    if have "$tool"; then
        path=$(command -v "$tool")
        say INFO "$tool: $path ($(first_line "$tool" --version))"
    fi
done
if have systemctl; then
    for unit in docker containerd kubelet k3s k3s-agent rke2-server rke2-agent snap.microk8s.daemon-kubelite; do
        state=$(systemctl is-active "$unit" 2>/dev/null)
        [ "$state" = active ] && say INFO "service $unit: active ($(systemctl show -p FragmentPath --value "$unit" 2>/dev/null))"
    done
fi
for sock in /run/containerd/containerd.sock /run/k3s/containerd/containerd.sock /var/snap/microk8s/common/run/containerd.sock; do
    [ -S "$sock" ] && say INFO "containerd socket: $sock"
done
if [ "$(id -u)" = 0 ] && have ctr; then
    say INFO "containerd namespaces: $(ctr namespaces ls -q 2>/dev/null | tr '\n' ' ')"
    say INFO "(k8s.io belongs to Kubernetes; nerdctl uses 'default' unless told otherwise)"
fi
if [ -e /usr/local/lib/systemd/system/containerd.service ]; then
    say WARN "/usr/local/lib/systemd/system/containerd.service exists (a nerdctl full bundle?); on a Kubernetes node it can replace the node's containerd unit"
fi

echo "== Kubernetes"
if have kubectl; then
    say INFO "kubectl: $(kubectl version --client 2>/dev/null | head -n 1)"
    if kubectl get --raw /version >/dev/null 2>&1; then
        say OK "cluster reachable: $(kubectl version 2>/dev/null | grep -i '^server' | head -n 1)"
        say INFO "context: $(kubectl config current-context 2>/dev/null)"
        kubectl get nodes --no-headers -o custom-columns='N:.metadata.name,V:.status.nodeInfo.kubeletVersion,R:.status.nodeInfo.containerRuntimeVersion,IP:.status.addresses[0].address' 2>/dev/null |
            awk '{ printf "INFO   node %s, kubelet %s, runtime %s, address %s\n", $1, $2, $3, $4 }'
        ports=$(kubectl get svc -A --no-headers -o custom-columns='T:.spec.type,P:.spec.ports[*].port,NP:.spec.ports[*].nodePort,N:.metadata.namespace,S:.metadata.name' 2>/dev/null |
            awk '$1 == "LoadBalancer" || $1 == "NodePort" { printf "%s/%s ports %s nodePorts %s; ", $4, $5, $2, $3 }')
        [ -n "$ports" ] && say INFO "services on node ports (not seen by ss): $ports"
        sc=$(kubectl get storageclass --no-headers 2>/dev/null)
        if [ -n "$sc" ]; then
            echo "$sc" | awk '{ printf "INFO   storage class %s %s\n", $1, $2 }'
            echo "$sc" | grep -q '(default)' || say WARN "no default storage class: Postgres and Redis need one, or name it in the values"
        else
            say WARN "no storage class: Postgres and Redis need persistent volumes"
        fi
        ic=$(kubectl get ingressclass --no-headers 2>/dev/null | awk '{ printf "%s ", $1 }')
        [ -n "$ic" ] && say INFO "ingress classes: $ic" || say WARN "no ingress class: expose the vault another way (NodePort, LoadBalancer)"
        kubectl get crd certificates.cert-manager.io >/dev/null 2>&1 && say INFO "cert-manager: installed" || say INFO "cert-manager: not installed"
        kubectl get namespace infisical >/dev/null 2>&1 && say WARN "namespace infisical exists already" || say OK "namespace infisical: free"
        for verb in "create namespaces" "create secrets -n infisical" "create deployments -n infisical"; do
            # shellcheck disable=SC2086
            answer=$(kubectl auth can-i $verb 2>/dev/null)
            say INFO "can I $verb: ${answer:-unknown}"
        done
    else
        say INFO "kubectl cannot reach a cluster with the current configuration"
    fi
else
    say INFO "kubectl not found"
fi
if have helm; then
    say INFO "helm: $(helm version --short 2>/dev/null)"
else
    say INFO "helm not found"
fi

echo "== Network"
for var in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
    eval "value=\${$var:-}"
    [ -n "$value" ] && say INFO "$var is set (value not shown)"
done
if have curl; then
    for url in https://registry-1.docker.io/v2/ https://dl.cloudsmith.io/ https://artifacts-cli.infisical.com/ https://pypi.org/simple/ https://github.com/; do
        code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url" 2>/dev/null)
        case $code in
            000|'') say WARN "no answer from $url" ;;
            *) say OK "$url answers (HTTP $code)" ;;
        esac
    done
else
    say INFO "curl not found; network not checked"
fi
echo "== Done (nothing was changed)"
