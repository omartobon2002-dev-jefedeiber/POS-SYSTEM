# Guía de despliegue de ModaPOS en Seenode

Esta guía lleva ModaPOS de tu computador a producción en Seenode, paso a paso.
Al terminar tendrás:

| Pieza | Qué es en Seenode | Repositorio | Costo aprox. |
|---|---|---|---|
| Base de datos | **PostgreSQL** (Database) | — | ~US$4/mes (Basic) |
| Backend (API Django) | **Web service** | `POS-SYSTEM` (carpeta `backend/`) | según tamaño de instancia |
| Fotos de productos | **Storage** (volumen) del backend | — | US$0,50 por GB/mes (mínimo 5 GB) |
| Frontend (la app web) | **Static site** | `InventorySas` | gratis |

> **Orden obligatorio:** 1) base de datos → 2) backend → 3) frontend → 4) volver
> al backend a poner el dominio del frontend → 5) dominios propios (opcional).
> El frontend necesita la URL del backend al construirse, y el backend necesita
> la URL del frontend para aceptar sus peticiones (CORS).

Tiempo estimado la primera vez: 45–90 minutos.

---

## 0. Antes de empezar (en tu computador)

### 0.1 Sube el código a GitHub

Seenode despliega desde GitHub. Los dos repositorios deben estar actualizados:

```bash
# Backend
cd "POS-SYSTEM"
git add -A && git commit -m "Listo para producción" && git push

# Frontend
cd "../InventorySas"
git add -A && git commit -m "Listo para producción" && git push
```

Comprueba que **no** se suban los archivos `.env` (ya están en `.gitignore`).
Sí deben subirse `backend/.env.production.example` y `.env.production.example`,
que son plantillas sin datos reales.

### 0.2 Genera los secretos (guárdalos en un lugar seguro)

**a) Clave secreta de Django** (una cadena larga y aleatoria):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Guarda el resultado como `DJANGO_SECRET_KEY`. No la cambies después: si cambia,
se cierran todas las sesiones abiertas.

**b) Claves de notificaciones push (VAPID):**

```bash
npx web-push generate-vapid-keys
```

Imprime una `Public Key` y una `Private Key`. Guárdalas como `VAPID_PUBLIC_KEY`
y `VAPID_PRIVATE_KEY`. **No las cambies nunca después**: si cambian, todos los
dispositivos suscritos dejan de recibir notificaciones y hay que reactivarlas.

**c) Contraseña del administrador de la plataforma:** inventa una fuerte, de
16 caracteres o más. Será `DJANGO_SUPERUSER_PASSWORD`. Es la cuenta con la que
entras a `/admin/` y al panel de plataforma. No reutilices la de desarrollo.

**d) Correo para enviar invitaciones (opcional pero recomendado):** si usas
Gmail, crea una "Contraseña de aplicación" en tu cuenta de Google (Seguridad →
Verificación en dos pasos → Contraseñas de aplicación). Esa es
`EMAIL_HOST_PASSWORD`. Sin correo, las invitaciones igual generan su enlace,
pero no se envían por email.

---

## 1. Base de datos (PostgreSQL)

1. En el panel de Seenode ve a la pestaña **Database** → **Create database**.
2. Nombre: `modapos-db`. Tipo: **PostgreSQL**. Versión: **16.14** (es la que
   usamos en desarrollo; la 18 también funciona).
3. Espera unos segundos a que quede creada.
4. Abre la base de datos → pestaña **Connection** → copia la **URI**. Se ve así:

   ```
   postgres://usuario:clave@host-privado:5432/nombre_base
   ```

   Usa el **host privado** (el que sirve dentro de Seenode), no el público.
   Guarda esa URI como `DATABASE_URL`.

---

## 2. Backend (Web service)

### 2.1 Crear el servicio

1. Panel → **New** → **Web service** → conecta GitHub y elige el repositorio
   **`POS-SYSTEM`**, rama `main`.
2. **Language:** Python **3.12**.
3. **Build command:**

   ```bash
   cd backend && pip install -r requirements.txt
   ```

4. **Start command:**

   ```bash
   cd backend && sh entrypoint.sh gunicorn config.wsgi:application --bind 0.0.0.0:80 --workers 2 --timeout 60 --access-logfile -
   ```

   El script `entrypoint.sh` hace, en cada arranque y en este orden: aplicar
   migraciones → cargar los planes de suscripción → crear el admin (solo si
   pusiste las variables del paso 2.2) → preparar los archivos estáticos →
   arrancar el servidor.

