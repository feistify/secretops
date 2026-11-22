#!/bin/sh
# Read-only check of a Linux host before installing the self-hosted Infisical
# on it, with nerdctl compose or with Helm on a Kubernetes node. It changes
# nothing, prints no secret and reads no Kubernetes Secret. Run it as the
# human; with sudo it can also look at containerd's namespaces:
#
#   sh check-node.sh            # or: sudo sh check-node.sh
#   sh check-node.sh https://VAULT:30843   # also: does the vault answer, direct or through the proxy
#
# A proxy is shown as scheme://host:port only, never with a user or password.
# It checks each download site directly and through the proxy, and whether the
# proxy intercepts TLS. Paste the output to whoever helps you; it holds host
# names, addresses, versions and certificate issuers, nothing else.
set -u
export LC_ALL=C

VAULT_URL=${1:-}
case $VAULT_URL in
    *@*|*[[:space:]]*) echo "check-node: give the vault URL without a user or password" >&2; exit 2 ;;
    ''|https://*) ;;
    *) echo "usage: sh check-node.sh [https://VAULT:PORT]" >&2; exit 2 ;;
esac
if [ $# -gt 1 ]; then
    echo "usage: sh check-node.sh [https://VAULT:PORT]" >&2
    exit 2
fi

say() { printf '%-6s %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
first_line() { "$@" 2>/dev/null | head -n 1; }

echo "== Host"
if [ -r /etc/os-release ]; then
    say INFO "OS: $(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')"
fi
say INFO "kernel $(uname -r), $(uname -m), $(getconf _NPROCESSORS_ONLN 2>/dev/null || echo '?') CPUs"
awk '/^MemTotal:/ { printf "INFO   memory: %.1f GiB\n", $2 / 1048576 }' /proc/meminfo
for dir in / /var/lib /opt; do
    [ -d "$dir" ] && df -Ph "$dir" | awk -v d="$dir" 'NR == 2 { printf "INFO   free on %s: %s (%s)\n", d, $4, $6 }'
done
say INFO "host name: $(hostname -f 2>/dev/null || hostname)"
if have ip; then
    say INFO "IPv4: $(ip -4 -o addr show scope global 2>/dev/null | awk '{ sub("/.*", "", $4); printf "%s ", $4 }')"
fi
if [ "$(id -u)" = 0 ]; then
    say INFO "running as root: kubectl rights and proxy settings below are root's, not yours; run without sudo for those"
else
    say INFO "running as $(id -un) (sudo shows containerd namespaces too)"
fi

echo "== Accounts with the vault containers' uids (61003: Kubernetes variant only)"
for uid in 61001 61002 61003 70; do
    if have getent; then
        name=$(getent passwd "$uid" | cut -d: -f1)
    else
        name=$(awk -F: -v u="$uid" '$3 == u { print $1 }' /etc/passwd)
    fi
    if [ -n "$name" ]; then
        say WARN "uid $uid belongs to host account '$name': no host account may share a uid with the vault's processes"
    else
        say OK "uid $uid: no host account"
    fi
done

echo "== Ports"
listening=""
if have ss && listening=$(ss -ltn 2>/dev/null); then
    :
elif have netstat && listening=$(netstat -ltn 2>/dev/null); then
    :
else
    listening=""
    say INFO "neither ss nor netstat worked; ports not checked"
fi
if [ -n "$listening" ]; then
    for port in 80 443 8443 30843; do
        if echo "$listening" | awk -v p="$port" '{ n = split($4, a, ":"); if (a[n] == p) found = 1 } END { exit !found }'; then
            say WARN "port $port is in use"
        else
            say OK "port $port is free"
        fi
    done
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
    for sock in /run/containerd/containerd.sock /run/k3s/containerd/containerd.sock /var/snap/microk8s/common/run/containerd.sock; do
        [ -S "$sock" ] || continue
        namespaces=$(ctr -a "$sock" namespaces ls -q 2>/dev/null | tr '\n' ' ')
        say INFO "containerd namespaces at $sock: ${namespaces:-cannot query}"
    done
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
        # Tell "none" from "not allowed to look".
        # 0 with output: found; 1: not found or another error; 2: forbidden.
        look() {
            out=$(kubectl "$@" 2>/dev/null) && { printf '%s' "$out"; return 0; }
            out=$(kubectl "$@" 2>&1)
            case $out in *Forbidden*|*forbidden*) return 2 ;; esac
            return 1
        }
        if nodes=$(look get nodes --no-headers -o custom-columns='N:.metadata.name,V:.status.nodeInfo.kubeletVersion,R:.status.nodeInfo.containerRuntimeVersion,IP:.status.addresses[0].address,K:.spec.taints[*].key,E:.spec.taints[*].effect'); then
            echo "$nodes" |
                awk '{ printf "INFO   node %s, kubelet %s, runtime %s, address %s\n", $1, $2, $3, $4 }'
            # Taints: the vault's pods have no tolerations, so NoSchedule or NoExecute keeps them off a node.
            echo "$nodes" | awk '{ $2 = $5; $3 = $6 } $2 == "<none>" { printf "OK     node %s: no taints\n", $1; next }
                 { hard = 0; n = split($3, e, ","); for (i = 1; i <= n; i++) if (e[i] == "NoSchedule" || e[i] == "NoExecute") hard = 1 }
                 hard { printf "WARN   node %s taints %s (%s): the vault pods have no tolerations and will not run there\n", $1, $2, $3; next }
                 { printf "INFO   node %s taints %s (%s): the scheduler prefers other nodes, the pods can still run here\n", $1, $2, $3 }'
        else
            say INFO "nodes: not allowed to list (versions and taints unknown)"
        fi
        ports=$(kubectl get svc -A --no-headers -o custom-columns='T:.spec.type,P:.spec.ports[*].port,NP:.spec.ports[*].nodePort,N:.metadata.namespace,S:.metadata.name' 2>/dev/null |
            awk '$1 == "LoadBalancer" || $1 == "NodePort" { printf "%s/%s ports %s nodePorts %s; ", $4, $5, $2, $3 }')
        [ -n "$ports" ] && say INFO "services on node ports (not seen by ss): $ports"
        if sc=$(look get storageclass --no-headers); then
            if [ -n "$sc" ] && ! echo "$sc" | grep -q '^No resources'; then
                # With "(default)" the provisioner is the third column.
                echo "$sc" | awk '{ d = ($2 == "(default)"); p = d ? $3 : $2
                    printf "INFO   storage class %s%s, provisioner %s\n", $1, d ? " (default)" : "", p
                    if (p ~ /local-path|no-provisioner|openebs\.io\/local|local\.csi\.openebs|zfs\.csi\.openebs|topolvm|hostpath/)
                        printf "INFO   storage class %s is node-local: a volume stays on the node that created it; if that node is lost, restore from a backup\n", $1 }'
                echo "$sc" | grep -q '(default)' || say WARN "no default storage class: Postgres and Redis need one, or name it in the manifests"
            else
                say WARN "no storage class: Postgres and Redis need persistent volumes"
            fi
        else
            say INFO "storage classes: not allowed to list"
        fi
        if ic=$(look get ingressclass --no-headers); then
            ic=$(echo "$ic" | grep -v '^No resources' | awk 'NF { printf "%s ", $1 }')
            say INFO "ingress classes: ${ic:-none}"
        else
            say INFO "ingress classes: not allowed to list"
        fi
        if out=$(look get crd certificates.cert-manager.io); then
            say INFO "cert-manager: installed"
        elif [ $? = 2 ]; then
            say INFO "cert-manager: not allowed to check"
        else
            say INFO "cert-manager: not installed"
        fi
        if out=$(look get namespace infisical); then
            say WARN "namespace infisical exists already"
        elif [ $? = 2 ]; then
            say INFO "namespace infisical: not allowed to check"
        else
            say OK "namespace infisical: free"
        fi
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
# A proxy URL can hold a user and password: only scheme://host:port is printed.
# Only a recognised scheme and host:port are printed; anything else on the line
# (a password with a space, another variable, a query) is never shown.
describe() {
    # First line, without leading quotes; cut at the first space, quote or ';'.
    line=$(printf '%s\n' "$1" | head -n 1 | sed -E "s#^[[:space:]\"']*##")
    value=$(printf '%s\n' "$line" | sed -E "s#[[:space:]\"';].*##")
    # A cut before the @ would leave a user and part of a password.
    case $line in
        *@*) case $value in *@*) ;; *) echo "set (not shown: not a plain proxy URL)"; return ;; esac ;;
    esac
    scheme=$(printf '%s\n' "$value" | sed -nE 's#^((https?|socks4a?|socks5h?)://).*#\1#ip')
    rest=${value#"$scheme"}
    creds=""
    case $rest in
        *@*) rest=${rest##*@}; creds=" (holds a user and password, not shown)" ;;
    esac
    host=$(printf '%s\n' "$rest" | sed -nE 's#^([A-Za-z0-9.-]+(:[0-9]+)?|\[[0-9A-Fa-f:.]+\](:[0-9]+)?)([/?#].*)?$#\1#p')
    if [ -n "$host" ]; then
        printf '%s%s%s\n' "$scheme" "$host" "$creds"
    else
        echo "set (not shown: not a plain proxy URL)"
    fi
}
# The value of the first https_proxy line read on stdin (also systemd's quoted
# "HTTPS_PROXY=..." and k3s's CONTAINERD_HTTPS_PROXY).
pick() {
    grep -iE "^[[:space:]\"']*(export[[:space:]]+)?(CONTAINERD_)?https_proxy=" | head -n 1 |
        sed -E "s/^[[:space:]\"']*(export[[:space:]]+)?[A-Za-z_]+=//"
}
proxy=${https_proxy:-${HTTPS_PROXY:-${all_proxy:-${ALL_PROXY:-}}}}
found=""
for var in https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY; do
    eval "value=\${$var:-}"
    [ -n "$value" ] || continue
    found=yes
    say INFO "$var: $(describe "$value")"
