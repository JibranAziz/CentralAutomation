#!/usr/bin/env bash
# Generate a self-signed TLS certificate for Aruba Central Automation.
#
# Works for any hostname and/or IP address - the cert is added as a SAN so
# browsers match it (after you accept the "not trusted" warning once).
#
#   sudo ./deploy/gen-cert.sh central.example.lan
#   sudo ./deploy/gen-cert.sh 192.168.1.50
#   sudo ./deploy/gen-cert.sh central.example.lan 192.168.1.50 10.0.0.50
#
# Output: /etc/ssl/aruba-central-automation/{fullchain.pem,privkey.pem}
set -euo pipefail

CERT_DIR="${CERT_DIR:-/etc/ssl/aruba-central-automation}"
DAYS="${DAYS:-3650}"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <hostname-or-ip> [additional-hostname-or-ip ...]" >&2
    exit 1
fi

primary="$1"
alt_names=""
i=0
for name in "$@"; do
    if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        i=$((i + 1)); alt_names+="IP.${i} = ${name}"$'\n'
    else
        i=$((i + 1)); alt_names+="DNS.${i} = ${name}"$'\n'
    fi
done

mkdir -p "$CERT_DIR"
conf="$(mktemp)"
cat > "$conf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = ${primary}
[v3]
subjectAltName = @san
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
[san]
${alt_names}
EOF

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/fullchain.pem" \
    -days "$DAYS" -config "$conf"

rm -f "$conf"
chmod 600 "$CERT_DIR/privkey.pem"
chmod 644 "$CERT_DIR/fullchain.pem"

echo
echo "Self-signed cert written to $CERT_DIR (valid ${DAYS} days):"
openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -dates -ext subjectAltName