5. **Port:** `80`. Debe coincidir con el `--bind 0.0.0.0:80` del Start command.
6. **Replicas:** `1`. Es obligatorio mientras tengas un volumen (paso 2.3).
7. Instancia: la más pequeña sirve para empezar. Si los logs muestran que se
   queda sin memoria, sube un tamaño o baja a `--workers 1`.

> Seenode **no** usa el `Dockerfile` del repositorio al desplegar desde Git
> (ese Dockerfile es para Docker local). Por eso se configuran los comandos a
> mano.

### 2.2 Variables de entorno (pestaña **Envs**)

Crea todas estas variables. Marca como **Secret** las que dicen 🔒.

| Variable | Valor | Nota |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | **Imprescindible.** Sin esto arranca en modo desarrollo (inseguro). |
| `DJANGO_SECRET_KEY` 🔒 | la clave del paso 0.2a | |
| `DATABASE_URL` 🔒 | la URI del paso 1 | |
| `DJANGO_ALLOWED_HOSTS` | `TEMPORAL.seenode.app` | Por ahora pon cualquier valor; lo corriges en el paso 2.4. Va **sin** `https://`. |
| `CSRF_TRUSTED_ORIGINS` | `https://TEMPORAL.seenode.app` | Igual: se corrige en el paso 2.4. Va **con** `https://`. |
| `CORS_ALLOWED_ORIGINS` | `https://TEMPORAL.seenode.app` | Se corrige en el paso 4 con la URL del frontend. |
| `FRONTEND_URL` | `https://TEMPORAL.seenode.app` | Se corrige en el paso 4. Es la base de los enlaces de invitación. |
| `MEDIA_ROOT` | `/data/media` | Dónde se guardan las fotos de productos (dentro del volumen). |
| `REDIS_URL` | *(vacío)* | Sin Redis se usa memoria. Suficiente con 1 réplica. |
| `WEB_CONCURRENCY` | `2` | Informativo; los workers reales los fija el Start command. |
| `VAPID_PUBLIC_KEY` | clave pública del paso 0.2b | |
| `VAPID_PRIVATE_KEY` 🔒 | clave privada del paso 0.2b | |
| `VAPID_SUBJECT` | `mailto:tu-correo-real@dominio.com` | Tiene que ser un correo real. Apple rechaza dominios `.local` o de ejemplo. |
| `DJANGO_SUPERUSER_EMAIL` | tu correo de admin | **Solo para el primer arranque.** |
| `DJANGO_SUPERUSER_PASSWORD` 🔒 | contraseña del paso 0.2c | **Solo para el primer arranque.** |
| `EMAIL_HOST` | `smtp.gmail.com` | Opcional (correo). |
| `EMAIL_PORT` | `587` | Opcional. |
| `EMAIL_HOST_USER` | `tucorreo@gmail.com` | Opcional. |
| `EMAIL_HOST_PASSWORD` 🔒 | contraseña de aplicación | Opcional. |
| `EMAIL_USE_TLS` | `true` | Opcional. |
| `DEFAULT_FROM_EMAIL` | `ModaPOS <tucorreo@gmail.com>` | Opcional. |

La plantilla completa está en
[`backend/.env.production.example`](backend/.env.production.example).

### 2.3 Volumen para las fotos (pestaña **Storage**)

El disco de un web service se **borra en cada reinicio o despliegue**. Sin
volumen, las fotos de productos se pierden.

1. Servicio backend → pestaña **Storage** → **Add storage**.
2. Tamaño: **5 GB** (se puede agrandar después, pero nunca achicar).
3. **Mount path:** `/data`.
4. **Add storage**. El servicio se detiene un momento mientras se prepara.

Con `MEDIA_ROOT=/data/media`, las fotos quedan dentro del volumen.

### 2.4 Primer despliegue y URL real

1. Pulsa **Deploy** y mira la pestaña **Logs**. Deberías ver, en orden:
   - `Applying ...` / `No migrations to apply` (migraciones)
   - `Superuser created successfully.`
   - `... static files copied ...`
   - `Booting worker with pid ...` (gunicorn arrancó)
2. Copia la URL que Seenode le dio al servicio, por ejemplo
   `https://modapos-api-xxxx.seenode.app`.
