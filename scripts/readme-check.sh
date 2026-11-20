#!/usr/bin/env bash
# Run the README's commands against a live daemon, so the documentation cannot drift from the server.
#   ./scripts/readme-check.sh <base-url> <admin-token> <slug>
set -uo pipefail
BASE="${1:?base url}"
TOKEN="${2:?admin token}"
SLUG="${3:?slug}"
JAR=$(mktemp)
pass=0; fail=0
show() { printf '  %-58s %s\n' "$1" "$2"; }
hit() { # name, expected-status, curl args...
  local name="$1" want="$2"; shift 2
  local out status
  out=$(curl -sS -o /tmp/readme_body -w '%{http_code}' "$@"); status=$?
  local code
  code=$(jq -r '.error.code // .jsonrpc // "ok"' /tmp/readme_body 2>/dev/null)
  if [ "$status" = "0" ] && [ "$out" = "$want" ]; then pass=$((pass+1)); show "$name" "$out $(printf %s "$code" | head -c 24)";
  else fail=$((fail+1)); show "$name" "WANT $want GOT ${out:-curl-error} $(head -c 120 /tmp/readme_body)"; fi
}

echo "== README examples against $BASE"
hit "POST /admin/api/login" 200 -c "$JAR" -X POST "$BASE/admin/api/login" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"
KEY_RESPONSE=$(curl -sS -b "$JAR" -X POST "$BASE/admin/api/keys" -H 'content-type: application/json' \
  -d "{\"name\":\"readme-check\",\"databases\":[\"$SLUG\"],\"allow_writes\":false}")
KEY=$(printf %s "$KEY_RESPONSE" | jq -r '.key // "null"')
KEY_ID=$(printf %s "$KEY_RESPONSE" | jq -r '.id // "null"')
if [ -n "$KEY" ] && [ "$KEY" != "null" ]; then pass=$((pass+1)); show "POST /admin/api/keys → .key" "${KEY:0:10}…";
else fail=$((fail+1)); show "POST /admin/api/keys → .key" "missing"; fi

hit "GET  /api/v1/health" 200 "$BASE/api/v1/health"
hit "GET  /api/v1/databases (key)" 200 -H "Authorization: Bearer $KEY" "$BASE/api/v1/databases"
hit "GET  …/schema (key)" 200 -H "Authorization: Bearer $KEY" "$BASE/api/v1/databases/$SLUG/schema"
hit "GET  …/indexes (key)" 200 -H "Authorization: Bearer $KEY" "$BASE/api/v1/databases/$SLUG/indexes"
hit "POST …/query (key)" 200 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/query" -d '{"query":"MATCH (d:Doc) RETURN d ORDER BY d.id LIMIT 5","limit":50}'
hit "POST …/query mode=graph" 200 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/graph" -d "{\"mode\":\"projection\",\"node_tables\":[\"Doc\",\"Tag\"],\"relationship_tables\":[\"CITES\"],\"weight_columns\":{\"CITES\":\"weight\"},\"max_nodes\":500,\"max_edges\":1500}"
hit "GET  …/explain?query=" 200 -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/databases/$SLUG/explain?query=MATCH%20(d:Doc)%20RETURN%20d"
hit "GET  …/neighbors" 200 -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/databases/$SLUG/neighbors?table=Doc&record_id=1&limit=200"
hit "POST …/search/text (boolean)" 200 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/search/text" -d '{"index":"doc_text","query":"vector AND storage","k":5,"prefix":false,"phrase":false}'
hit "POST …/search/vector" 200 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/search/vector" -d '{"space":"semantic","vector":[1,0],"table":"Doc","k":3}'
hit "POST …/search/hybrid" 200 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/search/hybrid" -d "{\"table\":\"Doc\",\"index\":\"doc_text\",\"query\":\"durable\",\"space\":\"semantic\",\"vector\":[1,0],\"k\":3}"
hit "POST …/query CREATE refused" 403 -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  "$BASE/api/v1/databases/$SLUG/query" -d '{"query":"CREATE (:Doc {id: 999, title: '"'"'x'"'"'})"}'
hit "GET  …/schema anonymous" 401 "$BASE/api/v1/databases/$SLUG/schema"
hit "GET  /api/v1/openapi.json" 200 "$BASE/api/v1/openapi.json"
hit "GET  /metrics" 200 "$BASE/metrics"
hit "GET  / (console SPA)" 200 "$BASE/"
hit "GET  /databases/deep-link (SPA fallback)" 200 "$BASE/databases/$SLUG"

hit "POST /mcp initialize" 200 -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
hit "POST /mcp tools/list" 200 -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
TOOLS=$(curl -sS -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | jq -r '[.result.tools[].name] | join(",")')
case "$TOOLS" in *write*) fail=$((fail+1)); show "read-only key does not see write" "$TOOLS";;
  *) pass=$((pass+1)); show "read-only key does not see write" "$TOOLS";; esac
hit "POST /mcp tools/call query" 200 -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"query\",\"arguments\":{\"db\":\"$SLUG\",\"cypher\":\"MATCH (d:Doc) RETURN d.id ORDER BY d.id LIMIT 5\"}}}"
hit "POST /mcp write refused (no allow_writes)" 200 -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"write\",\"arguments\":{\"db\":\"$SLUG\",\"cypher\":\"CREATE (:Doc {id: 998})\",\"confirm_write\":true}}}"
IS_ERROR=$(curl -sS -X POST "$BASE/mcp" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"write\",\"arguments\":{\"db\":\"$SLUG\",\"cypher\":\"CREATE (:Doc {id: 998})\",\"confirm_write\":true}}}" \
  | jq -r '.result.isError // .error.code // "unknown"')
if [ "$IS_ERROR" = "true" ]; then pass=$((pass+1)); show "mcp write answers isError=true" "$IS_ERROR";
else fail=$((fail+1)); show "mcp write answers isError=true" "$IS_ERROR"; fi

# The check mints a key to exercise the data plane; it does not leave one behind.
if [ "$KEY_ID" != "null" ] && [ -n "$KEY_ID" ]; then
  curl -sS -b "$JAR" -X POST "$BASE/admin/api/keys/$KEY_ID/revoke" >/dev/null
  curl -sS -b "$JAR" -X DELETE "$BASE/admin/api/keys/$KEY_ID" >/dev/null
  show "the key this check minted is revoked and removed" "id $KEY_ID"
fi

echo
echo "$pass README examples worked, $fail did not"
[ "$fail" = "0" ]
