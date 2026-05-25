#!/bin/bash
set -euo pipefail

# Directorio de libs bakeadas en el repo (siempre presentes, sin red)
REPO_LIBS_DIR="$(cd "$(dirname "$0")" && pwd)/libs"
PW_LIBS_DIR="/tmp/pw_libs"
SYS_RPATH="/usr/lib/x86_64-linux-gnu:/usr/lib:/lib/x86_64-linux-gnu:/lib:${PW_LIBS_DIR}"

rm -rf "$PW_LIBS_DIR" && mkdir -p "$PW_LIBS_DIR"

# ── Health-check server en puerto 8081 (INMEDIATO) ────────────────────────────
echo "=== Levantando health-check en :5000 y :8081 ==="
python3 - <<'PYEOF' &
import http.server, threading, time, sys
class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
started = []
for _port in (5000, 8081):
    try:
        _srv = http.server.HTTPServer(("0.0.0.0", _port), _H)
        threading.Thread(target=_srv.serve_forever, daemon=True).start()
        started.append(_port)
    except Exception as e:
        print(f"health-check :{_port} error: {e}", file=sys.stderr)
if started:
    time.sleep(600)
PYEOF
HEALTH_PID=$!
echo "=== Health-check PID: $HEALTH_PID ==="
trap "kill $HEALTH_PID 2>/dev/null || true" EXIT