3. Ve a **Envs** y corrige:
   - `DJANGO_ALLOWED_HOSTS` = `modapos-api-xxxx.seenode.app` (sin `https://`)
   - `CSRF_TRUSTED_ORIGINS` = `https://modapos-api-xxxx.seenode.app`
4. **Borra** `DJANGO_SUPERUSER_EMAIL` y `DJANGO_SUPERUSER_PASSWORD`: el admin ya
   quedó creado y no conviene dejar la contraseña guardada ahí.
5. Aplica los cambios y vuelve a desplegar.

### 2.5 Comprobar que el backend funciona

Abre en el navegador:

| URL | Qué debes ver |
|---|---|
| `https://modapos-api-xxxx.seenode.app/api/v1/health/` | `{"status":"ok","checks":{"database":true,"cache":true}}` |
| `https://modapos-api-xxxx.seenode.app/admin/` | El login del admin de Django, con estilos. Entra con el correo y la contraseña del admin. |
| `https://modapos-api-xxxx.seenode.app/api/v1/reference/` | La documentación de la API. |

Si `/admin/` no tiene estilos o la salud da error, revisa la sección
**Problemas comunes** al final.

---

## 3. Frontend (Static site)

1. Panel → **New** → **Static site** → repositorio **`InventorySas`**, rama
   `main`.
2. **Language:** Node 22 (el que viene por defecto).
3. **Build command:**

   ```bash
   npm install && npm run build
   ```

4. **Publish directory:** `dist`
5. **Client-side routing (SPA):** **activado**. Es lo que hace que enlaces
   como `/invitacion/abc123` abran la app en vez de dar 404.
6. **Envs** (variables de construcción):

   | Variable | Valor |
   |---|---|
   | `VITE_API_BASE_URL` | `https://modapos-api-xxxx.seenode.app/api/v1` |

   ⚠️ Es la URL **del backend** (paso 2.4) terminada en `/api/v1`, sin `/` al
   final. Esta variable se "hornea" dentro de la app al construirla: si algún día
   cambias el dominio del backend, hay que cambiarla y **volver a desplegar el
   frontend**. Tu `.env` local (`/api/v1`) no sirve en producción.

7. **Deploy** y mira los **Logs** hasta que diga que terminó.
8. Copia la URL del frontend, por ejemplo `https://modapos-xxxx.seenode.app`.

### 3.1 Encabezado para el service worker (recomendado)

Así los clientes reciben las actualizaciones de la app de inmediato:

Static site → pestaña **Routing** → **Custom headers** → agrega:

- Ruta: `/sw.js`
- Encabezado: `Cache-Control` = `no-cache`

---

## 4. Conectar backend y frontend

Vuelve al **backend** → **Envs** y pon la URL del frontend (paso 3.8):

| Variable | Valor |
|---|---|
| `CORS_ALLOWED_ORIGINS` | `https://modapos-xxxx.seenode.app` |
| `FRONTEND_URL` | `https://modapos-xxxx.seenode.app` |

Ambas **con** `https://` y **sin** `/` al final. Aplica y redespliega el backend.

> Si falta este paso, la app carga pero el login falla. En la consola del
> navegador (F12) aparece un error de **CORS**.

---

## 5. Prueba completa en producción

Hazla en este orden, desde un PC con Windows y Chrome o Edge:

1. **Login:** abre `https://modapos-xxxx.seenode.app` y entra con el admin.
2. **Crear negocio:** crea la organización, una sede y una caja.
3. **Catálogo:** crea una categoría y un producto **con foto**. Recarga la
   página: la foto debe seguir ahí.
4. **Foto persistente:** en Seenode redespliega el backend y vuelve a mirar el
   producto. Si la foto sigue, el volumen está bien.
5. **Inventario:** registra entrada de stock.
6. **Caja:** abre el turno con una base.
7. **Venta:** vende en el POS e imprime la tirilla (ver la sección 6).
8. **Invitación:** invita a un cajero. Abre el enlace del correo (o cópialo)
   en una ventana de incógnito: debe abrir la pantalla de aceptar invitación.
9. **Notificaciones:** con el admin, en Configuración activa las notificaciones
   y acepta el permiso del navegador. Desde otra sesión (el cajero), registra
   una venta: al admin le debe llegar la notificación.
   - Las suscripciones de pruebas locales **no** sirven en producción: cada
     dispositivo tiene que activarlas de nuevo aquí.
   - En iPhone solo funcionan si la app está instalada en la pantalla de inicio
     (Compartir → "Agregar a inicio") con iOS 16.4 o superior.
