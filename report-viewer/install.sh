#!/usr/bin/env bash
# Deploy the readiness report viewer into the readiness-viewer namespace.
# Safe to re-run: it updates the deployment and leaves existing Secrets alone.
#
#   AAP_URL=https://aap.apps.example.com report-viewer/install.sh
#   AAP_URL=https://aap.apps.example.com report-viewer/install.sh --render   # print, apply nothing
#
# Optional:
#   AAP_TOKEN_FILE       file holding the read-only AAP token; otherwise you are
#                        prompted, without echo. Used only when the
#                        report-viewer-aap-token Secret does not exist yet.
#   AAP_CA_FILE          CA bundle that signed AAP's route, if not publicly trusted
#   VIEWER_HOST          Route hostname; default report-viewer-readiness-viewer.<apps domain>
#   VIEWER_IMAGE         default: the image pushed to this cluster's internal registry
#   OAUTH_PROXY_IMAGE    default: the cluster's own, from the openshift/oauth-proxy image stream
#   REPORT_ORGANIZATION  only serve report jobs from this AAP organization
#
# Needs oc, logged in as a user who can create a namespace.

set -euo pipefail

NS=readiness-viewer
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER=false
[[ "${1:-}" == "--render" ]] && RENDER=true
die() { echo "error: $*" >&2; exit 1; }

[[ -n "${AAP_URL:-}" ]] || die "set AAP_URL to the AAP platform URL, e.g. AAP_URL=https://aap.apps.example.com"
[[ "${AAP_URL}" =~ ^https://[^/[:space:]]+/?$ ]] || die "AAP_URL must be https://<host>, with no path"
[[ -z "${VIEWER_HOST:-}" || "${VIEWER_HOST}" =~ ^[a-z0-9.-]+$ ]] || die "VIEWER_HOST must be a bare hostname"

VIEWER_IMAGE="${VIEWER_IMAGE:-image-registry.openshift-image-registry.svc:5000/${NS}/report-viewer:1.0}"
if [[ -z "${OAUTH_PROXY_IMAGE:-}" ]]; then
  if ${RENDER}; then
    OAUTH_PROXY_IMAGE="<resolved from openshift/oauth-proxy:v4.4 at install>"
  else
    oc whoami >/dev/null 2>&1 || die "oc is not logged in to a cluster"
    OAUTH_PROXY_IMAGE="$(oc get istag oauth-proxy:v4.4 -n openshift -o jsonpath='{.image.dockerImageReference}' 2>/dev/null || true)"
    [[ -n "${OAUTH_PROXY_IMAGE}" ]] || die "could not read the openshift/oauth-proxy:v4.4 image stream tag; set OAUTH_PROXY_IMAGE"
  fi
fi

# A kustomize images: entry for name -> ref, where ref is repo@digest, repo:tag or repo.
image() {
  local name="$1" ref="$2"
  echo "  - name: ${name}"
  if [[ "${ref}" == *@* ]]; then
    printf '    newName: %s\n    digest: %s\n' "${ref%@*}" "${ref#*@}"
  elif [[ "${ref##*/}" == *:* ]]; then
    printf '    newName: %s\n    newTag: "%s"\n' "${ref%:*}" "${ref##*:}"
  else
    printf '    newName: %s\n' "${ref}"
  fi
}

# kustomize accepts only relative bases, so the overlay gets its own copy.
overlay="$(mktemp -d)"
trap 'rm -rf "${overlay}"' EXIT
cp -R "${HERE}/openshift" "${overlay}/base"
{
  cat <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - base
images:
EOF
  image report-viewer "${VIEWER_IMAGE}"
  image oauth-proxy "${OAUTH_PROXY_IMAGE}"
  cat <<EOF
configMapGenerator:
  - name: report-viewer
    behavior: merge
    literals:
      - AAP_URL=${AAP_URL%/}
      - REPORT_ORGANIZATION=${REPORT_ORGANIZATION:-}
EOF
  if [[ -n "${VIEWER_HOST:-}" ]]; then
    cat <<EOF
patches:
  - target: {kind: Route, name: report-viewer}
    patch: '[{"op": "add", "path": "/spec/host", "value": "${VIEWER_HOST}"}]'
EOF
  fi
} > "${overlay}/kustomization.yaml"

if ${RENDER}; then
  if command -v oc >/dev/null; then oc kustomize "${overlay}"; else kubectl kustomize "${overlay}"; fi
  exit 0
fi

echo "== namespace"
oc apply -f "${HERE}/openshift/namespace.yaml"

echo "== secrets"
if oc get secret report-viewer-aap-token -n "${NS}" >/dev/null 2>&1; then
  echo "report-viewer-aap-token exists; leaving it (delete it to replace the token)"
else
  if [[ -n "${AAP_TOKEN_FILE:-}" ]]; then
    token="$(<"${AAP_TOKEN_FILE}")"
  else
    read -rsp "Read-only AAP token for the viewer: " token; echo
  fi
  [[ -n "${token}" ]] || die "empty token"
  # Through stdin, so the token never appears in a process listing.
  printf '%s' "${token}" | oc create secret generic report-viewer-aap-token -n "${NS}" --from-file=token=/dev/stdin
  unset token
fi
if ! oc get secret report-viewer-proxy -n "${NS}" >/dev/null 2>&1; then
  openssl rand -hex 16 | tr -d '\n' \
    | oc create secret generic report-viewer-proxy -n "${NS}" --from-file=session_secret=/dev/stdin
fi
if [[ -n "${AAP_CA_FILE:-}" ]]; then
  oc create configmap report-viewer-aap-ca -n "${NS}" --from-file=ca.crt="${AAP_CA_FILE}" \
    --dry-run=client -o yaml | oc apply -f -
fi

echo "== viewer"
oc apply -k "${overlay}"
oc rollout status deployment/report-viewer -n "${NS}" --timeout=180s

host="$(oc get route report-viewer -n "${NS}" -o jsonpath='{.spec.host}')"
cat <<EOF

Viewer: https://${host}/

Next (docs/DEMO-SETUP.md §6):
  1. Give people access:   oc adm groups new readiness-viewers <user>...
  2. Tell AAP the address: readiness_viewer_url=https://${host}
EOF
