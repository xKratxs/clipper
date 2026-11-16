# Clipper

Pega un link de YouTube y obtén los mejores momentos como clips con subtítulos estilo TikTok
(resaltando palabra por palabra), listos para descargar.

1. Descarga el video (yt-dlp)
2. Lo transcribe con marcas de tiempo por palabra (faster-whisper)
3. Elige los mejores momentos (con Claude si hay `ANTHROPIC_API_KEY`; si no, con una heurística)
4. Renderiza cada clip (vertical 9:16 u horizontal) con subtítulos y un `.srt`

## Versión online gratis (GitHub)

Todo se procesa en GitHub Actions y los clips se publican en *Releases* de este repositorio.
La galería (`docs/index.html`, servida con GitHub Pages) muestra los clips a medida que se terminan.
Funciona con la PC apagada y se puede abrir desde cualquier dispositivo.

### Configuración (una sola vez)

1. **Settings → Pages**: *Source* = **Deploy from a branch**, *Branch* = **main** y carpeta **/docs** → Save.
   La galería queda en `https://TU-USUARIO.github.io/NOMBRE-DEL-REPO/`.
2. **Settings → Secrets and variables → Actions → New repository secret** (opcionales):
   - `YT_COOKIES`: el contenido de tu `cookies.txt` de YouTube (necesario si YouTube bloquea con
     *"Sign in to confirm you're not a bot"*). Expórtalo con la extensión **Get cookies.txt LOCALLY**,
     mejor desde una cuenta de Google secundaria.
   - `ANTHROPIC_API_KEY`: para que la IA elija los clips.

### Crear clips

- **Desde la galería:** abre *Conectar este dispositivo* y pega un token de GitHub (las instrucciones
  están en la propia página). Después pegas el link y pulsas *Generar clips*.
- **Desde GitHub:** pestaña **Actions → Crear clips → Run workflow**.

Cada video tarda unos minutos en arrancar; luego los clips aparecen uno a uno.

## Versión local (en tu PC)

Doble clic en `iniciar.bat` → http://localhost:8000

## Otras opciones de servidor (requieren tarjeta)

- **Oracle Cloud Always Free:** `servidor/instalar.sh` instala la versión con servidor propio en un Ubuntu.
- **Railway:** detecta el `Dockerfile` (`railway up`); añade un volumen en `/data` y la variable `APP_PASSWORD`.