10. **Cierre:** cierra la caja con el arqueo e imprime el Reporte Z.
11. **Instalar como app (opcional):** en Chrome, el ícono de instalar en la
    barra de direcciones. Queda como un programa más en Windows.

---

## 6. Impresora térmica en los PCs de los clientes (Windows)

La app es una página web. En el PC del cliente **solo** hay que instalar el
driver de la impresora; nada más.

### 6.1 Instalar el driver (una sola vez por PC)

1. Descarga el **driver oficial de Xprinter** para Windows (el enlace está en
   la app: Configuración → Impresora → guía de Windows).
2. Instala el modelo **XP-80** (80 mm). **No** uses el driver genérico
   "POS-58": ese es de 58 mm, corta mal el contenido y no activa el cortador.
3. Conecta la impresora por USB. En **Configuración de Windows → Impresoras →
   (la impresora) → Propiedades de impresora → Puertos**, marca el puerto
   **USB001** (o el USB que aparezca).
4. Imprime una página de prueba desde Windows para confirmar.
5. Opcional: márcala como **impresora predeterminada**.

### 6.2 Configurar la impresión en Chrome o Edge (una sola vez por PC)

En la app, haz una venta de prueba y pulsa imprimir. En el cuadro de impresión:

- **Destino:** la impresora XP-80.
- **Más opciones → Márgenes:** **Ninguno**.
- **Escala:** Predeterminada (100%).
- **Encabezados y pies de página:** desactivado.

Chrome recuerda esta configuración para las próximas impresiones.

### 6.3 En la app

**Configuración → Impresora**: modo **Navegador** (el recomendado). El agente
de impresión es opcional y solo hace falta en Linux, donde el navegador deja
papel en blanco al final.

---

## 7. Dominio propio (opcional)

Si compras, por ejemplo, `mitienda.com`:

- Frontend en `app.mitienda.com`
- Backend en `api.mitienda.com`

Pasos:

1. En cada servicio: pestaña **Domains** → **Add custom domain** → escribe el
   dominio → **Get DNS config**.
2. En tu proveedor de dominio crea los registros **CNAME exactamente como los
   muestra Seenode**, sin inventar nombres. Si usas Cloudflare, deja la nube en
   **gris** (DNS only).
3. **Verify domain**. En unos 10 minutos (hasta 24 h) aparece **SSL Active**.
4. Actualiza las variables. Puedes dejar varios valores separados por coma
   mientras haces el cambio:

   **Backend:**

   ```
   DJANGO_ALLOWED_HOSTS=api.mitienda.com,modapos-api-xxxx.seenode.app
   CSRF_TRUSTED_ORIGINS=https://api.mitienda.com
   CORS_ALLOWED_ORIGINS=https://app.mitienda.com,https://modapos-xxxx.seenode.app
   FRONTEND_URL=https://app.mitienda.com
   ```

   **Frontend:**

   ```
   VITE_API_BASE_URL=https://api.mitienda.com/api/v1
   ```

5. Redespliega **los dos** (el frontend es obligatorio, por la variable
   `VITE_`).

> Cambiar el dominio del frontend es como cambiar de "sitio" para el
> navegador: los clientes tienen que volver a iniciar sesión, reinstalar la app
> y reactivar las notificaciones. Decide el dominio definitivo **antes** de
> entregarle la app a clientes reales.

---

## 8. Actualizaciones del día a día

- **Auto-deploy:** en cada servicio → **Settings** → activa el despliegue
  automático con cada `git push` a `main`. Si no, usa el menú **Deploy**.
- **Backend:** haz `git push` y listo. Las migraciones se aplican solas al
  arrancar.
- **Frontend:** haz `git push`. Los clientes reciben la nueva versión al
  recargar o volver a abrir la app.
- Haz los despliegues **fuera del horario de venta**: el backend se reinicia
  unos segundos. Las ventas hechas en ese momento quedan guardadas sin conexión
  y se sincronizan solas al volver.

---

## 9. Copias de seguridad

Revisa en la base de datos de Seenode si tu plan incluye backups automáticos.
Aunque los tenga, haz también una copia manual periódica (semanal, como mínimo)
desde tu PC. Usa el **host público** de la base de datos, que se habilita en su
configuración de red:

```bash
pg_dump "postgres://usuario:clave@HOST-PUBLICO:5432/nombre_base" -Fc -f modapos-$(date +%F).dump
```