done
for var in no_proxy NO_PROXY; do
    eval "value=\${$var:-}"
    [ -n "$value" ] && say INFO "$var: $value"
done
[ -n "$found" ] || say INFO "no proxy variables in this shell"
if [ -z "$proxy" ] && [ -n "$found" ]; then
    say INFO "only an http proxy is set; curl, gh and the Infisical CLI use https_proxy or HTTPS_PROXY for https"
fi
if [ -r /etc/environment ]; then
    value=$(pick < /etc/environment)
    [ -n "$value" ] && say INFO "/etc/environment: HTTPS proxy $(describe "$value")"
fi
if have apt-config; then
    # apt uses its http proxy for https too unless an https one is set.
    for key in https http; do
        value=$(apt-config dump 2>/dev/null | grep -i "^Acquire::$key::Proxy \"" | head -n 1 | sed 's/^[^"]*"//; s/";*$//')
        [ -n "$value" ] && { say INFO "apt: $key proxy $(describe "$value")"; break; }
    done
fi
# Images are pulled by the container runtime, not by this shell: it needs its own proxy settings.
if have systemctl; then
    for unit in containerd docker k3s k3s-agent rke2-server rke2-agent; do
        [ "$(systemctl is-active "$unit" 2>/dev/null)" = active ] || continue
        # systemd quotes an entry with spaces: keep a quoted entry together.
        value=$(systemctl show -p Environment --value "$unit" 2>/dev/null | grep -oE '"[^"]*"|[^ "]+' | pick)
        unreadable=""
        for file in "/etc/systemd/system/$unit.service.env" "/etc/default/$unit" "/etc/sysconfig/$unit"; do
            [ -z "$value" ] && [ -e "$file" ] || continue
            if [ -r "$file" ]; then
                value=$(pick < "$file")
            else
                unreadable=$file
            fi
        done
        if [ -n "$value" ]; then
            say INFO "service $unit: HTTPS proxy $(describe "$value")"
        elif [ -n "$unreadable" ]; then
            say INFO "service $unit: proxy unknown, $unreadable needs sudo to read"
        else
            say INFO "service $unit: no HTTPS proxy (its downloads and image pulls go direct)"
        fi
    done