# ── Copiar libs bakeadas a /tmp/pw_libs (siempre disponibles, sin red) ────────
echo "=== Copiando libs del repo → $PW_LIBS_DIR ==="
for _lib in "$REPO_LIBS_DIR"/*.so* "$REPO_LIBS_DIR"/*.so; do
    [ -f "$_lib" ] || continue
    _name="$(basename "$_lib")"
    cp -f "$_lib" "$PW_LIBS_DIR/$_name"
    echo "  → $_name ($(stat -c%s "$PW_LIBS_DIR/$_name") bytes)"
done
echo "=== Libs copiadas: $(ls "$PW_LIBS_DIR" 2>/dev/null | tr '\n' ' ') ==="

# ── Descargar libs faltantes desde Ubuntu Jammy (fallback si no están en repo) ──
# Estas libs son transitive deps de libglib/libgobject/libgio (Ubuntu 22.04).
# Si el repo las tiene no se descargan (curl sólo si el archivo no existe en /tmp/pw_libs).
declare -A _FALLBACK_LIBS=(
  ["libpcre.so.3"]="http://security.ubuntu.com/ubuntu/pool/main/p/pcre3/libpcre3_8.39-13ubuntu0.22.04.1_amd64.deb"
  ["libffi.so.8"]="http://archive.ubuntu.com/ubuntu/pool/main/libf/libffi/libffi8_3.4.2-4_amd64.deb"
  ["libselinux.so.1"]="http://archive.ubuntu.com/ubuntu/pool/main/libs/libselinux/libselinux1_3.3-1build2_amd64.deb"
  ["libpcre2-8.so.0"]="http://archive.ubuntu.com/ubuntu/pool/main/p/pcre2/libpcre2-8-0_10.39-3ubuntu0.1_amd64.deb"
  ["libXrender.so.1"]="http://archive.ubuntu.com/ubuntu/pool/main/libx/libxrender/libxrender1_0.9.10-1build4_amd64.deb"
  ["libbsd.so.0"]="http://archive.ubuntu.com/ubuntu/pool/main/libb/libbsd/libbsd0_0.11.5-1_amd64.deb"
  ["libwayland-server.so.0"]="http://archive.ubuntu.com/ubuntu/pool/main/w/wayland/libwayland-server0_1.20.0-1ubuntu0.1_amd64.deb"
  ["libXi.so.6"]="http://archive.ubuntu.com/ubuntu/pool/main/libx/libxi/libxi6_1.8-1build1_amd64.deb"
)
_NEED_DOWNLOAD=0
for _soname in "${!_FALLBACK_LIBS[@]}"; do
  [ -f "$PW_LIBS_DIR/$_soname" ] || { _NEED_DOWNLOAD=1; break; }
done
if [ "$_NEED_DOWNLOAD" -eq 1 ]; then
  echo "=== Descargando libs faltantes desde Ubuntu Jammy ==="
  _TMP_DEB="/tmp/_pw_deb_dl"
  mkdir -p "$_TMP_DEB"
  for _soname in "${!_FALLBACK_LIBS[@]}"; do
    if [ ! -f "$PW_LIBS_DIR/$_soname" ]; then
      _url="${_FALLBACK_LIBS[$_soname]}"
      _deb="$_TMP_DEB/$(basename "$_url")"
      echo "  DL $_soname ..."
      curl -sL --retry 3 --max-time 30 -o "$_deb" "$_url" || { echo "  WARN: fallo descarga $_soname"; continue; }
      _extract="$_TMP_DEB/ext_${_soname}"
      mkdir -p "$_extract"
      dpkg-deb -x "$_deb" "$_extract" 2>/dev/null || true
      _found=$(find "$_extract" -name "${_soname}*" -type f 2>/dev/null | head -1)
      if [ -n "$_found" ]; then
        cp "$_found" "$PW_LIBS_DIR/$_soname"
        echo "  → $_soname OK ($(stat -c%s "$PW_LIBS_DIR/$_soname") bytes)"
      else
        echo "  WARN: no encontré $_soname en el deb"
      fi
    fi
  done
  rm -rf "$_TMP_DEB"
else
  echo "=== Libs extra ya presentes en $PW_LIBS_DIR ==="
fi

# ── Verificar/instalar Playwright Chromium ────────────────────────────────────
# Usamos `find` directo en lugar de sync_playwright (que puede colgar indefinidamente)
echo "=== Verificando Playwright Chromium ==="
# Rutas conocidas donde puede estar el caché de Playwright (workspace primero)
_PW_SEARCH_DIRS=(
    "${HOME}/workspace/.cache/ms-playwright"
    "${HOME}/.cache/ms-playwright"
)
# También incluir PLAYWRIGHT_BROWSERS_PATH si está definido
if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    _PW_SEARCH_DIRS=("${PLAYWRIGHT_BROWSERS_PATH}" "${_PW_SEARCH_DIRS[@]}")
fi

# Buscar binarios sin usar | para evitar SIGPIPE con set -o pipefail
CHROME_BINS=""
for _pb in "${_PW_SEARCH_DIRS[@]}"; do
    [ -d "$_pb" ] || continue
    _found=$(find "$_pb" -maxdepth 6 \( -name "chrome" -o -name "chrome-headless-shell" \) -type f 2>/dev/null || true)
    if [ -n "$_found" ]; then
        CHROME_BINS="$_found"
        PW_CACHE_DIR="$_pb"
        break
    fi
done

if [ -z "$CHROME_BINS" ]; then
    echo "=== Instalando Playwright Chromium ==="
    playwright install chromium 2>&1 || true
    # Reintentar búsqueda post-install
    for _pb in "${_PW_SEARCH_DIRS[@]}"; do
        [ -d "$_pb" ] || continue
        _found=$(find "$_pb" -maxdepth 6 \( -name "chrome" -o -name "chrome-headless-shell" \) -type f 2>/dev/null || true)
        if [ -n "$_found" ]; then
            CHROME_BINS="$_found"
            PW_CACHE_DIR="$_pb"
            break
        fi
    done
else
    echo "=== Playwright Chromium ya en caché ==="
fi

PW_CACHE_DIR="${PW_CACHE_DIR:-${HOME}/.cache/ms-playwright}"
echo "=== PW_CACHE_DIR: ${PW_CACHE_DIR} ==="
if [ -z "$CHROME_BINS" ]; then
    echo "=== WARN: No se encontraron binarios chrome ==="
else
    echo "=== Binarios chrome: ==="
    echo "$CHROME_BINS"
fi

# ── Aplicar patchelf --force-rpath ───────────────────────────────────────────
# --force-rpath escribe DT_RPATH (mayor prioridad que DT_RUNPATH).
# Esto hace que el linker busque en sistema+/tmp/pw_libs ANTES que en /nix/store.
# Combinado con LD_PRELOAD de automation.py, libatk se carga desde /tmp/pw_libs.
echo "=== Buscando patchelf ==="
PATCHELF_BIN=$(command -v patchelf 2>/dev/null || true)
if [ -z "${PATCHELF_BIN:-}" ]; then
    PATCHELF_BIN=$(find /nix/store -maxdepth 3 -name "patchelf" -type f 2>/dev/null | head -1 || true)
fi

if [ -n "${PATCHELF_BIN:-}" ] && [ -n "$CHROME_BINS" ]; then
    echo "=== patchelf: $PATCHELF_BIN (paralelo) ==="
    _patchelf_pids=()
    _patchelf_bins=()
    while IFS= read -r _bin; do
        [ -z "$_bin" ] && continue
        echo "=== patchelf --force-rpath → $(basename "$_bin") (background) ==="
        "$PATCHELF_BIN" --force-rpath --set-rpath "$SYS_RPATH" "$_bin" 2>/dev/null &
        _patchelf_pids+=($!)
        _patchelf_bins+=("$_bin")
    done <<< "$CHROME_BINS"
    # Esperar a que todos los patchelf terminen
    for _i in "${!_patchelf_pids[@]}"; do
        wait "${_patchelf_pids[$_i]}" 2>/dev/null || true
        _rpath=$("$PATCHELF_BIN" --print-rpath "${_patchelf_bins[$_i]}" 2>/dev/null || echo "?")
        echo "=== RPATH $(basename "${_patchelf_bins[$_i]}"): $_rpath ==="
    done
else
    echo "=== WARN: patchelf no disponible o sin binarios ==="
fi

echo "=== Setup completo — libs: $(ls "$PW_LIBS_DIR" 2>/dev/null | tr '\n' ' ') ==="

# ── Arrancar bot en background, luego matar health-check ─────────────────────
# Flask tiene retry integrado: si el puerto 5000 sigue ocupado por el health-check
# cuando Python arranca, reintenta cada 0.5s hasta tomarlo.
echo "=== Iniciando bot ==="
(
    _CRASHES=0
    while true; do
        _START_TS=$(date +%s)
        python -u main.py; _exit_code=$?
        _UPTIME=$(( $(date +%s) - _START_TS ))

        # Si corrió más de 5 minutos → resetear backoff (fue estable)
        if [ $_UPTIME -ge 300 ]; then
            _CRASHES=0
        fi

        _CRASHES=$((_CRASHES + 1))
        # Backoff: 3s, 6s, 9s... máx 15s
        _wait=$((_CRASHES * 3))
        [ $_wait -gt 15 ] && _wait=15

        if [ $_exit_code -eq 0 ]; then
            echo "=== Bot salió limpiamente (uptime ${_UPTIME}s) — reconectando en ${_wait}s... ==="
        elif [ $_exit_code -eq 42 ]; then
            echo "=== Discord 503 (uptime ${_UPTIME}s) — reintento #${_CRASHES} en ${_wait}s... ==="
        else
            echo "=== Bot crasheó exit=${_exit_code} (uptime ${_UPTIME}s) — reintento #${_CRASHES} en ${_wait}s... ==="
        fi
        sleep $_wait
    done
) &
BOT_PID=$!

# Dar 1 segundo para que Python arranque y Flask empiece a intentar el bind
sleep 1

# Matar health-check: Flask tomará el puerto 5000 en el próximo retry (~0.5s)
echo "=== Parando health-check (PID=$HEALTH_PID) — Flask tomará :5000 en <1s ==="
kill $HEALTH_PID 2>/dev/null || true
trap "kill $BOT_PID 2>/dev/null || true" EXIT

# Esperar al loop del bot (proceso principal)
wait $BOT_PID