Para restaurar (⚠️ reemplaza los datos actuales):

```bash
pg_restore --clean --no-owner -d "postgres://usuario:clave@HOST:5432/nombre_base" modapos-AAAA-MM-DD.dump
```

Las fotos están en el volumen `/data/media`. Si son importantes, descárgalas
de vez en cuando.

---

## 10. Problemas comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| **502 Bad Gateway** en el backend | gunicorn no escucha en el puerto configurado, o se cayó al arrancar. | Revisa que **Port** = `80` y que el Start command diga `--bind 0.0.0.0:80`. Mira los **Logs**: el error real aparece ahí. |
| Logs: `ImproperlyConfigured: Set the DJANGO_SECRET_KEY` (o `ALLOWED_HOSTS`) | Falta esa variable. | Agrégala en **Envs** y redespliega. |
| **400 Bad Request** en todas las URL del backend | El dominio no está en `DJANGO_ALLOWED_HOSTS`. | Agrégalo sin `https://` y sin `/`. |
| Logs: `could not connect to server` / `connection refused` (base de datos) | `DATABASE_URL` mal copiada, o usa el host público sin permiso. | Copia otra vez la URI desde **Connection**, con el host privado. |
| Logs: error de base de datos que menciona **SSL** | La base exige SSL. | Agrega `?sslmode=require` al final de `DATABASE_URL`. |
| La app carga, pero el login dice "Error de red" | Error de **CORS**: el dominio del frontend no está en `CORS_ALLOWED_ORIGINS`, o `VITE_API_BASE_URL` apunta mal. | Revisa el paso 4 (con `https://`, sin `/` final) y la variable del frontend. En F12 → Consola se ve el error exacto. |
| La app pide a `https://modapos-xxxx.seenode.app/api/v1/...` y da 404 | El frontend se construyó sin `VITE_API_BASE_URL`. | Pon la variable y **redespliega el frontend**. |
| `/admin/`: "CSRF verification failed" | Falta `CSRF_TRUSTED_ORIGINS`. | `https://` + dominio del backend. |
| `/admin/` sin estilos | Falló `collectstatic` al arrancar. | Revisa los Logs del arranque. |
| Las fotos desaparecen tras un despliegue | No hay volumen, o `MEDIA_ROOT` no apunta dentro de él. | Paso 2.3: mount `/data` y `MEDIA_ROOT=/data/media`. |
| El enlace de invitación da 404 | **Client-side routing (SPA)** desactivado en el static site. | Actívalo y redespliega. |
| El enlace de invitación apunta a `localhost` | `FRONTEND_URL` no está bien puesto. | Paso 4. |
| No llegan las notificaciones | El dispositivo no activó las notificaciones en producción, faltan las claves VAPID o `VAPID_SUBJECT` no es un correo real. | Revisa las variables `VAPID_*`, reactiva las notificaciones en el dispositivo y mira los Logs del backend al hacer una venta. |
| Error `cash_session_stale` al vender | La caja se abrió otro día y no se cerró (regla de caja diaria). | Cierra el turno viejo con arqueo y abre uno nuevo. |
| Sale mucho papel en blanco o el contenido cortado | Driver equivocado (POS-58) o márgenes de Chrome. | Sección 6: driver XP-80 y márgenes "Ninguno". |

---

## 11. Lista de chequeo final

- [ ] Los dos repositorios subidos a GitHub, sin archivos `.env`.
- [ ] PostgreSQL creada; `DATABASE_URL` con el host privado.
- [ ] Backend: Build/Start commands, Port 80, 1 réplica.
- [ ] Backend: `DJANGO_SETTINGS_MODULE=config.settings.production`.
- [ ] Backend: `DJANGO_SECRET_KEY` y `VAPID_*` generadas y guardadas aparte.
- [ ] Volumen `/data` con `MEDIA_ROOT=/data/media`.
- [ ] Admin creado; `DJANGO_SUPERUSER_*` **borradas** después.
- [ ] `/api/v1/health/` responde `ok`.
- [ ] Frontend: static site, `dist`, SPA activado, `VITE_API_BASE_URL` correcto.
- [ ] `CORS_ALLOWED_ORIGINS` y `FRONTEND_URL` con la URL del frontend.
- [ ] Prueba completa de la sección 5 hecha.
- [ ] Impresora probada en un PC Windows real (sección 6).
- [ ] Auto-deploy activado (opcional).
- [ ] Primera copia de seguridad hecha.