fi
[ "$(id -u)" = 0 ] && say INFO "sudo drops proxy variables; if you use a proxy, run without sudo for the checks below"

# probe URL [curl options]: prints "HTTP_CODE CURL_STATUS".
probe() {
    url=$1
    shift
    code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$@" "$url" 2>/dev/null)
    printf '%s %s' "${code:-000}" "$?"
}
# Any HTTP code means the site answered, even if the connection then broke.
words() {
    [ "$1" != 000 ] && { echo "HTTP $1"; return; }
    case $2 in
        6) echo "name not resolved" ;;
        5|7) echo "no connection" ;;
        28) echo "timeout" ;;
        35) echo "TLS handshake failed" ;;
        56) echo "refused or cut off" ;;
        60) echo "certificate not trusted" ;;
        *) echo "failed (curl status $2)" ;;
    esac
}
# reach LABEL URL [curl options]: direct, and with this shell's proxy settings when a proxy is set.
# Sets direct_ok and proxy_ok to yes or no.
reach() {
    label=$1
    url=$2
    shift 2
    # A blocked direct route often drops packets: give up on it sooner than on a slow proxy.
    set -- $(probe "$url" --noproxy '*' --connect-timeout 5 "$@") "$@"
    direct_ok=no
    [ "$1" != 000 ] && direct_ok=yes
    line="direct: $(words "$1" "$2")"
    shift 2
    proxy_ok=no
    if [ -n "$proxy" ]; then
        set -- $(probe "$url" "$@")
        case $1 in
            000) text=$(words "$1" "$2") ;;
            # These can come from the proxy itself (blocked, login, no route), not from the site.
            403|407|502|503|504) text="HTTP $1 (may be the proxy's answer, not the site's)" ;;
            *) text="HTTP $1"; proxy_ok=yes ;;
        esac
        line="$line; with the proxy settings: $text"
    fi
    if [ $direct_ok = yes ] || [ $proxy_ok = yes ]; then
        say OK "$label $line"
    else
        say WARN "$label $line"
    fi
}
if have curl; then
    say INFO "an HTTP code means the site answers (through a proxy, 403, 407, 502-504 can be the proxy's own); dl.cloudsmith.io serves Infisical's Helm chart"
    for url in https://registry-1.docker.io/v2/ https://dl.cloudsmith.io/ https://artifacts-cli.infisical.com/infisical.gpg https://pypi.org/simple/secretops/ https://github.com/ https://api.github.com/; do
        reach "$url" "$url"
    done
    # A proxy that intercepts TLS shows its own issuer instead of the site's.
    # -k only to read the certificate; nothing is sent but a plain GET.
    issuer() {
        curl -sv -k -o /dev/null -m 15 "$@" https://api.github.com/ 2>&1 | sed -n 's/^\*[[:space:]]*issuer: *//p' | tail -n 1
    }
    direct_issuer=$(issuer --noproxy '*' --connect-timeout 5)
    [ -n "$direct_issuer" ] && say INFO "api.github.com certificate issuer, direct: $direct_issuer"
    if [ -n "$proxy" ]; then
        proxy_issuer=$(issuer)
        if [ -z "$proxy_issuer" ]; then
            say INFO "api.github.com certificate issuer, with the proxy settings: unknown (no answer)"
        else
            say INFO "api.github.com certificate issuer, with the proxy settings: $proxy_issuer"
            if [ -n "$direct_issuer" ] && [ "$direct_issuer" != "$proxy_issuer" ]; then
                say WARN "the proxy intercepts TLS (another issuer than direct): gh, pip, curl and the Infisical CLI need the company's CA"
            elif echo "$proxy_issuer" | grep -qiE "Sectigo|DigiCert|USERTrust|COMODO|Let's Encrypt|ISRG|GlobalSign|Google Trust|GTS|Amazon|Microsoft|Entrust|GeoTrust|Baltimore|GoDaddy|Starfield"; then
                say OK "the proxy does not intercept TLS (a public issuer)"
            else
                say WARN "the issuer is not a well-known public CA: the proxy probably intercepts TLS; gh, pip, curl and the Infisical CLI need the company's CA"
            fi
        fi
    fi
    if [ -n "$VAULT_URL" ]; then
        # -k: this checks the route only; the broker checks the vault's CA itself.
        reach "vault $VAULT_URL" "${VAULT_URL%/}/api/status" -k
        if [ $direct_ok = yes ] && [ -n "$proxy" ] && [ $proxy_ok = no ]; then
            say WARN "the vault answers directly but not with the proxy settings: put its host in NO_PROXY"
        fi
    fi
else
    say INFO "curl not found; network not checked"
fi
echo "== Done (nothing was changed)"
